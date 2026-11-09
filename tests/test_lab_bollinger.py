"""Bollinger causal: fórmula poblacional, caché, reglas y persistencia."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import (IndicatorCache, LabError, bollinger, evaluate,
                            supports_engine_version, validate_candles, validate_strategy)
from app.lab.store import LabStore


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 3600


def bars(prices):
    return validate_candles([
        {"t": START + i * STEP, "o": str(price), "h": str(price + 1),
         "l": str(price - 1), "c": str(price), "v": "10"}
        for i, price in enumerate(prices)], "1h", now=START + 50 * STEP)[0]


def strategy(rule):
    return validate_strategy({"direction": "LONG", "initial_capital": "1000",
                              "allocation": "1", "fee_rate": "0", "slippage_rate": "0",
                              "stop_loss": "0.1", "take_profit": "0.2", "entry": rule})


def test_bollinger_uses_population_std_current_closed_bar_and_multiplier():
    rise = bars([10, 10, 20, 20, 10, 10, 20, 20])
    upper = strategy({"kind": "CLOSE_ABOVE_BB_UPPER", "period": 3, "multiplier": "1"})["entry"]
    assert bollinger(rise, 1, 3, Decimal(1)) is None
    top, bottom = bollinger(rise, 2, 3, Decimal(1))
    assert abs(top - Decimal("18.0473785412")) < Decimal("0.000000001")
    assert abs(bottom - Decimal("8.6192881254")) < Decimal("0.000000001")
    assert evaluate(upper, rise, 1)[0] is False
    assert evaluate(upper, rise, 2)[0] is True
    assert evaluate(strategy({"kind": "CLOSE_ABOVE_BB_UPPER", "period": 3,
                              "multiplier": "2"})["entry"], rise, 2)[0] is False
    drop = bars([20, 20, 10, 10, 20])
    lower = strategy({"kind": "CLOSE_BELOW_BB_LOWER", "period": 3, "multiplier": "1"})["entry"]
    assert evaluate(lower, drop, 2)[0] is True
    flat = bars([10, 10, 10, 10])
    assert bollinger(flat, 2, 3, Decimal(2)) == (Decimal(10), Decimal(10))
    assert evaluate(upper, flat, 2)[0] is False


def test_bollinger_cache_matches_direct_on_every_prefix():
    data = bars([10, 10, 20, 15, 7, 17, 8, 11, 21])
    rules = [strategy({"kind": kind, "period": 3, "multiplier": multiplier})["entry"]
             for kind in ("CLOSE_ABOVE_BB_UPPER", "CLOSE_BELOW_BB_LOWER")
             for multiplier in ("1", "2.5")]
    full = IndicatorCache(data)
    for i in range(len(data)):
        prefix = data[:i + 1]
        for rule in rules:
            direct = evaluate(rule, data, i)
            assert evaluate(rule, data, i, full) == direct
            assert evaluate(rule, prefix, i, IndicatorCache(prefix)) == direct


def test_bollinger_strategy_persists_replays_and_old_engines_are_scoped(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    data = bars([10, 10, 20, 20, 10, 10, 20, 20])
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", data, [])
    version = store.create_strategy("BB", strategy({"kind": "CLOSE_ABOVE_BB_UPPER",
                                                     "period": 3, "multiplier": "1"}))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    response = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                               "version_id": version["version_id"]})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["engine_version"] == ENGINE_VERSION == "lab-1.16"
    assert run["events"][0]["explanation"]["multiplier"] == "1"
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    assert TestClient(reopened).get(f"/lab/runs/{run['id']}/verify").json()["status"] == "verified"
    assert not supports_engine_version("lab-1.6", version["definition"])
    assert not supports_engine_version("lab-1.5", version["definition"])


@pytest.mark.parametrize("multiplier", ["0", "10.1", "NaN", 2, None])
def test_bollinger_rejects_invalid_multipliers(multiplier):
    with pytest.raises(LabError):
        strategy({"kind": "CLOSE_ABOVE_BB_UPPER", "period": 20, "multiplier": multiplier})
