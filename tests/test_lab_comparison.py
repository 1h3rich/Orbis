"""Comparación explícita sólo sobre backtests LAB reproducibles."""

import json
import sqlite3
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.store import LabStore


START = 1735689600
STEP = 3600


def scenario(tmp_path):
    path = tmp_path / "lab.db"
    with LabStore(path).connect():  # Comparación y alteración directa del legado.
        pass
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    candles = [{"t": START + i * STEP, "o": str(100 + i), "h": str(102 + i),
                "l": str(99 + i), "c": str(101 + i), "v": "10"}
               for i in range(80)]
    dataset = client.post("/lab/datasets", json={"symbol": "BTC/USDT", "timeframe": "1h",
                                                 "source": "CSV local", "candles": candles}).json()
    definition = {"direction": "LONG", "initial_capital": "1000", "allocation": "1",
                  "fee_rate": "0.001", "slippage_rate": "0.001", "stop_loss": "0.03",
                  "take_profit": "0.08", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}
    first = client.post("/lab/strategies", json={"name": "Tendencia", "definition": definition}).json()
    second = client.post(f"/lab/strategies/{first['id']}/versions",
                         json={"definition": {**definition, "take_profit": "0.12"}}).json()
    left = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                           "version_id": first["version_id"]}).json()
    right = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                            "version_id": second["version_id"]}).json()
    return client, path, dataset, candles, first, left, right


def test_same_dataset_and_engine_compare_normalized_returns(tmp_path):
    client, _, dataset, _, first, left, right = scenario(tmp_path)
    response = client.post("/lab/runs/compare", json={"left_id": left["id"],
                                                       "right_id": right["id"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["environment"] == "LAB" and result["theoretical_only"] is True
    assert result["comparable"] is True
    assert result["left"]["dataset"]["hash"] == result["right"]["dataset"]["hash"] == dataset["hash"]
    assert result["left"]["version_id"] == first["version_id"]
    assert result["assumptions_equal"] is False
    assert Decimal(result["deltas"]["return_percentage_points"]) == (
        Decimal(right["metrics"]["return_pct"]) - Decimal(left["metrics"]["return_pct"]))
    assert result["deltas"]["trades"] == right["metrics"]["trades"] - left["metrics"]["trades"]


def test_different_dataset_has_no_performance_delta(tmp_path):
    client, _, _, candles, first, left, _ = scenario(tmp_path)
    other = [{**bar, "o": str(200 + i), "h": str(202 + i),
              "l": str(199 + i), "c": str(201 + i)} for i, bar in enumerate(candles)]
    dataset = client.post("/lab/datasets", json={"symbol": "BTC/USDT", "timeframe": "1h",
                                                 "source": "CSV local", "candles": other}).json()
    right = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                            "version_id": first["version_id"]}).json()
    response = client.post("/lab/runs/compare", json={"left_id": left["id"],
                                                       "right_id": right["id"]})
    assert response.status_code == 200
    result = response.json()
    assert result["comparable"] is False and "datasets distintos" in result["reason"]
    assert "deltas" not in result


def test_unverified_or_invalid_comparison_is_rejected(tmp_path):
    client, path, _, _, _, left, right = scenario(tmp_path)
    assert client.post("/lab/runs/compare", json={"left_id": left["id"],
                                                   "right_id": left["id"]}).status_code == 422
    assert client.post("/lab/runs/compare", json={"left_id": "missing",
                                                   "right_id": right["id"]}).status_code == 404
    assert client.post("/lab/runs/compare", json={"left_id": left["id"],
                                                   "right_id": right["id"]},
                       headers={"Origin": "https://outside.invalid"}).status_code == 403
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT result_json FROM runs WHERE id=?", (right["id"],)).fetchone()
        altered = json.loads(row[0])
        altered["metrics"]["return_pct"] = "999999"
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(altered), right["id"]))
    assert client.post("/lab/runs/compare", json={"left_id": left["id"],
                                                   "right_id": right["id"]}).status_code == 409
