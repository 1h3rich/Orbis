"""Reservas sucesivas disjuntas de una versión fija, sin ajuste retrospectivo."""

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import LabError, validate_candles, validate_strategy
from app.lab.store import LabStore
from app.lab.validation import ROLLING_ENGINE_VERSION, rolling_fixed_validation


START, STEP = 1_735_689_600, 3600


def history(count=122):
    rows = [{"t": START + i * STEP, "o": str(100 + i), "h": str(102 + i),
             "l": str(99 + i), "c": str(101 + i), "v": str(10 + i)}
            for i in range(count)]
    return validate_candles(rows, "1h", now=START + (count + 2) * STEP)[0]


def strategy():
    return validate_strategy({"direction": "LONG", "initial_capital": "1000",
                              "allocation": "0.2", "fee_rate": "0.001", "slippage_rate": "0.001",
                              "stop_loss": "0.03", "take_profit": "0.08",
                              "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 10}})


def test_rolling_windows_are_disjoint_causal_and_reproducible():
    bars = history()
    result = rolling_fixed_validation(bars, strategy(), "1h", 3)
    assert result["method"] == "ROLLING_FIXED_VERSION"
    assert result["train_bars"] == 30 and result["test_bars"] == 92
    assert [(w["train_bars"], w["test_bars"]) for w in result["windows"]] == [
        (30, 30), (60, 30), (90, 32)]
    assert [w["test_start_t"] for w in result["windows"]] == [bars[i].t for i in (30, 60, 90)]
    assert [w["test_end_t"] for w in result["windows"]] == [bars[i].t for i in (59, 89, 121)]
    for window in result["windows"]:
        assert len(window["out_of_sample"]["equity"]) == window["test_bars"]
        assert all(event["signal_time"] >= window["test_start_t"]
                   for event in window["out_of_sample"]["events"])
        assert all(trade["entry_time"] > window["test_start_t"]
                   for trade in window["out_of_sample"]["trades"])
    changed = list(bars)
    last = bars[-1]
    changed[-1] = type(last)(last.t, last.o, last.o, last.o / 2, last.o / 2, last.v)
    rerun = rolling_fixed_validation(changed, strategy(), "1h", 3)
    assert rerun["windows"][:2] == result["windows"][:2]
    assert rerun["windows"][2] != result["windows"][2]
    assert result == rolling_fixed_validation(bars, strategy(), "1h", 3)
    assert any("no se ha reoptimizado" in warning for warning in result["warnings"])


@pytest.mark.parametrize("folds,count", [(1, 120), (6, 210), (True, 120), (3, 119)])
def test_rolling_rejects_invalid_or_too_short_windows(folds, count):
    with pytest.raises(LabError):
        rolling_fixed_validation(history(count), strategy(), "1h", folds)


def test_rolling_api_persists_reopens_verifies_and_blocks_tampering(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", history(), [])
    version = store.create_strategy("EMA", strategy())
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    body = {"dataset_id": dataset["id"], "version_id": version["version_id"], "folds": 3}
    response = client.post("/lab/rolling-validations", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["engine_version"] == ROLLING_ENGINE_VERSION
    assert saved["lab_engine_version"] == ENGINE_VERSION
    assert len(saved["windows"]) == 3
    assert client.get("/lab/validations").json()[0]["folds"] == 3
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    other = TestClient(reopened)
    uri = "/lab/validations/" + saved["id"]
    assert other.get(uri).json() == saved
    assert other.get(uri + "/verify").json()["status"] == "verified"
    assert client.post("/lab/rolling-validations", json={**body, "folds": 1}).status_code == 422
    with sqlite3.connect(path) as db:
        result = json.loads(db.execute("SELECT result_json FROM validation_runs WHERE id=?",
                                       (saved["id"],)).fetchone()[0])
        result["windows"][0]["out_of_sample"]["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE validation_runs SET result_json=? WHERE id=?", (json.dumps(result), saved["id"]))
    check = other.get(uri + "/verify").json()
    assert check["status"] == "mismatch" and check["checks"]["stored_result"] is False
