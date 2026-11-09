"""Exportación local de evidencia LAB, separada de patrimonio y ejecución."""

from app.lab.schema import CURRENT_SCHEMA_VERSION
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.store import LabStore
from app.lab import api as lab_api


START = 1735689600
STEP = 3600


def _client(tmp_path):
    app = FastAPI()
    path = tmp_path / "lab.db"
    with LabStore(path).connect():  # Exportación y pruebas de corrupción SQLite legado.
        pass
    app.include_router(create_lab_router(path))
    return TestClient(app), path


def _run(client, count=8):
    candles = [
        {"t": START + i * STEP, "o": str(10 + i), "h": str(11 + i),
         "l": str(9 + i), "c": str(10 + i), "v": "10"}
        for i in range(count)
    ]
    dataset = client.post("/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "CSV local",
        "candles": candles,
    }).json()
    strategy = client.post("/lab/strategies", json={
        "name": "EMA", "definition": {
            "direction": "LONG", "initial_capital": "1000", "allocation": "1",
            "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
            "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2},
        },
    }).json()
    response = client.post("/lab/runs", json={
        "dataset_id": dataset["id"], "version_id": strategy["version_id"],
    })
    assert response.status_code == 200, response.text
    return response.json(), candles


def test_export_verified_lab_run_separates_features_from_future_labels(tmp_path):
    client, _ = _client(tmp_path)
    run, candles = _run(client)
    response = client.get(f"/lab/runs/{run['id']}/export")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    bundle = response.json()
    assert bundle["schema"] == "orbis.lab.evidence.v2"
    assert bundle["environment"] == "LAB" and bundle["theoretical_only"] is True
    assert bundle["verification"]["verified"] is True
    assert bundle["dataset"]["hash"] == run["dataset_hash"]
    assert bundle["strategy"]["hash"] == run["strategy_hash"]
    assert bundle["run"]["result_hash"] == run["result_hash"]
    assert bundle["candles"] == candles
    assert len(bundle["signals"]) == len(run["events"]) > 0
    first = bundle["signals"][0]
    assert set(first) == {"features_at_close", "future_labels", "simulation"}
    assert first["features_at_close"]["signal_time"] == run["events"][0]["signal_time"]
    assert "outcomes_pct" not in first["features_at_close"]
    assert "conditions" not in first["future_labels"]
    assert first["future_labels"]["full_7d_followup"] is False
    assert first["future_labels"]["mfe_pct"] is None
    assert first["future_labels"]["time_to_favorable_seconds"] is None
    assert bundle["trades"] == run["trades"]
    assert "portfolio" not in bundle and "wallet" not in bundle
    legacy = client.get(f"/lab/runs/{run['id']}/export?schema_version=v1")
    assert legacy.status_code == 200
    assert legacy.json()["schema"] == "orbis.lab.evidence.v1"
    assert "time_to_max_seconds" not in legacy.json()["signals"][0]["future_labels"]
    assert legacy.json()["trades"] == bundle["trades"]
    split_export = client.get(f"/lab/runs/{run['id']}/export?schema_version=v3")
    assert split_export.status_code == 200, split_export.text
    split_bundle = split_export.json()
    assert split_bundle["schema"] == "orbis.lab.evidence.v3"
    assert split_bundle["candles"] == candles
    assert split_bundle["split"]["method"] == "chronological_70_30_purged_7d_v1"
    assert sum(split_bundle["split"]["counts"].values()) == len(split_bundle["signals"])
    assert all("partition" in signal for signal in split_bundle["signals"])
    for phase in ("development", "validation"):
        assert set(split_bundle["split"][f"{phase}_nonoverlap_indices"]).issubset(
            split_bundle["split"][f"{phase}_indices"])
    assert "partition" not in bundle["signals"][0]
    assert client.get(f"/lab/runs/{run['id']}/export?schema_version=v4").status_code == 422


def test_strategy_bundle_roundtrip_creates_independent_version(tmp_path):
    client, path = _client(tmp_path)
    run, _ = _run(client)
    original = client.get("/lab/strategies").json()[0]
    export_path = f"/lab/strategies/{original['id']}/versions/{original['version_id']}/export"
    exported = client.get(export_path)
    assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
    bundle = exported.json()
    assert bundle["schema"] == "orbis.lab.strategy.v1"
    assert bundle["environment"] == "LAB" and bundle["theoretical_only"] is True
    assert bundle["strategy"]["definition"] == original["definition"]
    assert "wallet" not in bundle and "credentials" not in bundle

    imported = client.post("/lab/strategies/import", json=bundle)
    assert imported.status_code == 200, imported.text
    clone = imported.json()
    assert clone["id"] != original["id"] and clone["version_id"] != original["version_id"]
    assert clone["version"] == 1 and clone["definition"] == original["definition"]
    assert clone["imported_from"]["version_id"] == original["version_id"]
    assert len(client.get("/lab/strategies").json()) == 2
    changed = dict(original["definition"])
    changed["take_profit"] = "0.3"
    assert client.post(f"/lab/strategies/{original['id']}/versions",
                       json={"definition": changed}).status_code == 200
    assert client.get(f"/lab/runs/{run['id']}").json()["version_id"] == original["version_id"]
    assert client.get("/lab/strategies").json()[0]["definition"] == clone["definition"]
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    persisted = TestClient(reopened).get("/lab/strategies").json()
    copied = next(row for row in persisted if row["id"] == clone["id"])
    assert copied["imported_from"] == clone["imported_from"]
    assert copied["definition"] == clone["definition"]


