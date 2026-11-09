"""Copias LAB SQLCipher: recuperación privada y rechazo seguro."""

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
from app.lab.backup import create_lab_backup, lab_runtime_lease, list_lab_backups
from app.lab.engine import LabError
from app.lab.secure_backup import (
    create_secure_lab_backup, restore_secure_lab_backup, verify_secure_lab_backup,
)
from app.lab.store import LabStore


PASSWORD = "isolated LAB backup password"


def prepared(tmp_path):
    path = tmp_path / "lab.db"
    with LabStore(path).connect():  # Copia segura de una base LAB previa en claro.
        pass
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    candles = [{"t": 1735689600 + i * 3600, "o": str(100 + i),
                "h": str(102 + i), "l": str(99 + i), "c": str(101 + i),
                "v": "10"} for i in range(20)]
    response = client.post("/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "CSV local", "candles": candles,
    })
    assert response.status_code == 200, response.text
    return client, LabStore(path), response.json()


def test_encrypted_copy_verifies_without_plaintext_and_restores_offline(tmp_path):
    client, store, dataset = prepared(tmp_path)
    created = create_secure_lab_backup(store, PASSWORD)
    target = tmp_path / "lab-backups" / created["name"]
    assert created["encrypted"] is True
    assert target.read_bytes()[:16] != b"SQLite format 3\x00"
    assert dataset["hash"].encode() not in target.read_bytes()
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert verify_secure_lab_backup(target, PASSWORD) == created["verified"]
    with pytest.raises(LabError):
        verify_secure_lab_backup(target, "different long password")
    assert not list(target.parent.glob(".lab-plain-*"))
    assert not list(target.parent.glob(".lab-secure-pending-*"))

    second = [{"t": 1735689600 + i * 3600, "o": str(200 + i),
               "h": str(202 + i), "l": str(199 + i), "c": str(201 + i),
               "v": "10"} for i in range(20)]
    assert client.post("/lab/datasets", json={
        "symbol": "ETH/USDT", "timeframe": "1h", "source": "CSV local", "candles": second,
    }).status_code == 200
    with pytest.raises(LabError, match="RESTAURAR"):
        restore_secure_lab_backup(store, created["name"], "", PASSWORD)
    with pytest.raises(LabError):
        restore_secure_lab_backup(store, created["name"], "RESTAURAR",
                                  "different long password")
    assert len(client.get("/lab/datasets").json()) == 2
    restored = restore_secure_lab_backup(store, created["name"], "RESTAURAR", PASSWORD)
    assert restored["restored"] is True
    assert client.get("/lab/datasets").json()[0]["hash"] == dataset["hash"]
    assert len(client.get("/lab/datasets").json()) == 1
    previous = target.parent / restored["previous_lab_backup"]
    assert previous.read_bytes()[:16] != b"SQLite format 3\x00"
    assert verify_secure_lab_backup(previous, PASSWORD)["counts"]["datasets"] == 2
    assert not list(tmp_path.glob(".lab.db.restore-*"))


def test_secure_restore_refuses_running_server_and_corrupt_copy(tmp_path):
    _client, store, _dataset = prepared(tmp_path)
    created = create_secure_lab_backup(store, PASSWORD)
    with lab_runtime_lease(store.path):
        with pytest.raises(LabError, match="sigue usando LAB"):
            restore_secure_lab_backup(store, created["name"], "RESTAURAR", PASSWORD)
    target = tmp_path / "lab-backups" / created["name"]
    with target.open("r+b") as file:
        file.seek(100)
        file.write(b"corrupt")
    with pytest.raises(LabError):
        restore_secure_lab_backup(store, created["name"], "RESTAURAR", PASSWORD)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 1


def test_secure_restore_rejects_changed_active_after_preimage(tmp_path, monkeypatch):
    _client, store, dataset = prepared(tmp_path)
    created = create_secure_lab_backup(store, PASSWORD)
    from app.lab import secure_backup
    actual_backup = secure_backup._create_secure_lab_backup

    def changed_after_preimage(current, password, *, recovery):
        result = actual_backup(current, password, recovery=recovery)
        if recovery:
            original = store.path.stat()
            with sqlite3.connect(store.path) as db:
                old = db.execute("SELECT fetched_at FROM datasets WHERE id=?",
                                 (dataset["id"],)).fetchone()[0]
                db.execute("UPDATE datasets SET fetched_at=? WHERE id=?",
                           ("X" * len(old), dataset["id"]))
            assert store.path.stat().st_size == original.st_size
            os.utime(store.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        return result

    monkeypatch.setattr(secure_backup, "_create_secure_lab_backup", changed_after_preimage)
    with pytest.raises(LabError, match="cambió durante la restauración"):
        restore_secure_lab_backup(store, created["name"], "RESTAURAR", PASSWORD)
    assert store.path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert not list(tmp_path.glob(".lab.db.restore-*"))


def test_encrypted_backup_api_listing_and_offline_cli(tmp_path):
    client, store, dataset = prepared(tmp_path)
    assert client.post("/lab/backup/encrypted", json={"passphrase": "short"}).status_code == 422
    assert client.post("/lab/backup/encrypted", json={"passphrase": PASSWORD},
                       headers={"Origin": "https://outside.invalid"}).status_code == 403
    response = client.post("/lab/backup/encrypted", json={"passphrase": PASSWORD})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    created = response.json()
    assert created["encrypted"] is True
    assert PASSWORD not in response.text
    listed = client.get("/lab/backups").json()
    assert listed[0]["name"] == created["name"] and listed[0]["encrypted"] is True
    retired = client.post("/lab/backup")
    assert retired.status_code == 410
    assert [row["name"] for row in client.get("/lab/backups").json()] == [created["name"]]
    script = Path(__file__).resolve().parents[1] / "scripts" / "restore_lab.py"
    command = [sys.executable, str(script), "--lab-db", str(store.path)]
    missing = subprocess.run([*command, "--inspect", created["name"]],
                             capture_output=True, text=True)
    assert missing.returncode == 1 and "--passphrase-stdin" in missing.stderr
    inspected = subprocess.run([*command, "--inspect", created["name"], "--passphrase-stdin"],
                               input=PASSWORD + "\n", capture_output=True, text=True, check=True)
    assert json.loads(inspected.stdout)["verified_datasets"] == 1
    denied = subprocess.run([*command, "--restore", created["name"],
                             "--passphrase-stdin"], input=PASSWORD + "\n",
                            capture_output=True, text=True)
    assert denied.returncode == 1 and "RESTAURAR" in denied.stderr
    restored = subprocess.run([*command, "--restore", created["name"],
                               "--confirm", "RESTAURAR", "--passphrase-stdin"],
                              input=PASSWORD + "\n", capture_output=True, text=True, check=True)
    assert json.loads(restored.stdout)["restored"] is True
    assert client.get("/lab/datasets").json()[0]["hash"] == dataset["hash"]


def test_listing_orders_mixed_copies_by_time_and_labels_legacy(tmp_path):
    _client, store, _dataset = prepared(tmp_path)
    plain = create_lab_backup(store)
    secure = create_secure_lab_backup(store, PASSWORD)
    directory = tmp_path / "lab-backups"
    os.utime(directory / plain["name"], (100, 100))
    os.utime(directory / secure["name"], (200, 200))
    rows = list_lab_backups(store)
    assert [(row["name"], row["encrypted"]) for row in rows] == [
        (secure["name"], True), (plain["name"], False)]
    os.utime(directory / plain["name"], (300, 300))
    assert list_lab_backups(store)[0]["name"] == plain["name"]
