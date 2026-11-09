"""Cifrado DCA/PAPER con datos temporales; nunca usa el fichero del usuario."""

from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys

import pytest
import sqlcipher3
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.access import automation_access
from app.automation.api import create_automation_router
from app.automation.paper_dca import create_paper_dca_router
from app.automation.lease import automation_lease
from app.automation.migration import (
    encrypt_active_automation, restore_secure_automation, verify_automation_database,
)
from app.automation.store import AutomationStore
from app.lab.engine import LabError, validate_candles
from app.lab.store import LabStore, content_hash


PASSWORD = "automation migration test password"


def test_new_dca_requires_encrypted_creation_and_preserves_existing_file(tmp_path):
    path = tmp_path / "automation.db"
    app = FastAPI()
    app.include_router(create_automation_router(path, tmp_path / "lab.db"))
    client = TestClient(app)

    blocked = client.post("/automation/dca/rules", json={"name": "Plan", "definition": {}})
    assert blocked.status_code == 423
    assert not path.exists()
    paper_app = FastAPI()
    paper_app.include_router(create_paper_dca_router(path, tmp_path / "lab.db"))
    paper = TestClient(paper_app)
    assert paper.get("/automation/dca/paper/sessions").json() == []
    assert paper.post("/automation/dca/paper/sessions", json={
        "version_id": "missing", "dataset_id": "missing"}).status_code == 422
    assert not path.exists()
    assert client.post("/automation/dca/create", json={"passphrase": PASSWORD},
                       headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert not path.exists()
    created = client.post("/automation/dca/create", json={"passphrase": PASSWORD})
    assert created.status_code == 200 and created.json()["unlocked"] is True
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert client.get("/automation/dca/rules").json() == []
    assert client.post("/automation/dca/create", json={"passphrase": PASSWORD}).status_code == 422
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert client.post("/automation/dca/lock").json()["unlocked"] is False
    assert client.get("/automation/dca/rules").status_code == 423
    assert client.post("/automation/dca/unlock", json={"passphrase": PASSWORD}).status_code == 200


def prepared(tmp_path):
    store = AutomationStore(tmp_path / "automation.db")
    rule = store.create_rule("DCA BTC", {"amount": "100"})
    with store.connect() as db:
        db.execute("""INSERT INTO paper_dca_sessions
            (id,version_id,dataset_id,dataset_hash,symbol,source,rule_json,state_json,
             overlap_json,next_exec_t,tick_index,started_at_ns,status,status_reason,
             last_quote_json,engine_version)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             ("session-1", rule["version_id"], "dataset-1", "a" * 64, "BTC/USDT",
              "MEXC", json.dumps(rule["definition"]), '{"cash":"100"}', "[]",
              1_800_000_000, 0, 1_800_000_000_000_000_000, "STOPPED", None,
              None, "dca-paper-1"))
        db.execute("INSERT INTO paper_dca_events VALUES (?,?,?,?,?,?)",
                   ("session-1", 1_800_000_000, 0, 1, 1, '{"kind":"OBSERVATION"}'))
    return store, rule


def test_migration_preserves_rules_sessions_events_and_independent_backup(tmp_path):
    store, rule = prepared(tmp_path)
    before = verify_automation_database(store.path)
    result = encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    assert result["verified"] == before
    assert store.path.read_bytes()[:16] != b"SQLite format 3\x00"
    backup = tmp_path / "automation-backups" / result["backup"]
    assert backup.is_file() and backup.stat().st_ino != store.path.stat().st_ino
    assert verify_automation_database(backup, PASSWORD) == before
    encrypted = AutomationStore(store.path, passphrase=PASSWORD)
    assert encrypted.rules()[0]["version_id"] == rule["version_id"]
    with encrypted.connect() as db:
        assert db.execute("SELECT count(*) FROM paper_dca_events").fetchone()[0] == 1
    with pytest.raises(LabError, match="contraseña o formato"):
        store.rules()
    with encrypted.connect() as db:
        db.execute("UPDATE dca_rules SET name='Editada' WHERE id=?", (rule["id"],))
    assert verify_automation_database(backup, PASSWORD) == before


def test_migration_rejects_running_server_journal_and_external_writer(tmp_path):
    store, _ = prepared(tmp_path)
    original = store.path.read_bytes()
    with pytest.raises(LabError, match="CIFRAR DCA"):
        encrypt_active_automation(store, "", PASSWORD)
    with automation_lease(store.path):
        with pytest.raises(LabError, match="sigue usando"):
            encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    journal = Path(str(store.path) + "-wal")
    journal.touch()
    with pytest.raises(LabError, match="journals"):
        encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    journal.unlink()
    with sqlite3.connect(store.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(LabError, match="Otro proceso"):
            encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    assert store.path.read_bytes() == original


def test_migration_rejects_changed_bytes_with_restored_size_and_mtime(tmp_path, monkeypatch):
    store, rule = prepared(tmp_path)
    original = store.path.stat()
    from app.automation import migration
    actual_sync = migration._sync_dir
    changed = False

    def changed_after_backup(directory):
        nonlocal changed
        actual_sync(directory)
        if changed:
            return
        changed = True
        with sqlite3.connect(store.path) as db:
            old = db.execute("SELECT name FROM dca_rules WHERE id=?",
                             (rule["id"],)).fetchone()[0]
            db.execute("UPDATE dca_rules SET name=? WHERE id=?",
                       ("X" * len(old), rule["id"]))
        assert store.path.stat().st_size == original.st_size
        os.utime(store.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(migration, "_sync_dir", changed_after_backup)
    with pytest.raises(LabError, match="cambió durante la migración"):
        encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert not list(tmp_path.glob(".automation.db.encrypt-*"))


def test_encrypted_automation_api_unlock_and_lock(tmp_path):
    store, rule = prepared(tmp_path)
    encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    app = FastAPI()
    app.include_router(create_automation_router(store.path, tmp_path / "lab.db"))
    client = TestClient(app)
    assert client.get("/automation/dca/status").json()["unlocked"] is False
    assert client.get("/automation/dca/rules").status_code == 423
    denied = client.post("/automation/dca/unlock", json={"passphrase": "wrong password here"})
    assert denied.status_code == 422 and PASSWORD not in denied.text
    unlocked = client.post("/automation/dca/unlock", json={"passphrase": PASSWORD})
    assert unlocked.status_code == 200 and unlocked.json()["unlocked"] is True
    assert client.get("/automation/dca/rules").json()[0]["version_id"] == rule["version_id"]
    assert client.post("/automation/dca/lock").json()["unlocked"] is False
    assert client.get("/automation/dca/rules").status_code == 423
    automation_access.lock(store.path)


def test_encrypted_store_can_save_dca_run_with_attached_lab(tmp_path):
    store, rule = prepared(tmp_path)
    lab = LabStore(tmp_path / "lab.db")
    start = 1_735_689_600
    raw = [{"t": start + day * 86400, "o": "100", "h": "101", "l": "99",
            "c": "100", "v": "10"} for day in range(3)]
    bars, gaps = validate_candles(raw, "1d", now=start + 4 * 86400)
    dataset = lab.save_dataset("BTC/USDT", "1d", "fixture", bars, gaps)
    encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    encrypted = AutomationStore(store.path, passphrase=PASSWORD)
    saved = encrypted.save_run(rule["version_id"], dataset["id"], dataset["hash"],
                               {"results": {"conditional": {"metrics": {}},
                                            "fixed": {"metrics": {}},
                                            "lump": {"metrics": {}}}}, "test-engine",
                               lab_path=lab.path,
                               expected_definition_hash=content_hash(rule["definition"]))
    assert encrypted.run(saved["id"])["dataset_hash"] == dataset["hash"]
    assert verify_automation_database(store.path, PASSWORD)["counts"]["dca_runs"] == 1


def test_cli_requires_confirmation_and_repeated_password(tmp_path):
    store, _ = prepared(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "encrypt_automation.py"
    command = [sys.executable, str(script), "--automation-db", str(store.path)]
    denied = subprocess.run(command, capture_output=True, text=True)
    assert denied.returncode == 1 and "CIFRAR DCA" in denied.stderr
    mismatch = subprocess.run([*command, "--confirm", "CIFRAR DCA", "--passphrase-stdin"],
                              input=PASSWORD + "\nother password\n", capture_output=True, text=True)
    assert mismatch.returncode == 1 and "no coinciden" in mismatch.stderr
    encrypted = subprocess.run([*command, "--confirm", "CIFRAR DCA", "--passphrase-stdin"],
                               input=PASSWORD + "\n" + PASSWORD + "\n",
                               capture_output=True, text=True)
    assert encrypted.returncode == 0, encrypted.stderr
    assert PASSWORD not in encrypted.stdout + encrypted.stderr
    assert verify_automation_database(store.path, PASSWORD)["counts"]["dca_rules"] == 1


def test_restore_recovers_encrypted_data_and_preserves_encrypted_preimage(tmp_path):
    store, rule = prepared(tmp_path)
    migrated = encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    encrypted = AutomationStore(store.path, passphrase=PASSWORD)
    encrypted.create_rule("Segunda regla", {"amount": "50"})
    altered = verify_automation_database(store.path, PASSWORD)
    assert altered["counts"]["dca_rules"] == 2
    with pytest.raises(LabError, match="RESTAURAR DCA"):
        restore_secure_automation(store, migrated["backup"], "", PASSWORD)
    with pytest.raises(LabError, match="no se pudo verificar"):
        restore_secure_automation(store, migrated["backup"], "RESTAURAR DCA",
                                  "wrong restore password")
    with automation_lease(store.path):
        with pytest.raises(LabError, match="sigue usando"):
            restore_secure_automation(store, migrated["backup"], "RESTAURAR DCA", PASSWORD)
    restored = restore_secure_automation(store, migrated["backup"], "RESTAURAR DCA", PASSWORD)
    assert restored["verified"] == migrated["verified"]
    assert encrypted.rules()[0]["version_id"] == rule["version_id"]
    assert len(encrypted.rules()) == 1
    previous = tmp_path / "automation-backups" / restored["previous_backup"]
    assert previous.read_bytes()[:16] != b"SQLite format 3\x00"
    assert verify_automation_database(previous, PASSWORD) == altered
    assert previous.stat().st_ino != store.path.stat().st_ino


def test_restore_rejects_changed_active_after_preimage(tmp_path, monkeypatch):
    store, rule = prepared(tmp_path)
    migrated = encrypt_active_automation(store, "CIFRAR DCA", PASSWORD)
    from app.automation import migration
    actual_sync = migration._sync_dir
    changed = False

    def changed_after_preimage(directory):
        nonlocal changed
        actual_sync(directory)
        if changed:
            return
        changed = True
        original = store.path.stat()
        with sqlcipher3.connect(store.path) as db:
            db.execute("PRAGMA key = '" + PASSWORD + "'")
            old = db.execute("SELECT name FROM dca_rules WHERE id=?",
                             (rule["id"],)).fetchone()[0]
            db.execute("UPDATE dca_rules SET name=? WHERE id=?",
                       ("X" * len(old), rule["id"]))
        assert store.path.stat().st_size == original.st_size
        os.utime(store.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(migration, "_sync_dir", changed_after_preimage)
    with pytest.raises(LabError, match="cambió durante la restauración"):
        restore_secure_automation(store, migrated["backup"], "RESTAURAR DCA", PASSWORD)
    assert verify_automation_database(store.path, PASSWORD)["counts"]["dca_rules"] == 1
    assert not list(tmp_path.glob(".automation.db.restore-*"))
