"""Primer corte real LAB: datos persistidos, reglas sin futuro y ejecuciones reproducibles."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from threading import Event
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import ENGINE_VERSION, DatasetImport, chart_series_precision, create_lab_router, download_binance
from app.lab.engine import MAX_DATASET_CANDLES, IndicatorCache, LabError, evaluate, run_backtest, validate_candles, validate_strategy
from app.lab.store import LabStore, encoded


START = 1735689600  # 2025-01-01 UTC
STEP = 3600


def test_chart_curve_rounding_does_not_mutate_exact_run():
    exact = "1.123456789012345678901234567890123456789"
    original = {"equity": [{"t": START, "equity": exact}],
                "drawdown": [{"t": START, "drawdown_pct": exact}], "metrics": {"net_profit": exact}}
    chart = chart_series_precision(original)
    assert chart["equity"] == [{"t": START, "equity": "1.12345678901"}]
    assert chart["drawdown"] == [{"t": START, "drawdown_pct": "1.12345678901"}]
    assert chart["metrics"] == original["metrics"]
    assert original["equity"][0]["equity"] == exact


def rows(prices=(10, 11, 12, 13, 14)):
    return [{"t": START + i * STEP, "o": str(price), "h": str(price + 1),
             "l": str(price - 1), "c": str(price), "v": "10"} for i, price in enumerate(prices)]


def definition(direction="LONG"):
    return {"direction": direction, "initial_capital": "1000", "allocation": "1",
            "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1", "take_profit": "0.2",
            "entry": {"op": "AND", "children": [{"kind": "CLOSE_ABOVE_EMA", "period": 2}]}}


@pytest.mark.parametrize("extra", [
    {"leverage": "10"},
    {"execution_mode": "REALISTIC"},
    {"funding_rate": "0.0001"},
])
def test_strategy_rejects_unimplemented_parameters_instead_of_ignoring_them(extra):
    with pytest.raises(LabError, match="no implementados"):
        validate_strategy({**definition(), **extra})


@pytest.mark.parametrize("entry", [
    {"kind": "CLOSE_ABOVE_EMA", "period": 2, "venue": "grvt"},
    {"op": "AND", "children": [{"kind": "CLOSE_ABOVE_EMA", "period": 2}],
     "max_positions": 2},
])
def test_rules_reject_unimplemented_conditions_instead_of_ignoring_them(entry):
    with pytest.raises(LabError, match="no implementados"):
        validate_strategy({**definition(), "entry": entry})


def test_api_does_not_save_a_strategy_with_ignored_execution_settings(tmp_path):
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "phase two validation password"}).status_code == 200
    response = client.post("/lab/strategies", json={
        "name": "Apalancamiento no implementado",
        "definition": {**definition(), "leverage": "10"},
    })
    assert response.status_code == 422
    assert "no implementados" in response.text
    assert LabStore(path).strategies() == []


def test_candle_validation_exposes_gaps_and_rejects_open_or_duplicate():
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    assert len(bars) == 5 and gaps == []
    missing = rows()[:2] + rows()[3:]
    assert validate_candles(missing, "1h", now=START + 10 * STEP)[1][0]["missing"] == 1
    with pytest.raises(LabError, match="duplicadas"):
        validate_candles([*rows()[:2], rows()[1]], "1h", now=START + 10 * STEP)
    with pytest.raises(LabError, match="abierta"):
        validate_candles(rows(), "1h", now=START + 4 * STEP)
    for malformed in ("1e999999999", "1e-999999999", "NaN"):
        bad = rows()
        bad[0]["o"] = malformed
        with pytest.raises(LabError, match="rango"):
            validate_candles(bad, "1h", now=START + 10 * STEP)


def test_signal_uses_closed_candle_and_next_open_for_entry():
    bars, _ = validate_candles(rows(), "1h", now=START + 10 * STEP)
    strategy = validate_strategy(definition())
    result = run_backtest(bars, strategy)
    assert result["events"][0]["signal_time"] == START + STEP
    assert result["trades"][0]["entry_time"] == START + 2 * STEP
    assert result["trades"][0]["entry_price"] == "12"
    assert result["metrics"]["trades"] >= 1
    assert result["metrics"]["ambiguous_exits"] == 0
    assert result["metrics"]["sample_size"] == len(result["events"])
    assert result["events"][0]["outcomes_pct"]["3600"] is not None
    assert result["events"][0]["mfe_pct"] is not None
    assert len(result["drawdown"]) == len(bars)
    assert evaluate(strategy["entry"], bars, 0)[0] is False
    modified = list(bars)
    modified[3] = type(bars[3])(bars[3].t, Decimal("99"), Decimal("100"), Decimal("98"), Decimal("99"), Decimal("10"))
    assert evaluate(strategy["entry"], modified, 1) == evaluate(strategy["entry"], bars, 1)
    assert run_backtest(bars, strategy) == run_backtest(bars, strategy)


def test_intrabar_ambiguity_uses_stop_and_short_is_explicitly_experimental():
    data = rows()
    data[2] = {**data[2], "h": "20", "l": "5"}
    bars, _ = validate_candles(data, "1h", now=START + 10 * STEP)
    result = run_backtest(bars, validate_strategy(definition()))
    assert result["trades"][0]["reason"] == "STOP_AND_TARGET_AMBIGUOUS"
    assert result["metrics"]["ambiguous_exits"] == 1
    assert run_backtest(bars, validate_strategy(definition()))["metrics"]["ambiguous_exits"] == 1
    assert result["events"][-1]["action"] == "SKIPPED_NO_NEXT_CANDLE"
    short = validate_strategy({**definition("SHORT"), "entry": {"kind": "RSI_GT", "period": 2, "threshold": "50"}})
    short_result = run_backtest(bars, short)
    assert "Short sintético experimental" in short_result["assumptions"][-1]
    assert all(Decimal(row["mfe_pct"]) >= 0 and Decimal(row["mae_pct"]) <= 0
               for row in short_result["events"] if row["mfe_pct"] is not None)


def test_independent_exit_rule_executes_at_next_open_before_intrabar_stop():
    data = rows()
    data[3]["l"] = "1"  # El stop habría saltado dentro de la vela de salida.
    bars, _ = validate_candles(data, "1h", now=START + 10 * STEP)
    exit_rule = {"kind": "RSI_GT", "period": 2, "threshold": "60"}
    strategy = validate_strategy({**definition(), "exit": exit_rule})
    result = run_backtest(bars, strategy)
    trade = result["trades"][0]
    assert trade["entry_time"] == START + 2 * STEP
    assert trade["exit_signal_time"] == START + 2 * STEP
    assert trade["exit_time"] == START + 3 * STEP
    assert trade["exit_price"] == "13"
    assert trade["reason"] == "EXIT_RULE"
    assert trade["exit_explanation"]["matched"] is True
    assert result["exit_events"][0]["planned_exit_time"] == trade["exit_time"]
    assert result["metrics"]["exposure_pct"] == "40.0"
    assert "exit" not in validate_strategy(definition())  # Versiones antiguas conservan su semántica.
    with pytest.raises(LabError, match="no implementados"):
        validate_strategy({**definition(), "exit": {"kind": "FIBONACCI", "period": 2}})


def test_range_breakout_retest_confirms_once_without_future_candles():
    prices = [
        (99, 101, 98, 99), (99, 100, 97, 98), (98, 102, 97, 100),
        (101, 105, 100, 104), (104, 105, Decimal("101.5"), 103),
        (106, 108, 105, 107), (107, 109, 107, 108),
    ]
    bars, _ = validate_candles([
        {"t": START + i * STEP, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": "10"}
        for i, (o, h, l, c) in enumerate(prices)
    ], "1h", now=START + 10 * STEP)
    rule = {"kind": "RETEST_ABOVE_RANGE", "period": 3, "retest_bars": 3, "tolerance_pct": "0.01"}
    strategy = validate_strategy({**definition(), "entry": rule})
    assert [evaluate(strategy["entry"], bars, i)[0] for i in range(len(bars))] == [False, False, False, False, True, False, False]
    matched, explanation = evaluate(strategy["entry"], bars, 4)
    assert matched and explanation["breakout_time"] == START + 3 * STEP
    assert explanation["level"] == "102" and explanation["retest_extreme"] == "101.5"
    changed = list(bars)
    changed[5] = type(bars[5])(bars[5].t, Decimal("50"), Decimal("51"), Decimal("49"), Decimal("50"), Decimal("10"))
    assert evaluate(strategy["entry"], changed, 4) == evaluate(strategy["entry"], bars, 4)
    run = run_backtest(bars, strategy)
    assert len(run["events"]) == 1
    assert run["events"][0]["signal_time"] == START + 4 * STEP
    assert run["events"][0]["entry_time"] == START + 5 * STEP
    assert run["trades"][0]["entry_price"] == "106"

    invalidated = list(bars)
    invalidated[4] = type(bars[4])(bars[4].t, Decimal("104"), Decimal("105"), Decimal("99"), Decimal("100"), Decimal("10"))
    assert evaluate(strategy["entry"], invalidated, 4)[0] is False
    assert evaluate(strategy["entry"], invalidated, 5)[0] is False


def test_range_retest_down_and_parameter_validation():
    prices = [(101, 102, 100, 101), (101, 103, 99, 102), (102, 103, 98, 100),
              (99, 100, 94, 95), (95, Decimal("98.5"), 94, 97), (96, 97, 90, 92)]
    bars, _ = validate_candles([
        {"t": START + i * STEP, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": "10"}
        for i, (o, h, l, c) in enumerate(prices)
    ], "1h", now=START + 10 * STEP)
    rule = {"kind": "RETEST_BELOW_RANGE", "period": 3, "retest_bars": 2, "tolerance_pct": "0.01"}
    clean = validate_strategy({**definition("SHORT"), "entry": rule})["entry"]
    assert evaluate(clean, bars, 3)[0] is False
    assert evaluate(clean, bars, 4)[0] is True
    assert evaluate(clean, bars, 5)[0] is False
    for change in ({"period": 101}, {"retest_bars": 0}, {"tolerance_pct": "0.2"}):
        with pytest.raises(LabError, match="Retesteo"):
            validate_strategy({**definition(), "entry": {**rule, **change}})


def test_nested_and_or_groups_keep_semantics_and_bound_work():
    bars, _ = validate_candles(rows(), "1h", now=START + 10 * STEP)
    nested = {"op": "AND", "children": [
        {"kind": "CLOSE_ABOVE_EMA", "period": 2},
        {"op": "OR", "children": [
            {"kind": "RSI_GT", "period": 2, "threshold": "70"},
            {"kind": "VOLUME_ABOVE_AVG", "period": 2, "factor": "2"},
        ]},
    ]}
    clean = validate_strategy({**definition(), "entry": nested})["entry"]
    matched, explanation = evaluate(clean, bars, 2)
    assert matched is True
    assert explanation["children"][1]["op"] == "OR"
    assert [child["matched"] for child in explanation["children"][1]["children"]] == [True, False]
    assert run_backtest(bars, validate_strategy({**definition(), "entry": nested}))["events"][0]["signal_time"] == START + 2 * STEP

    oversized = {"op": "AND", "children": [
        {"op": "OR", "children": [{"kind": "CLOSE_ABOVE_EMA", "period": 2} for _ in range(8)]}
        for _ in range(8)
    ]}
    with pytest.raises(LabError, match="64 nodos"):
        validate_strategy({**definition(), "entry": oversized})
    too_deep = {"kind": "CLOSE_ABOVE_EMA", "period": 2}
    for _ in range(5):
        too_deep = {"op": "AND", "children": [too_deep]}
    with pytest.raises(LabError, match="anidada"):
        validate_strategy({**definition(), "entry": too_deep})


def test_cached_indicators_match_direct_evaluation_and_prefixes():
    prices = (10, 11, 9, 12, 13, 11, 14, 15, 13, 16)
    raw = rows(prices)
    for index, bar in enumerate(raw):
        bar["v"] = str(10 + index % 3)
    bars, _ = validate_candles(raw, "1h", now=START + 20 * STEP)
    rules = [
        {"kind": "CLOSE_ABOVE_EMA", "period": 3},
        {"kind": "RSI_GT", "period": 3, "threshold": "55"},
        {"kind": "VOLUME_ABOVE_AVG", "period": 3, "factor": "1.01"},
    ]
    clean = [validate_strategy({**definition(), "entry": rule})["entry"] for rule in rules]
    full = IndicatorCache(bars)
    for index in range(len(bars)):
        prefix = IndicatorCache(bars[:index + 1])
        for rule in clean:
            direct = evaluate(rule, bars, index)
            assert evaluate(rule, bars, index, full) == direct
            assert evaluate(rule, bars[:index + 1], index, prefix) == direct


def test_store_versions_dataset_and_runs_survive_reopen(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    assert store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)["id"] == dataset["id"]
    first = store.create_strategy("EMA", validate_strategy(definition()))
    result = run_backtest(bars, first["definition"])
    run = store.save_run(dataset["id"], first["version_id"], result, "lab-1.0")
    changed = store.add_version(first["id"], validate_strategy({**definition(), "take_profit": "0.3"}))
    assert changed["version"] == 2
    reopened = LabStore(path)
    assert reopened.version(run["version_id"])["definition"] == first["definition"]
    assert reopened.run(run["id"])["trades"] == result["trades"]
    assert reopened.dataset(dataset["id"])[1] == [bar.json() for bar in bars]
    assert path.read_bytes()[:16] == b"SQLite format 3\x00"  # LAB no contiene patrimonio real; documentar límite.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE datasets SET candles_json='[]' WHERE id=?", (dataset["id"],))
    with pytest.raises(LabError, match="Huella"):
        reopened.dataset(dataset["id"])


def test_five_year_four_hour_history_can_be_saved_backtested_and_reopened(tmp_path):
    count = 5 * 365 * 6
    step = 4 * STEP
    raw = [{"t": START + i * step, "o": "10", "h": "11", "l": "9", "c": "10", "v": "10"}
           for i in range(count)]
    bars, gaps = validate_candles(raw, "4h", now=START + (count + 1) * step)
    assert len(bars) == count and not gaps
    assert len(DatasetImport(symbol="BTC/USDT", timeframe="4h", source="fixture", candles=raw).candles) == count
    store = LabStore(tmp_path / "long-history.db")
    dataset = store.save_dataset("BTC/USDT", "4h", "fixture-five-years", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    result = run_backtest(bars, strategy["definition"], "4h")
    run = store.save_run(dataset["id"], strategy["version_id"], result, ENGINE_VERSION)
    reopened = LabStore(tmp_path / "long-history.db")
    assert len(reopened.dataset(dataset["id"])[1]) == count
    assert reopened.run(run["id"])["metrics"] == result["metrics"]
    with pytest.raises(LabError, match="20000 velas"):
        validate_candles(raw * 2, "4h", now=START + (count + 1) * step)


def test_run_pages_are_stable_and_do_not_load_the_whole_event_dataset(tmp_path):
    store = LabStore(tmp_path / "pages.db")
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    result = run_backtest(bars, strategy["definition"])
    result["events"] = [{"signal_time": START + index * STEP, "index": index,
                         "action": "ENTER_NEXT_OPEN"} for index in range(121)]
    result["trades"] = [{"entry_time": START + index * STEP, "exit_time": START + (index + 1) * STEP,
                         "entry_price": "10", "exit_price": "11", "direction": "LONG", "pnl": "1",
                         "reason": "TARGET", "index": index} for index in range(61)]
    run = store.save_run(dataset["id"], strategy["version_id"], result, ENGINE_VERSION)
    reopened = LabStore(tmp_path / "pages.db")
    first = reopened.run_rows_page(run["id"], "events", 0, 50)
    middle = reopened.run_rows_page(run["id"], "events", 50, 50)
    last = reopened.run_rows_page(run["id"], "events", 100, 50)
    assert [len(page["items"]) for page in (first, middle, last)] == [50, 50, 21]
    assert first["total"] == middle["total"] == last["total"] == 121
    assert [item["index"] for page in (first, middle, last) for item in page["items"]] == list(range(121))
    assert reopened.run_rows_page(run["id"], "trades", 50, 50)["items"][-1]["index"] == 60
    view = reopened.run_chart_view(run["id"])
    assert view["view_mode"] == "chart"
    assert "outcomes_pct" not in str(view)
    assert len(view["trades"]) == 61
    assert len(encoded(view)) < len(encoded(run))
    assert all(set(trade) == {"entry_time", "exit_time", "entry_price", "exit_price", "direction", "pnl", "reason"}
               for trade in view["trades"])
    shell = reopened.run_chart_view(run["id"], include_markers=False)
    assert shell["events"] == shell["trades"] == []
    assert shell["signal_count"] == 121 and shell["trade_count"] == 61
    assert shell["markers_windowed"] is True
    assert len(encoded(shell)) < len(encoded(view))
    window = reopened.run_chart_markers(run["id"], START + 50 * STEP, START + 51 * STEP)
    assert [event["index"] for event in window["events"]] == [50, 51]
    assert [trade["entry_time"] for trade in window["trades"]] == [START + i * STEP for i in (49, 50, 51)]
    assert all(set(trade) == {"entry_time", "exit_time", "entry_price", "exit_price", "direction", "pnl", "reason"}
               for trade in window["trades"])
    with pytest.raises(LabError, match="Backtest no encontrado"):
        reopened.run_rows_page("missing", "events", 0, 50)


def test_api_import_create_run_reopen_and_reject_gaps(tmp_path):
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "phase two encrypted lab password"}).status_code == 200
    dataset = client.post("/lab/datasets", json={"symbol": "BTC/USDT", "timeframe": "1h", "source": "archivo CSV",
                                               "candles": rows()}).json()
    assert dataset["count"] == 5
    chart_dataset = client.get(f"/lab/datasets/{dataset['id']}?view=chart")
    assert chart_dataset.status_code == 200
    assert chart_dataset.headers["cache-control"] == "no-store"
    assert chart_dataset.json()["candles"] == []
    assert chart_dataset.json()["times"] == [row["t"] for row in rows()]
    assert chart_dataset.json()["hash"] == dataset["hash"]
    assert chart_dataset.json()["candles_windowed"] is True
    candle_first = client.get(f"/lab/datasets/{dataset['id']}/candles?from_t={START}&to_t={START + 4 * STEP}&limit=2")
    assert candle_first.status_code == 200 and candle_first.headers["cache-control"] == "no-store"
    assert candle_first.json()["candles"] == rows()[:2]
    assert candle_first.json()["dataset_hash"] == dataset["hash"]
    assert candle_first.json()["has_more"] is True and candle_first.json()["next_after_index"] == 1
    candle_second = client.get(f"/lab/datasets/{dataset['id']}/candles?from_t={START}&to_t={START + 4 * STEP}&after_index=1&limit=2").json()
    candle_third = client.get(f"/lab/datasets/{dataset['id']}/candles?from_t={START}&to_t={START + 4 * STEP}&after_index=3&limit=2").json()
    assert candle_first.json()["candles"] + candle_second["candles"] + candle_third["candles"] == rows()
    assert candle_third["has_more"] is False and candle_third["next_after_index"] is None
    assert client.get(f"/lab/datasets/{dataset['id']}/candles?from_t={START + STEP}&to_t={START}").status_code == 422
    assert client.get(f"/lab/datasets/{dataset['id']}/candles?from_t={START}&to_t={START + STEP}&limit=2001").status_code == 422
    candle_plot = client.get(f"/lab/datasets/{dataset['id']}/candles?from_t={START}&to_t={START + 4 * STEP}&limit=20000&view=plot")
    assert candle_plot.status_code == 200 and candle_plot.headers["cache-control"] == "no-store"
    assert candle_plot.json()["dataset_hash"] == dataset["hash"]
    assert candle_plot.json()["candles"] == [{"t":row["t"], **{key:float(row[key]) for key in "ohlc"}} for row in rows()]
    assert not candle_plot.json()["has_more"]
    assert client.get(f"/lab/datasets/missing/candles?from_t={START}&to_t={START + STEP}").status_code == 404
    strategy = client.post("/lab/strategies", json={"name": "EMA", "definition": definition()}).json()
    assert strategy["version"] == 1
    response = client.post("/lab/runs", json={"dataset_id": dataset["id"], "version_id": strategy["version_id"]})
    assert response.status_code == 200, response.text
    run = response.json()
    assert len(run["events"]) >= 1
    assert client.get(f"/lab/runs/{run['id']}").json() == run
    listing = client.get("/lab/runs").json()
    assert listing[0]["id"] == run["id"]
    assert listing[0]["integrity"] == "unchecked"
    assert "metrics" not in listing[0]
    checked_view = client.get(f"/lab/runs/{run['id']}?view=chart&markers=window&curves=window&require_verified=true")
    assert checked_view.status_code == 200
    assert checked_view.json()["verified"] is True
    assert checked_view.json()["metrics"] == run["metrics"]
    chart_view = client.get(f"/lab/runs/{run['id']}?view=chart").json()
    assert chart_view["view_mode"] == "chart"
    assert chart_view["metrics"] == run["metrics"] and chart_view["risk"] == run["risk"]
    assert all(set(event) == {"index", "signal_time", "action"} for event in chart_view["events"])
    assert all(event["action"] == "ENTER_NEXT_OPEN" for event in chart_view["events"])
    assert chart_view["exit_event_count"] == len(run["exit_events"])
    shell_response = client.get(f"/lab/runs/{run['id']}?view=chart&markers=window")
    assert shell_response.headers["cache-control"] == "no-store"
    shell = shell_response.json()
    assert shell["events"] == shell["trades"] == []
    assert shell["signal_count"] == len(chart_view["events"])
    assert shell["trade_count"] == len(run["trades"])
    assert shell["markers_windowed"] is True
    curve_shell = client.get(f"/lab/runs/{run['id']}?view=chart&markers=window&curves=window")
    assert curve_shell.status_code == 200
    assert curve_shell.headers["cache-control"] == "no-store"
    assert curve_shell.json()["equity"] == curve_shell.json()["drawdown"] == []
    assert curve_shell.json()["curve_point_count"] == len(run["equity"])
    assert curve_shell.json()["risk"] == run["risk"]
    assert curve_shell.json()["metrics"] == run["metrics"]
    assert client.get(f"/lab/runs/{run['id']}?curves=window").status_code == 422
    from_t, to_t = dataset["start_t"], dataset["end_t"]
    first_curve = client.get(f"/lab/runs/{run['id']}/curves?from_t={from_t}&to_t={to_t}&limit=2")
    assert first_curve.status_code == 200
    assert first_curve.headers["cache-control"] == "no-store"
    first_page = first_curve.json()
    assert first_page["equity"] == run["equity"][:2]
    assert first_page["drawdown"] == run["drawdown"][:2]
    assert first_page["has_more"] is True and first_page["next_after_index"] == 1
    second_page = client.get(f"/lab/runs/{run['id']}/curves?from_t={from_t}&to_t={to_t}"
                             f"&after_index=1&limit=2").json()
    third_page = client.get(f"/lab/runs/{run['id']}/curves?from_t={from_t}&to_t={to_t}"
                            f"&after_index=3&limit=2").json()
    assert first_page["equity"] + second_page["equity"] + third_page["equity"] == run["equity"]
    assert second_page["has_more"] is True and second_page["next_after_index"] == 3
    assert third_page["has_more"] is False and third_page["next_after_index"] is None
    single_curve = client.get(f"/lab/runs/{run['id']}/curves?from_t={from_t + STEP}&to_t={from_t + STEP}").json()
    assert single_curve["equity"] == run["equity"][1:2]
    assert client.get(f"/lab/runs/{run['id']}/curves?from_t={to_t}&to_t={from_t}").status_code == 422
    assert client.get(f"/lab/runs/{run['id']}/curves?from_t={from_t}&to_t={to_t}&limit=2001").status_code == 422
    curve_plot = client.get(f"/lab/runs/{run['id']}/curves?from_t={from_t}&to_t={to_t}&limit=20000&view=plot")
    assert curve_plot.status_code == 200 and curve_plot.headers["cache-control"] == "no-store"
    assert not curve_plot.json()["has_more"]
    for field, key in (("equity", "equity"), ("drawdown", "drawdown_pct")):
        assert curve_plot.json()[field] == [{"t": row["t"], key: float(row[key])} for row in run[field]]
    assert client.get(f"/lab/runs/{run['id']}/curves?from_t={from_t}&to_t={to_t}&limit=20001&view=plot").status_code == 422
    assert client.get(f"/lab/runs/missing/curves?from_t={from_t}&to_t={to_t}").status_code == 404
    window = client.get(f"/lab/runs/{run['id']}/markers?from_t={from_t}&to_t={to_t}")
    assert window.status_code == 200
    assert window.headers["cache-control"] == "no-store"
    assert window.json()["events"] == chart_view["events"]
    assert window.json()["trades"] == chart_view["trades"]
    assert client.get(f"/lab/runs/{run['id']}/markers?from_t={to_t}&to_t={from_t}").status_code == 422
    assert client.get(f"/lab/runs/{run['id']}/markers?from_t=0&to_t=999999999999999999999").status_code == 422
    assert client.get(f"/lab/runs/missing/markers?from_t=0&to_t=1").status_code == 404
    assert client.get(f"/lab/runs/{run['id']}/markers?from_t={from_t}&to_t={to_t}",
                      headers={"Origin": "https://other.invalid"}).status_code == 403
    assert client.get(f"/lab/runs/{run['id']}?view=invalid").status_code == 422
    chart_creation = client.post("/lab/runs?view=chart", json={"dataset_id": dataset["id"],
                                                           "version_id": strategy["version_id"]})
    assert chart_creation.status_code == 200
    assert chart_creation.json()["view_mode"] == "chart"
    window_creation = client.post("/lab/runs?view=chart&markers=window&curves=window",
                                  json={"dataset_id": dataset["id"],
                                        "version_id": strategy["version_id"]})
    assert window_creation.status_code == 200
    assert window_creation.json()["equity"] == window_creation.json()["drawdown"] == []
    assert window_creation.json()["curve_point_count"] == len(run["equity"])
    assert client.get(f"/lab/runs/{window_creation.json()['id']}").json()["equity"] == run["equity"]
    shell_creation = client.post("/lab/runs?view=chart&markers=window", json={"dataset_id": dataset["id"],
                                                              "version_id": strategy["version_id"]})
    assert shell_creation.status_code == 200
    assert shell_creation.json()["events"] == shell_creation.json()["trades"] == []
    assert client.get(f"/lab/runs/{chart_creation.json()['id']}").json()["events"] == run["events"]
    events_page = client.get(f"/lab/runs/{run['id']}/events?offset=0&limit=1").json()
    assert events_page == {"run_id": run["id"], "offset": 0, "limit": 1,
                           "total": len(run["events"]), "items": run["events"][:1]}
    assert client.get(f"/lab/runs/{run['id']}/events?offset=1&limit=1").json()["items"] == run["events"][1:2]
    trades_page = client.get(f"/lab/runs/{run['id']}/trades?offset=0&limit=1").json()
    assert trades_page["total"] == len(run["trades"])
    assert trades_page["items"] == run["trades"][:1]
    assert client.get(f"/lab/runs/{run['id']}/events?limit=101").status_code == 422
    assert client.get("/lab/runs/missing/events").status_code == 404
    later = client.post(f"/lab/strategies/{strategy['id']}/versions", json={"definition": {**definition(), "take_profit": "0.3"}}).json()
    assert later["version"] == 2
    replay = client.post("/lab/runs", json={"dataset_id": dataset["id"], "version_id": strategy["version_id"]}).json()
    for field in ("events", "trades", "equity", "drawdown", "metrics"):
        assert replay[field] == run[field]
    app2 = FastAPI()
    app2.include_router(create_lab_router(path))
    assert TestClient(app2).get(f"/lab/runs/{run['id']}").json() == run
    gap = client.post("/lab/datasets", json={"symbol": "BTC/USDT", "timeframe": "1h", "source": "fixture-gap",
                                           "candles": rows()[:2] + rows()[3:]}).json()
    assert len(gap["gaps"]) == 1
    assert client.post("/lab/runs", json={"dataset_id": gap["id"], "version_id": strategy["version_id"]}).status_code == 422
    assert client.post("/lab/strategies", json={"name": "fake", "definition": {**definition(), "entry": {"kind": "FIBONACCI", "period": 2}}}).status_code == 422
    assert client.get("/lab/datasets", headers={"Origin": "https://outside.invalid"}).status_code == 403


def test_curve_window_blocks_misaligned_persisted_series(tmp_path):
    path = tmp_path / "curve-integrity.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "curve integrity password"}).status_code == 200
    dataset = client.post("/lab/datasets", json={"symbol": "BTC/USDT", "timeframe": "1h",
        "source": "fixture", "candles": rows()}).json()
    strategy = client.post("/lab/strategies", json={"name": "Curvas", "definition": definition()}).json()
    run = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                         "version_id": strategy["version_id"]}).json()
    store = LabStore(path)
    with store.connect() as db:
        saved = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (run["id"],)).fetchone()[0])
        saved["drawdown"][1]["t"] += STEP
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (encoded(saved), run["id"]))
    response = client.get(f"/lab/runs/{run['id']}/curves?from_t={START}&to_t={START + 4 * STEP}")
    assert response.status_code == 409
    assert "inconsistentes" in response.json()["detail"]
    assert client.get(f"/lab/runs/{run['id']}/curves?from_t={START}&to_t={START + 4 * STEP}&view=plot").status_code == 409


def test_plot_window_discards_data_if_lab_is_locked_after_read(tmp_path,monkeypatch):
    from app.lab import api as lab_api

    path = tmp_path / "plot-session.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create",json={"passphrase":"isolated plot session password"}).status_code == 200
    data = client.post("/lab/datasets",json={"symbol":"BTC/USDT","timeframe":"1h",
        "source":"fixture","candles":rows()}).json()
    project = lab_api._plot_points

    def lock_after_projection(*args):
        result = project(*args)
        lab_api.lab_access.lock(path)
        return result

    monkeypatch.setattr(lab_api,"_plot_points",lock_after_projection)
    response = client.get(f"/lab/datasets/{data['id']}/candles?from_t={START}&to_t={START + 4*STEP}&view=plot")
    assert response.status_code == 409 and "sesión LAB cambió" in response.json()["detail"]
    assert "candles" not in response.json()


def test_candle_window_rejects_dataset_with_changed_contents(tmp_path):
    path = tmp_path / "candle-integrity.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "candle integrity password"}).status_code == 200
    dataset = client.post("/lab/datasets", json={"symbol": "BTC/USDT", "timeframe": "1h",
        "source": "fixture", "candles": rows()}).json()
    store = LabStore(path)
    with store.connect() as db:
        saved = json.loads(db.execute("SELECT candles_json FROM datasets WHERE id=?", (dataset["id"],)).fetchone()[0])
        saved[0]["c"] = "999"
        db.execute("UPDATE datasets SET candles_json=? WHERE id=?", (encoded(saved), dataset["id"]))
    response = client.get(f"/lab/datasets/{dataset['id']}/candles?from_t={START}&to_t={START + 4 * STEP}")
    assert response.status_code == 409
    assert "Huella del dataset incorrecta" in response.json()["detail"]


def test_run_verification_survives_reopen_and_new_strategy_version(tmp_path):
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/create", json={"passphrase": "phase two verification password"}).status_code == 200
    dataset = client.post("/lab/datasets", json={"symbol": "BTC/USDT", "timeframe": "1h",
                                                "source": "fixture", "candles": rows()}).json()
    strategy = client.post("/lab/strategies", json={"name": "EMA", "definition": definition()}).json()
    run = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                         "version_id": strategy["version_id"]}).json()
    assert len(run["strategy_hash"]) == len(run["result_hash"]) == 64
    client.post(f"/lab/strategies/{strategy['id']}/versions",
                json={"definition": {**definition(), "take_profit": "0.3"}})
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    check = TestClient(reopened).get(f"/lab/runs/{run['id']}/verify")
    assert check.status_code == 200
    assert check.json()["status"] == "verified"
    assert check.json()["checks"] == {"dataset": True, "strategy": True,
                                       "stored_result": True, "replay": True}


@pytest.mark.parametrize("encrypted", [False, True])
def test_reopened_run_uses_one_snapshot_for_result_events_and_trades(tmp_path, encrypted):
    path = tmp_path / "lab.db"
    passphrase = "isolated LAB snapshot password" if encrypted else None
    store = LabStore(path, passphrase=passphrase)
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    saved = store.save_run(dataset["id"], strategy["version_id"],
                           run_backtest(bars, strategy["definition"]), ENGINE_VERSION)
    assert saved["events"]
    original_connect = store.connect
    started = Event()
    pending = []
    writer_store = LabStore(path, passphrase=passphrase)

    def change_first_event():
        with writer_store.connect() as db:
            db.execute("UPDATE detected_events SET event_json='{}' WHERE run_id=? AND index_no=0",
                       (saved["id"],))
            started.set()
            db.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        class CompetingConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, statement, *args):
                cursor = self.db.execute(statement, *args)
                if statement.startswith("SELECT * FROM runs WHERE id=") and not pending:
                    class FirstRow:
                        def fetchone(self):
                            row = cursor.fetchone()
                            pending.append(pool.submit(change_first_event))
                            assert started.wait(timeout=2)
                            time.sleep(0.1)
                            assert not pending[0].done()
                            return row

                    return FirstRow()
                return cursor

        @contextmanager
        def competing_connect():
            with original_connect() as db:
                yield CompetingConnection(db)

        store.connect = competing_connect
        before = store.run(saved["id"])
        assert before["events"] == saved["events"]
        pending[0].result(timeout=5)
        store.connect = original_connect
        after = store.run(saved["id"])
    assert after["events"][0] == {}


@pytest.mark.parametrize("changed", ("dataset", "strategy", "result"))
def test_run_verification_detects_tampering(tmp_path, changed):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    run = store.save_run(dataset["id"], strategy["version_id"],
                         run_backtest(bars, strategy["definition"]), ENGINE_VERSION)
    with sqlite3.connect(path) as db:
        if changed == "dataset":
            db.execute("UPDATE datasets SET candles_json='[]' WHERE id=?", (dataset["id"],))
        elif changed == "strategy":
            db.execute("UPDATE strategy_versions SET definition_json=? WHERE id=?",
                       (json.dumps({**strategy["definition"], "take_profit": "0.3"}), strategy["version_id"]))
        else:
            db.execute("UPDATE detected_events SET event_json='{}' WHERE run_id=? AND index_no=0", (run["id"],))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    check = TestClient(app).get(f"/lab/runs/{run['id']}/verify").json()
    assert check["status"] == "mismatch"
    client = TestClient(app)
    assert "metrics" not in client.get("/lab/runs").json()[0]
    blocked = client.get(f"/lab/runs/{run['id']}?require_verified=true")
    assert blocked.status_code == 409 and blocked.json()["detail"]
    key = {"dataset": "dataset", "strategy": "strategy", "result": "stored_result"}[changed]
    assert check["checks"][key] is False or (changed == "dataset" and check["checks"][key] is None)


def test_run_verification_does_not_certify_old_or_unpinned_runs(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    result = run_backtest(bars, strategy["definition"])
    old = store.save_run(dataset["id"], strategy["version_id"], result, "lab-1.0")
    current = store.save_run(dataset["id"], strategy["version_id"], result, ENGINE_VERSION)
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (current["id"],)).fetchone()[0])
        saved.pop("strategy_hash")
        saved.pop("result_hash")
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(saved), current["id"]))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.get(f"/lab/runs/{old['id']}/verify").json()["status"] == "engine_unavailable"
    legacy = client.get(f"/lab/runs/{current['id']}/verify").json()
    assert legacy["status"] == "legacy_unpinned"
    assert legacy["verified"] is False and legacy["checks"]["replay"] is True


@pytest.mark.parametrize("engine_version,expected", (
    ("lab-1.5", "legacy_unpinned"),
    (ENGINE_VERSION, "mismatch"),
))
def test_missing_later_metric_is_never_certified(tmp_path, engine_version, expected):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    saved = store.save_run(dataset["id"], strategy["version_id"],
                           run_backtest(bars, strategy["definition"], engine_version=engine_version),
                           engine_version)
    with sqlite3.connect(path) as db:
        result = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (saved["id"],)).fetchone()[0])
        result["metrics"].pop("ambiguous_exits")
        result.pop("strategy_hash")
        result.pop("result_hash")
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(result), saved["id"]))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    report = client.get(f"/lab/runs/{saved['id']}/verify").json()
    assert report["status"] == expected and report["verified"] is False
    assert report["checks"]["replay"] is (expected == "legacy_unpinned")
    blocked = client.get(f"/lab/runs/{saved['id']}?require_verified=true")
    assert blocked.status_code == 409


def test_legacy_run_can_be_repeated_as_new_without_rewriting_original(tmp_path):
    from app.lab.backup import create_lab_backup

    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    original = store.save_run(dataset["id"], strategy["version_id"],
                              run_backtest(bars, strategy["definition"], engine_version="lab-1.5"),
                              "lab-1.5")
    with sqlite3.connect(path) as db:
        old = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (original["id"],)).fetchone()[0])
        old["metrics"].pop("ambiguous_exits")
        old.pop("strategy_hash")
        old.pop("result_hash")
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(old), original["id"]))
        original_bytes = db.execute("SELECT result_json FROM runs WHERE id=?", (original["id"],)).fetchone()[0]
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.get(f"/lab/runs/{original['id']}?require_verified=true").status_code == 409
    created = client.post(f"/lab/runs/{original['id']}/replay-as-new")
    assert created.status_code == 200, created.text
    new_id = created.json()["run_id"]
    assert new_id != original["id"]
    assert created.json()["replay_of_run_id"] == original["id"]
    replayed = client.get(f"/lab/runs/{new_id}?require_verified=true")
    assert replayed.status_code == 200 and replayed.json()["verified"] is True
    assert replayed.json()["replay_of_run_id"] == original["id"]
    assert replayed.json()["engine_version"] == ENGINE_VERSION
    assert client.get(f"/lab/runs/{new_id}/verify").json()["status"] == "verified"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT result_json FROM runs WHERE id=?", (original["id"],)).fetchone()[0] == original_bytes
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
    backup = create_lab_backup(store)
    assert backup["verified"]["verified_runs"] == 1


@pytest.mark.parametrize("field,replacement", (
    ("dataset_hash", "0" * 64),
    ("dataset_source", "origen alterado"),
    ("strategy_hash", "0" * 64),
))
def test_repeating_a_run_rejects_changed_original_inputs(tmp_path, field, replacement):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    original = store.save_run(dataset["id"], strategy["version_id"],
                              run_backtest(bars, strategy["definition"]), ENGINE_VERSION)
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (original["id"],)).fetchone()[0])
        saved[field] = replacement
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(saved), original["id"]))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    blocked = TestClient(app).post(f"/lab/runs/{original['id']}/replay-as-new")
    assert blocked.status_code == 409
    assert len(store.runs()) == 1


def test_save_run_rejects_inputs_changed_after_execution(tmp_path):
    store = LabStore(tmp_path / "lab.db")
    bars, gaps = validate_candles(rows(), "1h", now=START + 10 * STEP)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    strategy = store.create_strategy("EMA", validate_strategy(definition()))
    result = run_backtest(bars, strategy["definition"])
    with pytest.raises(LabError, match="Versión de estrategia cambió"):
        store.save_run(dataset["id"], strategy["version_id"], result, ENGINE_VERSION,
                       expected_strategy_hash="0" * 64)
    with pytest.raises(LabError, match="Huella del dataset cambió"):
        store.save_run(dataset["id"], strategy["version_id"], result, ENGINE_VERSION,
                       expected_dataset_hash="0" * 64)
    assert store.runs() == []


def test_binance_pagination_advances_and_excludes_end():
    class Adapter:
        known_pairs = ("BTC/USDT",)
        calls = []

        def get_candles(self, symbol, timeframe, limit, since):
            self.calls.append((since, limit))
            return rows()[int((since - START) / STEP):][:2]

    adapter = Adapter()
    output = download_binance("BTC/USDT", "1h", START, START + 5 * STEP, adapter=adapter, now=START + 10 * STEP)
    assert len(output) == 5
    assert [call[0] for call in adapter.calls] == [START, START + 2 * STEP, START + 4 * STEP]


def test_spot_download_uses_dataset_capacity_without_saving_partial_history():
    class Adapter:
        known_pairs = ("BTC/USDT",)

        def get_candles(self, symbol, timeframe, limit, since):
            return [
                {"t": since + i * STEP, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
                for i in range(limit)
            ]

    adapter = Adapter()
    output = download_binance("BTC/USDT", "1h", START, START + MAX_DATASET_CANDLES * STEP,
                              adapter=adapter, now=START + (MAX_DATASET_CANDLES + 1) * STEP)
    assert len(output) == MAX_DATASET_CANDLES
    assert output[0]["t"] == START and output[-1]["t"] == START + (MAX_DATASET_CANDLES - 1) * STEP
    with pytest.raises(LabError, match=f"superior a {MAX_DATASET_CANDLES} velas"):
        download_binance("BTC/USDT", "1h", START, START + (MAX_DATASET_CANDLES + 1) * STEP,
                         adapter=adapter, now=START + (MAX_DATASET_CANDLES + 2) * STEP)

    class IncompleteAdapter(Adapter):
        def get_candles(self, symbol, timeframe, limit, since):
            if since >= START + 1000 * STEP:
                return []
            return super().get_candles(symbol, timeframe, limit, since)

    with pytest.raises(LabError, match="histórico parcial"):
        download_binance("BTC/USDT", "1h", START, START + 5001 * STEP,
                         adapter=IncompleteAdapter(), now=START + 5002 * STEP)
