"""Copias LAB: datos exactos, aislamiento local y rechazo de corrupción."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.backup import create_lab_backup, lab_runtime_lease, restore_lab_backup, verify_lab_backup
from app.lab.engine import LabError
from app.lab.store import LabStore


def prepared_lab(tmp_path):
    path = tmp_path / "lab.db"
    with LabStore(path).connect():  # Copia/migración de LAB heredado sin cifrar.
        pass
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    candles = [{"t": 1735689600 + i * 3600, "o": str(100 + i),
                "h": str(102 + i), "l": str(99 + i), "c": str(101 + i),
                "v": "10"} for i in range(20)]
    dataset_response = client.post("/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "CSV local", "candles": candles})
    assert dataset_response.status_code == 200, dataset_response.text
    dataset = dataset_response.json()
    strategy_response = client.post("/lab/strategies", json={
        "name": "Tendencia", "definition": {
            "direction": "LONG", "initial_capital": "1000", "allocation": "1",
            "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.03",
            "take_profit": "0.08", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}})
    assert strategy_response.status_code == 200, strategy_response.text
    strategy = strategy_response.json()
    run_response = client.post("/lab/runs", json={
        "dataset_id": dataset["id"], "version_id": strategy["version_id"]})
    assert run_response.status_code == 200, run_response.text
    return client, path, dataset, run_response.json()


def test_lab_backup_is_private_verified_and_contains_exact_run(tmp_path):
    client, path, dataset, run = prepared_lab(tmp_path)
    result = create_lab_backup(LabStore(path))
    assert result["encrypted"] is False
    assert result["verified"]["verified_datasets"] == 1
    assert result["verified"]["verified_runs"] == 1
    backup = path.parent / "lab-backups" / result["name"]
    assert str(backup.resolve()) == result["path"]
    assert os.stat(backup).st_mode & 0o777 == 0o600
    assert os.stat(backup.parent).st_mode & 0o777 == 0o700
    assert verify_lab_backup(backup) == result["verified"]
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT hash FROM datasets").fetchone()[0] == dataset["hash"]
        assert db.execute("SELECT id FROM runs").fetchone()[0] == run["id"]
    assert client.get("/lab/backups").json()[0]["name"] == result["name"]
    assert not list(backup.parent.glob(".lab-pending-*"))


def test_lab_backup_rejects_corrupt_dataset_without_final_file(tmp_path):
    client, path, _, _ = prepared_lab(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE datasets SET hash='bad'")
    with pytest.raises(LabError, match="huella"):
        create_lab_backup(LabStore(path))
    assert client.get("/lab/backups").json() == []
    assert not list((tmp_path / "lab-backups").glob(".lab-pending-*"))


def test_lab_backup_rejects_corrupt_run_without_final_file(tmp_path):
    client, path, _, run = prepared_lab(tmp_path)
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?",
                                      (run["id"],)).fetchone()[0])
        saved["metrics"]["return_pct"] = "999"
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(saved), run["id"]))
    with pytest.raises(LabError, match="huella"):
        create_lab_backup(LabStore(path))
    assert client.get("/lab/backups").json() == []


def test_lab_backup_labels_legacy_run_without_hash(tmp_path):
    client, path, _, run = prepared_lab(tmp_path)
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT result_json FROM runs WHERE id=?",
                                      (run["id"],)).fetchone()[0])
        for field in ("result_hash", "dataset_hash", "dataset_source", "strategy_hash"):
            saved.pop(field, None)
        db.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(saved), run["id"]))
    result = create_lab_backup(LabStore(path))
    assert result["verified"]["verified_runs"] == 0
    assert result["verified"]["legacy_runs_without_hash"] == 1


def test_lab_backup_local_only_and_missing_database(tmp_path):
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    assert client.post("/lab/backup", headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert client.get("/lab/backups", headers={"Origin": "https://outside.invalid"}).status_code == 403
    retired = client.post("/lab/backup")
    assert retired.status_code == 410 and "sin cifrar" in retired.json()["detail"]
    assert not path.exists()


def test_offline_restore_keeps_previous_lab_and_exact_recovered_data(tmp_path):
    client, path, dataset, run = prepared_lab(tmp_path)
    backup = create_lab_backup(LabStore(path))
    other = [{"t": 1735689600 + i * 3600, "o": str(200 + i),
              "h": str(202 + i), "l": str(199 + i), "c": str(201 + i),
              "v": "10"} for i in range(20)]
    assert client.post("/lab/datasets", json={"symbol": "ETH/USDT", "timeframe": "1h",
                                               "source": "CSV local", "candles": other}).status_code == 200
    assert len(client.get("/lab/datasets").json()) == 2
    restored = restore_lab_backup(LabStore(path), backup["name"], "RESTAURAR")
    assert restored["restored"] is True
    assert restored["verified"]["verified_runs"] == 1
    assert len(client.get("/lab/datasets").json()) == 1
    assert client.get(f"/lab/runs/{run['id']}").json()["dataset_hash"] == dataset["hash"]
    recovery = tmp_path / "lab-backups" / restored["previous_lab_backup"]
    assert recovery.is_file()
    assert os.stat(recovery).st_mode & 0o777 == 0o600
    assert verify_lab_backup(recovery)["counts"]["datasets"] == 2


def test_plain_restore_rejects_changed_active_after_preimage(tmp_path, monkeypatch):
    _client, path, dataset, _run = prepared_lab(tmp_path)
    backup = create_lab_backup(LabStore(path))
    from app.lab import backup as backup_module
    actual_copy = backup_module._copy_exclusive

    def changed_after_preimage(source, destination):
        actual_copy(source, destination)
        if source != path:
            return
        original = path.stat()
        with sqlite3.connect(path) as db:
            old = db.execute("SELECT fetched_at FROM datasets WHERE id=?",
                             (dataset["id"],)).fetchone()[0]
            db.execute("UPDATE datasets SET fetched_at=? WHERE id=?",
                       ("X" * len(old), dataset["id"]))
        assert path.stat().st_size == original.st_size
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(backup_module, "_copy_exclusive", changed_after_preimage)
    with pytest.raises(LabError, match="cambió durante la restauración"):
        restore_lab_backup(LabStore(path), backup["name"], "RESTAURAR")
    assert path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert not list(tmp_path.glob(".lab.db.restore-*"))


def test_offline_restore_refuses_running_app_and_unmanaged_names(tmp_path):
    client, path, _, _ = prepared_lab(tmp_path)
    backup = create_lab_backup(LabStore(path))
    store = LabStore(path)
    with pytest.raises(LabError, match="RESTAURAR"):
        restore_lab_backup(store, backup["name"], "")
    with pytest.raises(LabError, match="gestionada"):
        restore_lab_backup(store, "../lab.db", "RESTAURAR")
    with lab_runtime_lease(path):
        with pytest.raises(LabError, match="sigue usando LAB"):
            restore_lab_backup(store, backup["name"], "RESTAURAR")
    assert client.get("/lab/backups").json()[0]["name"] == backup["name"]


def test_offline_restore_rejects_invalid_backup_and_pending_journal(tmp_path):
    client, path, _, _ = prepared_lab(tmp_path)
    backup = create_lab_backup(LabStore(path))
    source = tmp_path / "lab-backups" / backup["name"]
    with sqlite3.connect(source) as db:
        db.execute("UPDATE datasets SET hash='bad'")
    with pytest.raises(LabError, match="huella"):
        restore_lab_backup(LabStore(path), backup["name"], "RESTAURAR")
    assert not list(tmp_path.glob(".lab.db.restore-*"))
    source.unlink()
    backup = create_lab_backup(LabStore(path))
    journal = Path(str(path) + "-wal")
    journal.touch()
    with pytest.raises(LabError, match="journal"):
        restore_lab_backup(LabStore(path), backup["name"], "RESTAURAR")
    assert path.is_file()


def test_offline_restore_cli_requires_confirmation(tmp_path):
    client, path, _, _ = prepared_lab(tmp_path)
    backup = create_lab_backup(LabStore(path))
    script = Path(__file__).resolve().parents[1] / "scripts" / "restore_lab.py"
    command = [sys.executable, str(script), "--lab-db", str(path)]
    listing = subprocess.run([*command, "--list"], capture_output=True, text=True, check=True)
    assert backup["name"] in listing.stdout
    denied = subprocess.run([*command, "--restore", backup["name"]],
                            capture_output=True, text=True)
    assert denied.returncode == 1 and "RESTAURAR" in denied.stderr
    inspected = subprocess.run([*command, "--inspect", backup["name"]],
                               capture_output=True, text=True, check=True)
    assert json.loads(inspected.stdout)["verified_runs"] == 1
    restored = subprocess.run([*command, "--restore", backup["name"],
                               "--confirm", "RESTAURAR"],
                              capture_output=True, text=True, check=True)
    assert json.loads(restored.stdout)["restored"] is True