def test_strategy_import_rejects_mismatched_or_untrusted_package(tmp_path):
    client, path = _client(tmp_path)
    _run(client)
    original = client.get("/lab/strategies").json()[0]
    url = f"/lab/strategies/{original['id']}/versions/{original['version_id']}/export"
    bundle = client.get(url).json()
    assert client.get(url.replace(original["id"], "other")).status_code == 404
    assert client.get(url.replace(original["version_id"], "other")).status_code == 404
    for bad in ({**bundle, "environment": "REAL"},
                {**bundle, "schema": "orbis.lab.strategy.v2"},
                {**bundle, "trade_key": "secret"},
                {**bundle, "definition_hash": "0" * 64},
                {**bundle, "strategy": {**bundle["strategy"], "definition": {
                    **bundle["strategy"]["definition"], "take_profit": "0.9"}}}):
        assert client.post("/lab/strategies/import", json=bad).status_code == 422
    assert len(client.get("/lab/strategies").json()) == 1
    assert client.post("/lab/strategies/import", json=bundle,
                       headers={"Origin": "https://outside.invalid"}).status_code == 403
    with sqlite3.connect(path) as db:
        db.execute("UPDATE strategy_versions SET definition_json='{}' WHERE id=?",
                   (original["version_id"],))
    assert client.get(url).status_code == 409


def test_lab_v5_migration_adds_import_lineage_without_losing_runs(tmp_path):
    client, path = _client(tmp_path)
    run, _ = _run(client)
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
        db.execute("PRAGMA user_version=5")
    reopened = FastAPI()
    reopened.include_router(create_lab_router(path))
    new_client = TestClient(reopened)
    assert new_client.get(f"/lab/runs/{run['id']}").json()["id"] == run["id"]
    assert new_client.get("/lab/strategies").json()[0]["imported_from"] is None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert db.execute("SELECT count(*) FROM strategy_imports").fetchone()[0] == 0


def test_export_refuses_tampered_or_unknown_run(tmp_path):
    client, path = _client(tmp_path)
    run, _ = _run(client)
    assert client.get("/lab/runs/no-existe/export").status_code == 404
    with sqlite3.connect(path) as db:
        db.execute("UPDATE detected_events SET event_json='{}' WHERE run_id=? AND index_no=0", (run["id"],))
    response = client.get(f"/lab/runs/{run['id']}/export")
    assert response.status_code == 409
    assert "no se exporta" in response.text


def test_export_only_labels_full_week_extrema(tmp_path):
    client, _ = _client(tmp_path)
    run, _ = _run(client, count=180)
    bundle = client.get(f"/lab/runs/{run['id']}/export").json()
    full = [signal for signal in bundle["signals"] if signal["future_labels"]["full_7d_followup"]]
    partial = [signal for signal in bundle["signals"] if not signal["future_labels"]["full_7d_followup"]]
    assert full and partial
    assert full[0]["future_labels"]["mfe_pct"] is not None
    assert full[0]["future_labels"]["time_to_max_seconds"] == (
        full[0]["future_labels"]["max_at"] - full[0]["features_at_close"]["signal_time"])
    assert full[0]["future_labels"]["time_to_min_seconds"] == (
        full[0]["future_labels"]["min_at"] - full[0]["features_at_close"]["signal_time"])
    assert full[0]["future_labels"]["time_to_favorable_seconds"] == (
        full[0]["future_labels"]["favorable_at"] - full[0]["features_at_close"]["signal_time"])
    assert full[0]["future_labels"]["time_to_adverse_seconds"] == (
        full[0]["future_labels"]["adverse_at"] - full[0]["features_at_close"]["signal_time"])
    assert all(signal["future_labels"]["mfe_pct"] is None for signal in partial)
    assert all(signal["future_labels"]["max_at"] is None for signal in partial)
    assert all(signal["future_labels"]["time_to_max_seconds"] is None and
               signal["future_labels"]["time_to_min_seconds"] is None and
               signal["future_labels"]["time_to_favorable_seconds"] is None and
               signal["future_labels"]["time_to_adverse_seconds"] is None
               for signal in partial)


def test_export_remains_local_only(tmp_path):
    client, _ = _client(tmp_path)
    run, _ = _run(client)
    response = client.get(f"/lab/runs/{run['id']}/export",
                          headers={"Origin": "https://outside.invalid"})
    assert response.status_code == 403


def test_export_rechecks_strategy_if_it_changes_after_replay(tmp_path, monkeypatch):
    client, path = _client(tmp_path)
    run, _ = _run(client)
    original = lab_api.verify_backtest_run

    def alter_after_verification(store, saved):
        result = original(store, saved)
        assert result["verified"] is True
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT definition_json FROM strategy_versions WHERE id=?",
                             (run["version_id"],)).fetchone()
            changed = json.loads(row[0])
            changed["take_profit"] = "0.3"
            db.execute("UPDATE strategy_versions SET definition_json=? WHERE id=?",
                       (json.dumps(changed), run["version_id"]))
        return result

    monkeypatch.setattr(lab_api, "verify_backtest_run", alter_after_verification)
    response = client.get(f"/lab/runs/{run['id']}/export")
    assert response.status_code == 409
    assert "cambiaron" in response.text
