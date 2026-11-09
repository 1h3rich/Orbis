"""Owned encrypted LAB with real archive APIs and a controlled analysis provider."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab import api as lab_api
from app.lab.ai_research import COMPARISON_SCHEMA, MODEL, evidence_digest
from app.web.mount import register_ui
from tests.test_lab_research import rows, structure
from tests.test_spot_strategy_paper import definition

PASSWORD = "Owned browser AI archive password"
path = Path(os.environ["ORBIS_LAB_PATH"])
app = FastAPI()
app.include_router(lab_api.create_lab_router(path))
register_ui(app)


def analyze(evidence, question, key):
    assert key == "sk-owned-browser-fixture"
    assert evidence["environment"] == "LAB" and evidence["theoretical_only"]
    assert not ({"candles", "trades", "wallet", "portfolio"} & evidence.keys())
    if evidence["schema"] == COMPARISON_SCHEMA:
        assert [item["role"] for item in evidence["runs"]] == ["base", "alternative"]
        assert all("source" not in item["dataset"] and "strategy_name" not in item for item in evidence["runs"])
        assert evidence["comparison"]["comparable"]
        text = "Comparación de prueba: Base y Alternativa."
    else:
        text = "Interpretación de prueba de " + evidence["dataset"]["symbol"]
    return {
        "answer": text,
        "model": MODEL, "evidence_sha256": evidence_digest(evidence), "usage": {"output_tokens": 12},
    }


lab_api.analyze_research = analyze
with TestClient(app) as client:
    def result(method, url, **kwargs):
        response = client.request(method, url, **kwargs)
        assert response.is_success, response.text
        return response.json()

    result("POST", "/lab/create", json={"passphrase": PASSWORD})
    dataset = result("POST", "/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "owned-browser-fixture", "candles": rows(),
    })
    research = result("POST", "/lab/research/runs", json={"dataset_id": dataset["id"], "rule": structure()})
    strategy = result("POST", "/lab/strategies", json={"name": "Prueba LAB", "definition": definition()})
    backtest = result("POST", "/lab/runs", json={"dataset_id": dataset["id"], "version_id": strategy["version_id"]})
    alternative = result("POST", "/lab/strategies", json={
        "name": "Alternativa LAB", "definition": {**definition(), "max_holding_bars": 2},
    })
    alternative_run = result("POST", "/lab/runs", json={
        "dataset_id": dataset["id"], "version_id": alternative["version_id"],
    })
    comparison = {"left_id": backtest["id"], "right_id": alternative_run["id"]}
    records = {}
    for kind, endpoint, run in (
        ("research", "/lab/research/runs/", research), ("backtest", "/lab/runs/", backtest),
    ):
        source = endpoint + run["id"]
        preview = result("GET", source + "/ai-preview")
        question = "Límites de estructuras" if kind == "research" else "Límites del backtest"
        analysis = result("POST", source + "/ai-analyze", json={
            "question": question, "api_key": "sk-owned-browser-fixture",
            "evidence_sha256": preview["evidence_sha256"], "save_locally": True,
        })
        assert analysis["saved_locally"]
        records[kind] = {"id": analysis["analysis_id"], "run_id": run["id"], "question": question}


@app.get("/__test/state")
def state():
    return {"fixture_id": path.parent.parent.name, "records": records, "password": PASSWORD, "comparison": comparison}
