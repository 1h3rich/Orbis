"""Migración offline LAB: datos exactos, copia cifrada y fallo cerrado."""

import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.backup import lab_runtime_lease
from app.lab.engine import CURRENT_ENGINE_VERSION, LabError, run_backtest, validate_candles, validate_strategy
from app.lab.migration import encrypt_active_lab
from app.lab.secure_backup import verify_secure_lab_backup
from app.lab.store import LabStore


PASSWORD = "LAB migration test password"


def prepared(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    raw = [{"t": 1735689600 + i * 3600, "o": str(100 + i),
            "h": str(102 + i), "l": str(99 + i), "c": str(101 + i),
            "v": "10"} for i in range(20)]
    bars, gaps = validate_candles(raw, "1h", now=1735689600 + 30 * 3600)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)
    definition = validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.03",
        "take_profit": "0.08", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2},
    })
    version = store.create_strategy("Tendencia", definition)
    result = store.save_run(dataset["id"], version["version_id"],
                            run_backtest(bars, definition), CURRENT_ENGINE_VERSION)
    return store, dataset, result


def test_migration_keeps_dataset_run_and_encrypted_recovery(tmp_path):
    store, dataset, run = prepared(tmp_path)
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    migrated = encrypt_active_lab(store, "CIFRAR LAB", PASSWORD)
    assert migrated["encrypted"] is True
    assert migrated["verified"]["verified_datasets"] == 1
    assert migrated["verified"]["verified_runs"] == 1
    assert store.path.read_bytes()[:16] != b"SQLite format 3\x00"
    backup = tmp_path / "lab-backups" / migrated["backup"]
    assert backup.is_file() and backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert verify_secure_lab_backup(backup, PASSWORD) == migrated["verified"]
    reopened = LabStore(store.path, passphrase=PASSWORD)
    assert reopened.dataset(dataset["id"])[0]["hash"] == dataset["hash"]
    assert reopened.run(run["id"]) == run
    with pytest.raises(LabError, match="contraseña o formato"):
        LabStore(store.path).datasets()
    app = FastAPI()
    app.include_router(create_lab_router(store.path))
    client = TestClient(app)
    assert client.get("/lab/status").json()["unlocked"] is False
    try:
        unlocked = client.post("/lab/unlock", json={"passphrase": PASSWORD})
        assert unlocked.status_code == 200 and unlocked.json()["unlocked"] is True
        assert client.get(f"/lab/runs/{run['id']}").json()["id"] == run["id"]
    finally:
        lab_access.lock(store.path)
    assert not list(tmp_path.glob(".lab.db.encrypt-*"))
    with pytest.raises(LabError, match="plano"):
        encrypt_active_lab(LabStore(store.path), "CIFRAR LAB", PASSWORD)


def test_migration_requires_confirmation_closed_server_and_no_journal(tmp_path):
    store, _, _ = prepared(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(LabError, match="CIFRAR LAB"):
        encrypt_active_lab(store, "", PASSWORD)
    with lab_runtime_lease(store.path):
        with pytest.raises(LabError, match="sigue usando LAB"):
            encrypt_active_lab(store, "CIFRAR LAB", PASSWORD)
    journal = Path(str(store.path) + "-wal")
    journal.touch()
    with pytest.raises(LabError, match="journal"):
        encrypt_active_lab(store, "CIFRAR LAB", PASSWORD)
    journal.unlink()
    assert store.path.read_bytes() == before
    assert not (tmp_path / "lab-backups").exists()


def test_migration_detects_external_change_after_backup(tmp_path, monkeypatch):
    store, dataset, _ = prepared(tmp_path)
    from app.lab import migration
    actual_backup = migration.create_secure_lab_backup

    def changed_after_snapshot(current, password):
        result = actual_backup(current, password)
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE datasets SET fetched_at='changed' WHERE id=?", (dataset["id"],))
        return result

    monkeypatch.setattr(migration, "create_secure_lab_backup", changed_after_snapshot)
    with pytest.raises(LabError, match="cambió"):
        encrypt_active_lab(store, "CIFRAR LAB", PASSWORD)
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert not list(tmp_path.glob(".lab.db.encrypt-*"))


def test_migration_rejects_changed_bytes_with_restored_size_and_mtime(tmp_path, monkeypatch):
    store, dataset, _ = prepared(tmp_path)
    original = store.path.stat()
    from app.lab import migration
    actual_copy = migration._copy_exclusive

    def changed_after_verified_backup(source, destination):
        actual_copy(source, destination)
        with sqlite3.connect(store.path) as db:
            old = db.execute("SELECT fetched_at FROM datasets WHERE id=?",
                             (dataset["id"],)).fetchone()[0]
            db.execute("UPDATE datasets SET fetched_at=? WHERE id=?",
                       ("x" * len(old), dataset["id"]))
        assert store.path.stat().st_size == original.st_size
        os.utime(store.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(migration, "_copy_exclusive", changed_after_verified_backup)
    with pytest.raises(LabError, match="cambió durante la migración"):
        encrypt_active_lab(store, "CIFRAR LAB", PASSWORD)
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert not list(tmp_path.glob(".lab.db.encrypt-*"))


def test_migration_rejects_external_sqlite_writer_without_replacing_lab(tmp_path):
    store, _, _ = prepared(tmp_path)
    with sqlite3.connect(store.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(LabError, match="Otro proceso usa LAB"):
            encrypt_active_lab(store, "CIFRAR LAB", PASSWORD)
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert not list(tmp_path.glob(".lab.db.encrypt-*"))


def test_migration_cli_requires_confirmation_and_reads_password_from_stdin(tmp_path):
    store, dataset, run = prepared(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "encrypt_lab.py"
    command = [sys.executable, str(script), "--lab-db", str(store.path)]
    denied = subprocess.run(command, capture_output=True, text=True)
    assert denied.returncode == 1 and "CIFRAR LAB" in denied.stderr
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    mismatch = subprocess.run([*command, "--confirm", "CIFRAR LAB", "--passphrase-stdin"],
                              input=PASSWORD + "\n" + "different password\n",
                              capture_output=True, text=True)
    assert mismatch.returncode == 1 and "no coinciden" in mismatch.stderr
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    encrypted = subprocess.run([*command, "--confirm", "CIFRAR LAB", "--passphrase-stdin"],
                               input=PASSWORD + "\n" + PASSWORD + "\n",
                               capture_output=True, text=True, check=True)
    result = json.loads(encrypted.stdout)
    assert result["encrypted"] is True
    assert PASSWORD not in encrypted.stdout + encrypted.stderr
    reopened = LabStore(store.path, passphrase=PASSWORD)
    assert reopened.dataset(dataset["id"])[0]["hash"] == dataset["hash"]
    assert reopened.run(run["id"]) == run
