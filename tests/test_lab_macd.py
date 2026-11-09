"""Cruces MACD causales compartidos entre LAB y PAPER."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import (IndicatorCache, LabError, evaluate, macd, run_backtest,
                            supports_engine_version, validate_candles, validate_strategy)
from app.lab.store import LabStore


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
PRICES = [10, 10, 10, 10, 10, 12, 14, 16, 18, 17, 15, 13, 11, 9, 11, 13, 15, 17]


def candles(prices=PRICES):
    return validate_candles([
        {"t": START + i * 3600, "o": str(price), "h": str(price + 1),
         "l": str(price - 1), "c": str(price), "v": "10"}
        for i, price in enumerate(prices)], "1h", now=START + 100 * 3600)[0]


def strategy(kind, fast=2, slow=3, signal=2):
    return validate_strategy({"direction": "LONG", "initial_capital": "1000",
                              "allocation": "1", "fee_rate": "0", "slippage_rate": "0",
                              "stop_loss": "0.1", "take_profit": "0.2",
                              "entry": {"kind": kind, "period": fast,
                                        "slow_period": slow, "signal_period": signal}})


def test_macd_warmup_cross_direction_and_prefix_cache_parity():
    bars = candles()
    up = strategy("MACD_CROSS_ABOVE_SIGNAL")["entry"]
    down = strategy("MACD_CROSS_BELOW_SIGNAL")["entry"]
    full = IndicatorCache(bars)
    assert macd(bars, 1, 2, 3, 2) == (None, None)
    assert macd(bars, 2, 2, 3, 2) == (Decimal(0), None)
    assert macd(bars, 3, 2, 3, 2) == (Decimal(0), Decimal(0))
    assert [i for i in range(len(bars)) if evaluate(up, bars, i, full)[0]] == [5, 14]
    assert [i for i in range(len(bars)) if evaluate(down, bars, i, full)[0]] == [9]
    for i in range(len(bars)):
        prefix = bars[:i + 1]
        assert full.macd(2, 3, 2, i) == macd(prefix, i, 2, 3, 2)
        for rule in (up, down):
            assert evaluate(rule, bars, i, full) == evaluate(rule, prefix, i)
            assert evaluate(rule, bars, i) == evaluate(rule, bars, i, full)
    assert all(not evaluate(up, bars, i)[0] for i in range(5))


def test_macd_run_reopens_verifies_and_old_versions_remain_scoped(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars = candles()
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    definition = strategy("MACD_CROSS_ABOVE_SIGNAL")
    version = store.create_strategy("MACD", definition)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    response = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                              "version_id": version["version_id"]})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["engine_version"] == ENGINE_VERSION == "lab-1.16"
    assert run["events"][0]["explanation"]["slow_period"] == 3
    assert any(event["explanation"]["matched"] for event in run["events"])
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    assert TestClient(reopened).get(f"/lab/runs/{run['id']}/verify").json()["status"] == "verified"
    assert all(not supports_engine_version(old, definition) for old in ("lab-1.5", "lab-1.6", "lab-1.7"))
    old_definition = validate_strategy({**definition, "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 3}})
    old_version = store.create_strategy("EMA", old_definition)
    old_run = store.save_run(dataset["id"], old_version["version_id"],
                             run_backtest(bars, old_definition, engine_version="lab-1.7"), "lab-1.7")
    assert client.get(f"/lab/runs/{old_run['id']}/verify").json()["status"] == "verified"


@pytest.mark.parametrize("fast,slow,signal", [(3, 3, 2), (4, 3, 2), (2, 301, 2),
                                               (2, 3, 1), (2, 3, 301), (2, True, 2),
                                               (2, 3, "9")])
def test_macd_rejects_invalid_periods(fast, slow, signal):
    with pytest.raises(LabError, match="MACD"):
        strategy("MACD_CROSS_ABOVE_SIGNAL", fast, slow, signal)
