"""Distingue falta de calentamiento de una condición que no se cumple."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.lab.api import create_lab_router
from app.lab.coverage import higher_timeframe_coverage, validate_paper_timeframe_capacity
from app.lab.engine import LabError, validate_candles, validate_strategy
from app.lab.store import LabStore


START = 1_735_689_600
HOUR = 3600


def bars(count=16):
    rows = []
    for index in range(count):
        close = 10 + index // 4 * 10
        rows.append({"t": START + index * HOUR, "o": str(close),
                     "h": str(close + 1), "l": str(close - 1),
                     "c": str(close), "v": "1"})
    return validate_candles(rows, "1h", now=START + (count + 1) * HOUR)[0]


def strategy(period):
    return validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.9",
        "take_profit": "9", "entry": {
            "kind": "CLOSE_ABOVE_EMA", "period": period, "timeframe": "4h"},
    })


def test_coverage_reports_exact_complete_bars_and_warmup():
    data = bars()
    absent = higher_timeframe_coverage(data, strategy(200), "1h")
    assert absent["insufficient_rules"] == 1
    assert absent["rules"] == [{
        "path": "entry", "kind": "CLOSE_ABOVE_EMA", "period": 200,
        "timeframe": "4h", "complete_bars": 4, "minimum_bars": 200,
        "status": "INSUFFICIENT", "first_evaluable_at": None,
    }]
    enough = higher_timeframe_coverage(data, strategy(2), "1h")
    assert enough["insufficient_rules"] == 0
    assert enough["rules"][0]["status"] == "READY_TO_EVALUATE"
    assert enough["rules"][0]["first_evaluable_at"] == START + 8 * HOUR


def test_coverage_does_not_claim_complex_structure_is_ready():
    definition = strategy(2)
    definition["entry"] = {"kind": "RETEST_ABOVE_RANGE", "period": 2,
                           "retest_bars": 2, "tolerance_pct": "0.01", "timeframe": "4h"}
    result = higher_timeframe_coverage(bars(), definition, "1h")
    assert result["rules"][0]["status"] == "COVERAGE_ONLY"
    assert result["rules"][0]["minimum_bars"] is None


def test_coverage_lists_nested_entry_and_exit_independently():
    definition = strategy(2)
    definition["entry"] = {"op": "AND", "children": [
        {"kind": "RSI_GT", "period": 2, "threshold": "30", "timeframe": "4h"},
        {"kind": "CLOSE_ABOVE_EMA", "period": 200, "timeframe": "4h"},
    ]}
    definition["exit"] = {"kind": "MACD_CROSS_BELOW_SIGNAL", "period": 2,
                          "slow_period": 3, "signal_period": 2, "timeframe": "4h"}
    result = higher_timeframe_coverage(bars(), definition, "1h")
    assert [row["path"] for row in result["rules"]] == [
        "entry.children[0]", "entry.children[1]", "exit"]
    assert [row["minimum_bars"] for row in result["rules"]] == [3, 200, 5]
    assert result["insufficient_rules"] == 2


def test_paper_capacity_rejects_indicator_that_cannot_warm_up_before_5000_bars():
    definition = strategy(300)
    definition["entry"]["timeframe"] = "1d"
    with pytest.raises(LabError, match=r"entry: necesita 300 velas 1d completas.*5000 velas 1h"):
        validate_paper_timeframe_capacity(definition, "1h", START)
    assert validate_paper_timeframe_capacity(strategy(200), "1h", START) is None


def test_paper_capacity_counts_utc_alignment_and_nested_exit():
    definition = strategy(2)
    definition["entry"] = {"kind": "BEARISH_STREAK", "period": 2}
    definition["exit"] = {"op": "OR", "children": [
        {"kind": "CLOSE_ABOVE_EMA", "period": 2, "timeframe": "4h"},
        {"kind": "BEARISH_STREAK", "period": 2},
    ]}
    assert validate_paper_timeframe_capacity(definition, "1h", 0, max_base_bars=8) is None
    with pytest.raises(LabError, match=r"exit.children\[0\]: necesita 2 velas 4h completas"):
        validate_paper_timeframe_capacity(definition, "1h", HOUR, max_base_bars=8)


def test_verified_run_exposes_coverage_without_changing_saved_result(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    data = bars()
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", data, [])
    version = store.create_strategy("EMA 4h sin calentamiento", strategy(200))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    created = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                            "version_id": version["version_id"]})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    original = store.run(run_id)
    assert original["metrics"]["sample_size"] == 0
    assert "coverage" not in original
    verified = client.get(f"/lab/runs/{run_id}/verify").json()
    assert verified["verified"] is True
    assert verified["coverage"]["rules"][0]["status"] == "INSUFFICIENT"
    assert store.run(run_id) == original
    assert client.get(f"/lab/runs/{run_id}").json()["result_hash"] == original["result_hash"]
