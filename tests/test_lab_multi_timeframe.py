"""Las condiciones 4h derivadas de 1h nunca leen una vela aún abierta."""

from decimal import Decimal

import pytest

from app.lab.engine import (
    CURRENT_ENGINE_VERSION, IndicatorCache, LabError, evaluate, run_backtest,
    supports_engine_version, validate_candles, validate_rule, validate_rule_timeframes,
    validate_strategy,
)
from app.lab.store import LabStore
from app.automation.spot_strategy_paper import SpotStrategyPaper


HOUR = 3600
START = 1_735_689_600


def bars(*, missing: int | None = None):
    rows = []
    for index in range(12):
        if index == missing:
            continue
        close = 10 + index // 4 * 10
        rows.append({"t": START + index * HOUR, "o": str(close),
                     "h": str(close + 1), "l": str(close - 1),
                     "c": str(close), "v": "1"})
    return validate_candles(rows, "1h", now=START + 13 * HOUR)[0]


def strategy():
    return validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.9",
        "take_profit": "9", "entry": {"op": "AND", "children": [
            {"kind": "CLOSE_ABOVE_SMA", "period": 2},
            {"kind": "CLOSE_ABOVE_SMA", "period": 2, "timeframe": "4h"},
        ]},
    })


def test_higher_timeframe_waits_for_close_and_matches_prefix():
    data = bars()
    rule = validate_rule({"kind": "CLOSE_ABOVE_SMA", "period": 2,
                          "timeframe": "4h"})
    full_cache = IndicatorCache(data)
    assert [evaluate(rule, data, index, full_cache, base_timeframe="1h")[0]
            for index in range(12)] == [False] * 7 + [True] * 5
    matched, evidence = evaluate(rule, data, 7, full_cache, base_timeframe="1h")
    assert matched
    assert evidence["higher_candle_t"] == START + 4 * HOUR
    assert evidence["observed"] == "20"
    assert evidence["threshold"] == "15"
    assert evidence["base_timeframe"] == "1h"
    assert evaluate(rule, data, 6, full_cache, base_timeframe="1h")[1]["threshold"] is None
    for index in range(12):
        assert evaluate(rule, data, index, full_cache, base_timeframe="1h") == evaluate(
            rule, data[:index + 1], index, base_timeframe="1h")
    changed_future = list(data)
    changed_future[11] = changed_future[11].__class__(changed_future[11].t,
        Decimal("300"), Decimal("301"), Decimal("299"), Decimal("300"), Decimal("1"))
    assert evaluate(rule, changed_future, 7, base_timeframe="1h") == evaluate(
        rule, data, 7, base_timeframe="1h")


def test_incomplete_higher_bucket_does_not_reuse_stale_signal():
    data = bars(missing=5)
    rule = validate_rule({"kind": "CLOSE_ABOVE_SMA", "period": 2,
                          "timeframe": "4h"})
    assert evaluate(rule, data, 7, base_timeframe="1h")[0] is False
    assert evaluate(rule, data, 8, base_timeframe="1h")[1]["reason"] == "NO_COMPLETE_CANDLE"


def test_multi_timeframe_strategy_version_and_backtest_are_reproducible():
    definition = strategy()
    data = bars()
    assert CURRENT_ENGINE_VERSION == "lab-1.16"
    assert supports_engine_version("lab-1.12", definition) is False
    validate_rule_timeframes(definition, "1h")
    with pytest.raises(LabError, match="superior divisible"):
        validate_rule_timeframes(definition, "4h")
    with pytest.raises(LabError, match="Versión de motor"):
        run_backtest(data, definition, "1h", engine_version="lab-1.12")
    one = run_backtest(data, definition, "1h")
    two = run_backtest(data, definition, "1h")
    assert one == two
    assert [event["signal_time"] for event in one["events"]] == [START + 8 * HOUR]
    assert one["events"][0]["explanation"]["children"][1]["higher_candle_t"] == START + 4 * HOUR


def test_invalid_timeframes_are_rejected_before_running():
    with pytest.raises(LabError, match="Temporalidad superior"):
        validate_rule({"kind": "RSI_LT", "period": 14, "threshold": "30", "timeframe": "1y"})
    definition = strategy()
    with pytest.raises(LabError, match="superior divisible"):
        validate_rule_timeframes(definition, "4h")


def test_spot_paper_reuses_lab_higher_timeframe_evidence(tmp_path):
    state = {"now": START + 8 * HOUR + 10}
    rows = [bar.json() for bar in bars()]

    class Source:
        def get_candles(self, symbol, timeframe="1h", limit=3, since=None, before=None):
            return [row.copy() for row in rows if row["t"] >= since][:limit]

        def get_paper_quote(self, symbol):
            return {"symbol": symbol, "source": "mexc-spot-public",
                    "event_time_ns": state["now"] * 1_000_000_000,
                    "bid": "29.9", "ask": "30", "last": "29.9"}

    lab_path = tmp_path / "lab.db"
    lab = LabStore(lab_path)
    initial, gaps = validate_candles(rows[:8], "1h", now=state["now"])
    dataset = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", initial,
                               gaps, verified_public=True)
    definition = strategy()
    definition["entry"]["children"][0] = {"kind": "RSI_GT", "period": 2,
                                             "threshold": "30"}
    version = lab.create_strategy("1h + 4h", definition)
    paper = SpotStrategyPaper(tmp_path / "spot.db", lab_path, adapter_factory=Source,
                              clock_ns=lambda: state["now"] * 1_000_000_000)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 9 * HOUR + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    event = observed["events"][-1]
    assert event["entry_match"] is True
    expected = evaluate(definition["entry"], bars(), 8, IndicatorCache(bars()),
                        base_timeframe="1h")
    assert event["entry"] == expected[1]
    assert event["entry"]["children"][1]["higher_candle_t"] == START + 4 * HOUR
