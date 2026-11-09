"""Bóveda GRVT PAPER: bloqueo, migración y recuperación sin tocar datos reales."""

from app.derivatives.paper_schema import CURRENT_PAPER_SCHEMA
from tests.paper_schema_fixture import drop_observed_tables
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

from app.derivatives.paper_access import PaperPerpAccess, paper_perp_access, paper_perp_format
from app.derivatives.paper_lease import paper_perp_lease
from app.derivatives.paper_math import PaperPerpError
from app.derivatives.paper_migration import (
    encrypt_active_paper_perp, restore_secure_paper_perp, verify_paper_perp_database,
)
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.paper_api import create_paper_perp_router
from app.derivatives.signal_monitor import SignalMonitor
from app.derivatives.funding_worker import AutoFunding


PASSWORD = "GRVT paper test secret 123"


def test_new_grvt_paper_requires_encrypted_setup_and_persists_account(tmp_path):
    path = tmp_path / "paper.db"
    assert PaperPerpStore(path, allow_create=False).accounts() == []
    monitor = SignalMonitor(path, tmp_path / "lab.db")
    assert monitor.active_sessions() == []
    assert monitor.active_risk_sessions() == []
    assert AutoFunding(monitor.paper).candidates() == []
    assert not path.exists()
    app = FastAPI()
    app.include_router(create_paper_perp_router(path))
    client = TestClient(app)
    assert client.get("/paper/perp/accounts").json() == []
    assert client.post("/paper/perp/accounts", json={
        "name": "Prueba", "initial_balance": "1000", "max_position_notional": "500",
        "max_total_notional": "800", "max_leverage": "5", "max_positions": 2,
        "fee_rate": "0.001", "slippage_bps": "0"}).status_code == 423
    assert not path.exists()
    assert client.post("/paper/perp/create", json={"passphrase": PASSWORD},
                       headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert not path.exists()
    created = client.post("/paper/perp/create", json={"passphrase": PASSWORD})
    assert created.status_code == 200 and created.json()["unlocked"] is True
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert client.post("/paper/perp/create", json={"passphrase": PASSWORD}).status_code == 422
    account = PaperPerpStore(path, allow_create=False).create_account(
        "Privada", "1000", "500", "800", "5", 2, "0.001", "0")
    assert client.post("/paper/perp/lock").json()["unlocked"] is False
    assert client.get("/paper/perp/accounts").status_code == 423
    assert client.post("/paper/perp/unlock", json={"passphrase": PASSWORD}).status_code == 200
    assert PaperPerpStore(path, allow_create=False).account(account["id"])["id"] == account["id"]


def seeded(tmp_path: Path) -> PaperPerpStore:
    store = PaperPerpStore(tmp_path / "paper.db")
    store.create_account("Privada", "1000", "500", "800", "5", 2, "0.001", "0")
    return store


def test_encrypted_version_six_unlocks_and_migrates_funding_anchor_schema(tmp_path):
    store = seeded(tmp_path)
    with sqlite3.connect(store.path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("DROP TABLE paper_perp_funding_anchors")
        db.execute("DROP TABLE paper_perp_funding_legacy")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN source_run_id")
        db.execute("PRAGMA user_version=6")
    assert verify_paper_perp_database(store.path)["schema_version"] == 6
    encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
    access = PaperPerpAccess()
    assert access.unlock(store.path, PASSWORD)["unlocked"] is True
    migrated = PaperPerpStore(store.path, passphrase=PASSWORD)
    assert len(migrated.accounts()) == 1
    assert verify_paper_perp_database(store.path, PASSWORD)["schema_version"] == CURRENT_PAPER_SCHEMA


def test_grvt_paper_access_expires_and_does_not_create_plain_database(tmp_path):
    store = seeded(tmp_path)
    encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
    clock = [10.0]
    access = PaperPerpAccess(timeout_seconds=30, clock=lambda: clock[0])
    assert access.status(store.path)["format"] == "encrypted_or_invalid"
    with pytest.raises(PaperPerpError, match="contraseña o formato"):
        access.unlock(store.path, "wrong password 12345")
    assert access.unlock(store.path, PASSWORD)["unlocked"]
    clock[0] = 41.0
    assert access.get(store.path) is None
    assert not access.status(store.path)["unlocked"]
    assert paper_perp_format(store.path) == "encrypted_or_invalid"
    with store.path.open("rb") as encrypted:
        assert encrypted.read(16) != b"SQLite format 3\x00"


def test_grvt_paper_unlock_and_lock_wake_observers(tmp_path):
    store = seeded(tmp_path)
    encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
    wakes = []
    app = FastAPI()
    app.include_router(create_paper_perp_router(store.path,
                                                on_access_change=lambda: wakes.append(True)))
    client = TestClient(app)
    assert client.get("/paper/perp/accounts").status_code == 423
    assert client.post("/paper/perp/unlock", json={"passphrase": PASSWORD}).status_code == 200
    assert wakes == [True]
    assert client.get("/paper/perp/accounts").status_code == 200
    assert client.post("/paper/perp/lock").status_code == 200
    assert wakes == [True, True]
    assert client.get("/paper/perp/accounts").status_code == 423
    paper_perp_access.lock(store.path)


def test_grvt_paper_migration_rejects_running_server_and_open_reader(tmp_path):
    store = seeded(tmp_path)
    before = verify_paper_perp_database(store.path)
    with paper_perp_lease(store.path):
        with pytest.raises(PaperPerpError, match="Orbis sigue usando"):
            encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
    assert verify_paper_perp_database(store.path) == before
    with sqlite3.connect(store.path) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM paper_perp_accounts").fetchall()
        with pytest.raises(PaperPerpError, match="Otro proceso usa"):
            encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
        reader.rollback()
    assert paper_perp_format(store.path) == "plain"


def test_grvt_migration_rejects_changed_bytes_with_restored_metadata(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    original = store.path.stat()
    from app.derivatives import paper_migration
    actual_sync = paper_migration._sync_dir
    changed = False

    def changed_after_backup(directory):
        nonlocal changed
        actual_sync(directory)
        if changed:
            return
        changed = True
        with sqlite3.connect(store.path) as db:
            account_id, old = db.execute(
                "SELECT id,name FROM paper_perp_accounts LIMIT 1").fetchone()
            db.execute("UPDATE paper_perp_accounts SET name=? WHERE id=?",
                       ("X" * len(old), account_id))
        assert store.path.stat().st_size == original.st_size
        os.utime(store.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(paper_migration, "_sync_dir", changed_after_backup)
    with pytest.raises(PaperPerpError, match="cambió durante la migración"):
        encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
    assert paper_perp_format(store.path) == "plain"
    assert not list(tmp_path.glob(".paper.db.encrypt-*"))


def test_grvt_restore_rejects_changed_active_after_preimage(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    migrated = encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
    from app.derivatives import paper_migration
    actual_sync = paper_migration._sync_dir
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
            account_id, old = db.execute(
                "SELECT id,name FROM paper_perp_accounts LIMIT 1").fetchone()
            db.execute("UPDATE paper_perp_accounts SET name=? WHERE id=?",
                       ("X" * len(old), account_id))
        assert store.path.stat().st_size == original.st_size
        os.utime(store.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    monkeypatch.setattr(paper_migration, "_sync_dir", changed_after_preimage)
    with pytest.raises(PaperPerpError, match="cambió durante la restauración"):
        restore_secure_paper_perp(store, migrated["backup"], "RESTAURAR GRVT PAPER", PASSWORD)
    assert verify_paper_perp_database(store.path, PASSWORD)["counts"]["paper_perp_accounts"] == 1
    assert not list(tmp_path.glob(".paper.db.restore-*"))


def test_grvt_paper_restore_rejects_traversal_and_bad_password(tmp_path):
    store = seeded(tmp_path)
    encrypted = encrypt_active_paper_perp(store, "CIFRAR GRVT PAPER", PASSWORD)
    before = verify_paper_perp_database(store.path, PASSWORD)
    with pytest.raises(PaperPerpError, match="Selecciona"):
        restore_secure_paper_perp(store, "../outside.db", "RESTAURAR GRVT PAPER", PASSWORD)
    with pytest.raises(PaperPerpError, match="verificar"):
        restore_secure_paper_perp(store, encrypted["backup"],
                                  "RESTAURAR GRVT PAPER", "wrong password 12345")
    paper_perp_access.unlock(store.path, PASSWORD)
    with pytest.raises(PaperPerpError, match="Bloquea"):
        restore_secure_paper_perp(store, encrypted["backup"],
                                  "RESTAURAR GRVT PAPER", PASSWORD)
    paper_perp_access.lock(store.path)
    assert verify_paper_perp_database(store.path, PASSWORD) == before


def test_grvt_paper_encrypt_cli_requires_confirmation_and_migrates(tmp_path):
    store = seeded(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "encrypt_paper_perp.py"
    command = [sys.executable, str(script), "--paper-db", str(store.path),
               "--passphrase-stdin"]
    rejected = subprocess.run(command, input=PASSWORD + "\n" + PASSWORD + "\n",
                              text=True, capture_output=True, check=False)
    assert rejected.returncode == 1
    assert paper_perp_format(store.path) == "plain"
    accepted = subprocess.run(command + ["--confirm", "CIFRAR GRVT PAPER"],
                              input=PASSWORD + "\n" + PASSWORD + "\n",
                              text=True, capture_output=True, check=False)
    assert accepted.returncode == 0, accepted.stderr
    assert PASSWORD not in accepted.stdout
    assert verify_paper_perp_database(store.path, PASSWORD)["counts"]["paper_perp_accounts"] == 1
    backup = json.loads(accepted.stdout)["backup"]
    PaperPerpStore(store.path, passphrase=PASSWORD).create_account(
        "Posterior", "1000", "500", "800", "5", 2, "0.001", "0")
    restore_script = script.with_name("restore_paper_perp.py")
    restored = subprocess.run(
        [sys.executable, str(restore_script), "--paper-db", str(store.path),
         "--restore", backup, "--confirm", "RESTAURAR GRVT PAPER", "--passphrase-stdin"],
        input=PASSWORD + "\n", text=True, capture_output=True, check=False,
    )
    assert restored.returncode == 0, restored.stderr
    assert PASSWORD not in restored.stdout
    assert verify_paper_perp_database(store.path, PASSWORD)["counts"]["paper_perp_accounts"] == 1
