"""Reserva cronológica LAB: causalidad, muestras y persistencia."""

from app.lab.schema import CURRENT_SCHEMA_VERSION
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import LabError, validate_candles, validate_strategy
from app.lab.reserve_reuse import reserve_reuse, tested_intervals as _tested_intervals
from app.lab.store import LabStore
from app.lab.validation import chronological_holdout


START, STEP = 1_735_689_600, 3600


def history(count=100):
    raw = [{"t": START + i * STEP, "o": str(100 + i), "h": str(102 + i),
            "l": str(99 + i), "c": str(101 + i), "v": str(10 + i)}
           for i in range(count)]
    return validate_candles(raw, "1h", now=START + (count + 2) * STEP)[0]


def strategy():
    return validate_strategy({"direction": "LONG", "initial_capital": "1000",
        "allocation": "0.2", "fee_rate": "0.001", "slippage_rate": "0.001",
        "stop_loss": "0.03", "take_profit": "0.08",
        "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 10}})


def test_holdout_excludes_training_trades_but_warms_indicators_causally():
    bars = history()
    result = chronological_holdout(bars, strategy(), "1h", "0.7")
    assert result["split_index"] == 70
    assert result["train_end_t"] < result["test_start_t"]
    assert len(result["train"]["equity"]) == 70
    assert len(result["out_of_sample"]["equity"]) == 30
    assert result["out_of_sample"]["equity"][0]["t"] == bars[70].t
    assert all(event["signal_time"] >= bars[70].t for event in result["out_of_sample"]["events"])
    assert all(trade["entry_time"] > bars[70].t for trade in result["out_of_sample"]["trades"])
    assert result["out_of_sample"]["equity"][0]["equity"] == strategy()["initial_capital"]
    assert any("mirando todo" in warning for warning in result["warnings"])

    changed = list(bars)
    last = bars[-1]
    changed[-1] = type(last)(last.t, last.o, last.o, last.o / 2, last.o / 2, last.v)
    rerun = chronological_holdout(changed, strategy(), "1h", "0.7")
    assert rerun["train"] == result["train"]
    assert rerun["out_of_sample"]["metrics"] != result["out_of_sample"]["metrics"]


def test_holdout_rejects_too_little_history_or_bad_split():
    with pytest.raises(LabError, match="60 velas"):
        chronological_holdout(history(59), strategy(), "1h")
    with pytest.raises(LabError, match="fracción"):
        chronological_holdout(history(), strategy(), "1h", "0.9")
    with pytest.raises(LabError, match="fuera de rango"):
        chronological_holdout(history(), strategy(), "1h", "NaN")


def test_api_validation_persists_and_v3_migration_keeps_existing_data(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    bars = history()
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    version = store.create_strategy("EMA", strategy())
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE perp_optimization_variants")
        db.execute("DROP TABLE perp_optimizations")
        db.execute("DROP TABLE perp_rule_observations")
        db.execute("DROP TABLE perp_validations")
        db.execute("DROP TABLE perp_runs")
        db.execute("DROP TABLE perp_profiles")
        db.execute("DROP TABLE perp_datasets")
        db.execute("DROP TABLE ai_analyses")
        db.execute("DROP TABLE paper_gap_reports")
        db.execute("DROP TABLE dataset_refreshes")
        db.execute("DROP TABLE strategy_imports")
        db.execute("DROP TABLE optimization_runs")
        db.execute("DROP TABLE validation_runs")
        db.execute("PRAGMA user_version=3")
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    response = client.post("/lab/validations", json={"dataset_id": dataset["id"],
                                                     "version_id": version["version_id"],
                                                     "train_fraction": "0.7"})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["dataset_hash"] == dataset["hash"]
    assert saved["engine_version"] == "holdout-1.0"
    assert saved["lab_engine_version"] == ENGINE_VERSION
    assert len(saved["strategy_hash"]) == len(saved["result_hash"]) == 64
    assert saved["train"]["metrics"]["trades"] >= 1
    assert saved["out_of_sample"]["metrics"]["trades"] >= 1
    assert len(client.get("/lab/validations").json()) == 1
    assert client.get("/lab/validations/" + saved["id"]).json() == saved
    check = client.get("/lab/validations/" + saved["id"] + "/verify").json()
    assert check["status"] == "verified" and all(check["checks"].values())
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    assert TestClient(reopened).get("/lab/validations/" + saved["id"] + "/verify").json()["status"] == "verified"
    assert LabStore(path).dataset(dataset["id"])[0]["hash"] == dataset["hash"]
    assert LabStore(path).version(version["version_id"])["id"] == version["version_id"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert client.get("/lab/validations", headers={"Origin":"https://other.invalid"}).status_code == 403


def test_saved_reserve_reuse_is_visible_across_methods_and_restart(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", history(), [])
    other = store.save_dataset("ETH/USDT", "1h", "fixture", history(), [])
    version = store.create_strategy("EMA", strategy())
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    body = {"dataset_id": dataset["id"], "version_id": version["version_id"],
            "train_fraction": "0.7"}
    first = client.post("/lab/validations", json=body).json()
    assert first["reserve_reuse"]["status"] == "NO_SHARED_SAVED_RESERVE"
    second = client.post("/lab/validations", json=body).json()
    assert second["reserve_reuse"]["status"] == "SHARED_WITH_SAVED_EXPERIMENTS"
    assert second["reserve_reuse"]["overlapping_run_count"] == 1
    rolling = client.post("/lab/rolling-validations", json={
        "dataset_id": dataset["id"], "version_id": version["version_id"], "folds": 2,
    }).json()
    assert rolling["reserve_reuse"]["overlapping_run_count"] == 2
    independent = client.post("/lab/validations", json={**body, "dataset_id": other["id"]}).json()
    assert independent["reserve_reuse"]["status"] == "NO_SHARED_SAVED_RESERVE"
    longer = store.save_dataset("BTC/USDT", "1h", "fixture", history(200), [])
    large_validation = client.post("/lab/validations", json={
        **body, "dataset_id": longer["id"],
    }).json()
    optimized_response = client.post("/lab/optimizations", json={
        "dataset_id": longer["id"], "version_id": version["version_id"],
        "stop_losses": ["0.02", "0.04"], "take_profits": ["0.05", "0.1"],
        "train_fraction": "0.7",
    })
    assert optimized_response.status_code == 200, optimized_response.text
    optimized = optimized_response.json()
    assert optimized["selected"] is not None
    assert optimized["reserve_reuse"]["status"] == "SHARED_WITH_SAVED_EXPERIMENTS"
    assert optimized["reserve_reuse"]["overlapping_runs"] == [
        {"id": large_validation["id"], "kind": "validation"}]
    assert client.get("/lab/optimizations/" + optimized["id"]).json() == optimized
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    after_restart = TestClient(reopened)
    assert after_restart.get("/lab/validations/" + first["id"]).json()["reserve_reuse"]["overlapping_run_count"] == 2
    assert after_restart.get("/lab/validations/" + first["id"] + "/verify").json()["status"] == "verified"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE validation_runs SET result_json=? WHERE id=?",
                   ('{"method":"CHRONOLOGICAL_HOLDOUT","test_start_t":1,"test_end_t":2}', second["id"]))
    assert after_restart.get("/lab/validations/" + first["id"]).json()["reserve_reuse"]["status"] == "ARCHIVE_UNVERIFIABLE"


def test_reserve_reuse_does_not_count_unselected_or_malformed_windows():
    unevaluated = {"method": "GRID_STOP_TARGET_HOLDOUT", "out_of_sample": None,
                   "test_start_t": 10, "test_end_t": 20}
    assert _tested_intervals(unevaluated) == []
    assert reserve_reuse(unevaluated, [])["status"] == "NO_RESERVE_EVALUATED"
    current = {"method": "CHRONOLOGICAL_HOLDOUT", "test_start_t": 10, "test_end_t": 20}
    assert reserve_reuse(current, [{"id": "other", "kind": "optimization", "result": unevaluated}])[
        "status"] == "NO_SHARED_SAVED_RESERVE"
    malformed = {"method": "ROLLING_FIXED_VERSION", "windows": [None]}
    assert reserve_reuse(current, [{"id": "bad", "kind": "validation", "result": malformed}])[
        "status"] == "ARCHIVE_UNVERIFIABLE"


def test_walk_forward_final_reserve_is_in_overlap_audit_only_if_evaluated():
    report = {"method": "WALK_FORWARD_GRID_STOP_TARGET",
              "windows": [{"test_start_t": 10, "test_end_t": 20, "out_of_sample": {}}],
              "final_holdout": {"start_t": 30, "end_t": 40, "out_of_sample": {}}}
    assert _tested_intervals(report) == [(10, 20), (30, 40)]
    peer = {"id": "final", "kind": "validation", "result": {
        "method": "CHRONOLOGICAL_HOLDOUT", "test_start_t": 35, "test_end_t": 45}}
    assert reserve_reuse(report, [peer])["overlapping_runs"] == [{"id": "final", "kind": "validation"}]
    report["final_holdout"]["out_of_sample"] = None
    assert _tested_intervals(report) == [(10, 20)]
    report["final_holdout"] = "corrupt"
    assert reserve_reuse(report, [])['status'] == "ARCHIVE_UNVERIFIABLE"


def test_validation_verifier_distinguishes_tamper_legacy_and_unavailable_engine(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", history(), [])
    version = store.create_strategy("EMA", strategy())
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    body = {"dataset_id": dataset["id"], "version_id": version["version_id"],
            "train_fraction": "0.7"}
    saved = client.post("/lab/validations", json=body).json()
    uri = f"/lab/validations/{saved['id']}/verify"
    assert client.get(uri).json()["status"] == "verified"
    store.add_version(version["id"], validate_strategy({**strategy(), "stop_loss": "0.04"}))
    assert client.get(uri).json()["status"] == "verified"

    with sqlite3.connect(path) as db:
        result = json.loads(db.execute("SELECT result_json FROM validation_runs WHERE id=?",
                                       (saved["id"],)).fetchone()[0])
        result["train"]["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE validation_runs SET result_json=? WHERE id=?",
                   (json.dumps(result), saved["id"]))
    changed = client.get(uri).json()
    assert changed["status"] == "mismatch" and changed["checks"]["stored_result"] is False

    with sqlite3.connect(path) as db:
        original = {key: value for key, value in saved.items() if key not in (
            "id", "dataset_id", "version_id", "dataset_hash", "created_at", "engine_version", "risk")}
        original.pop("strategy_hash")
        original.pop("result_hash")
        original.pop("lab_engine_version")
        db.execute("UPDATE validation_runs SET result_json=? WHERE id=?",
                   (json.dumps(original), saved["id"]))
    assert client.get(uri).json()["status"] == "legacy_unpinned"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE validation_runs SET engine_version='holdout-0.1' WHERE id=?", (saved["id"],))
    assert client.get(uri).json()["status"] == "engine_unavailable"
