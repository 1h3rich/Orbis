"""Desbloqueo local de LAB cifrado compartido por API y DCA."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.api import create_automation_router
from app.lab.access import LabAccess, lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError, validate_candles
from app.lab.store import LabStore


PASSWORD = "active LAB secret password"


def test_new_lab_requires_encrypted_creation_and_empty_lists_do_not_write(tmp_path):
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)

    for route in ("datasets", "strategies", "strategy-versions", "runs",
                  "validations", "optimizations", "research/runs"):
        response = client.get("/lab/" + route)
        assert response.status_code == 200 and response.json() == []
    assert not path.exists()
    blocked = client.post("/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1d", "source": "fixture", "candles": [
            {"t": 1735689600 + i * 86400, "o": "100", "h": "101", "l": "99", "c": "100", "v": "10"}
            for i in range(3)]})
    assert blocked.status_code == 423
    assert not path.exists()
    assert client.post("/lab/create", json={"passphrase": PASSWORD},
                       headers={"Origin": "https://outside.invalid"}).status_code == 403
    assert not path.exists()
    created = client.post("/lab/create", json={"passphrase": PASSWORD})
    assert created.status_code == 200 and created.json()["unlocked"] is True
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert client.post("/lab/create", json={"passphrase": PASSWORD}).status_code == 422
    assert client.post("/lab/lock").json()["unlocked"] is False
    assert client.get("/lab/datasets").status_code == 423
    assert client.post("/lab/unlock", json={"passphrase": PASSWORD}).status_code == 200


def test_access_expires_without_worker_reads_extending_it(tmp_path):
    path = tmp_path / "lab.db"
    with LabStore(path, passphrase=PASSWORD).connect():
        pass
    tick = [100.0]
    access = LabAccess(timeout_seconds=15, clock=lambda: tick[0])
    assert access.status(path)["unlocked"] is False
    assert access.unlock(path, PASSWORD)["remaining_seconds"] == 15
    tick[0] = 114.0
    assert access.get(path) == PASSWORD
    assert access.status(path)["remaining_seconds"] == 1
    tick[0] = 115.0
    assert access.get(path) is None
    assert access.status(path)["unlocked"] is False


def test_lab_api_unlock_is_shared_with_dca_and_lock_fails_closed(tmp_path):
    lab_path, dca_path = tmp_path / "lab.db", tmp_path / "dca.db"
    rows = [{"t": int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()) + i * 86400,
             "o": str(100 - i), "h": str(101 - i), "l": str(99 - i),
             "c": str(100 - i), "v": "10"} for i in range(5)]
    bars = validate_candles(rows, "1d", now=rows[-1]["t"] + 10 * 86400)[0]
    dataset = LabStore(lab_path, passphrase=PASSWORD).save_dataset(
        "BTC/USDT", "1d", "fixture", bars, [])
    app = FastAPI()
    app.include_router(create_lab_router(lab_path))
    app.include_router(create_automation_router(dca_path, lab_path))
    client = TestClient(app)
    lab_access.lock(lab_path)
    try:
        status = client.get("/lab/status")
        assert status.status_code == 200
        assert status.json()["format"] == "encrypted_or_invalid"
        assert status.json()["unlocked"] is False
        assert status.headers["cache-control"] == "no-store"
        assert client.post("/lab/unlock", json={"passphrase": PASSWORD},
                           headers={"Origin": "https://outside.invalid"}).status_code == 403
        wrong = client.post("/lab/unlock", json={"passphrase": "incorrect LAB password"})
        assert wrong.status_code == 422 and PASSWORD not in wrong.text
        assert client.get("/lab/status").json()["unlocked"] is False
        with pytest.raises(LabError, match="contraseña o formato"):
            LabStore(lab_path).datasets()

        unlocked = client.post("/lab/unlock", json={"passphrase": PASSWORD})
        assert unlocked.status_code == 200 and unlocked.json()["unlocked"] is True
        assert unlocked.headers["cache-control"] == "no-store"
        assert PASSWORD not in unlocked.text
        assert client.get("/lab/datasets").json()[0]["id"] == dataset["id"]
        created = client.post("/automation/dca/create", json={"passphrase": PASSWORD})
        assert created.status_code == 200 and created.json()["unlocked"] is True
        rule = client.post("/automation/dca/rules", json={"name": "Plan", "definition": {
            "frequency": "daily", "amount": "10", "budget_total": "100",
            "monthly_limit": "100", "max_exposure": "1", "fee_rate": "0",
            "slippage_rate": "0", "max_extra_activations": 0,
            "cooldown_bars": 0, "dips": []}})
        assert rule.status_code == 200, rule.text
        run = client.post("/automation/dca/runs", json={
            "dataset_id": dataset["id"], "version_id": rule.json()["version_id"]})
        assert run.status_code == 200, run.text
        assert run.json()["dataset_hash"] == dataset["hash"]
        assert PASSWORD.encode() not in dca_path.read_bytes()

        locked = client.post("/lab/lock")
        assert locked.status_code == 200 and locked.json()["unlocked"] is False
        with pytest.raises(LabError, match="contraseña o formato"):
            LabStore(lab_path).datasets()
        assert client.get("/lab/status").json()["unlocked"] is False
    finally:
        lab_access.lock(lab_path)
