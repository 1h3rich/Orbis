"""SMA causal para LAB y para el evaluador compartido con PAPER."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import (IndicatorCache, LabError, evaluate, run_backtest, sma,
                            supports_engine_version, validate_candles, validate_strategy)
from app.lab.store import LabStore


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 3600


def candles(prices=(10, 12, 14, 10, 8, 11, 15, 16)):
    rows = [{"t": START + i * STEP, "o": str(price), "h": str(price + 1),
             "l": str(price - 1), "c": str(price), "v": "10"}
            for i, price in enumerate(prices)]
    return validate_candles(rows, "1h", now=START + 20 * STEP)[0]


def strategy(rule):
    return validate_strategy({"direction": "LONG", "initial_capital": "1000",
                              "allocation": "1", "fee_rate": "0", "slippage_rate": "0",
                              "stop_loss": "0.1", "take_profit": "0.2", "entry": rule})


def test_sma_matches_exact_window_cache_and_each_causal_prefix():
    bars = candles()
    above = strategy({"kind": "CLOSE_ABOVE_SMA", "period": 3})["entry"]
    below = strategy({"kind": "CLOSE_BELOW_SMA", "period": 3})["entry"]
    assert sma(bars, 1, 3) is None
    assert sma(bars, 2, 3) == Decimal("12")
    assert sma(bars, 3, 3) == Decimal("12")
    assert evaluate(above, bars, 2)[0] is True
    assert evaluate(below, bars, 3)[0] is True
    assert evaluate(above, bars, 3)[1]["threshold"] == "12"
    full = IndicatorCache(bars)
    for i in range(len(bars)):
        prefix = bars[:i + 1]
        assert full.value("SMA", 3, i) == sma(bars, i, 3)
        for rule in (above, below):
            assert evaluate(rule, bars, i, full) == evaluate(rule, prefix, i, IndicatorCache(prefix))
            assert evaluate(rule, bars, i) == evaluate(rule, bars, i, full)


def test_sma_rule_runs_persists_and_old_ema_engine_remains_replayable(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars = candles()
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    sma_version = store.create_strategy("SMA", strategy({"kind": "CLOSE_ABOVE_SMA", "period": 3}))
    ema_version = store.create_strategy("EMA", strategy({"kind": "CLOSE_ABOVE_EMA", "period": 3}))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    run = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                         "version_id": sma_version["version_id"]}).json()
    assert run["engine_version"] == ENGINE_VERSION == "lab-1.16"
    assert run["events"][0]["explanation"]["threshold"] == "12"
    assert client.get(f"/lab/runs/{run['id']}/verify").json()["status"] == "verified"
    old = store.save_run(dataset["id"], ema_version["version_id"],
                         run_backtest(bars, ema_version["definition"], engine_version="lab-1.5"), "lab-1.5")
    assert client.get(f"/lab/runs/{old['id']}/verify").json()["status"] == "verified"
    previous_sma = store.save_run(dataset["id"], sma_version["version_id"],
                                  run_backtest(bars, sma_version["definition"], engine_version="lab-1.6"), "lab-1.6")
    assert client.get(f"/lab/runs/{previous_sma['id']}/verify").json()["status"] == "verified"
    assert supports_engine_version("lab-1.5", ema_version["definition"])
    assert not supports_engine_version("lab-1.5", sma_version["definition"])
    assert not supports_engine_version("lab-1.4", ema_version["definition"])


@pytest.mark.parametrize("period", [1, 301, True, "20"])
def test_sma_period_validation(period):
    with pytest.raises(LabError, match="Periodo"):
        strategy({"kind": "CLOSE_ABOVE_SMA", "period": period})
