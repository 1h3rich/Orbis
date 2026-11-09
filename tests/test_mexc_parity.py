"""Paridad Spot: doble lectura necesaria, nunca permiso para operar."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.portfolio.api import create_real_router
from app.portfolio import api as portfolio_api
from app.portfolio.domain import AccountSnapshot, PortfolioError, Position
from app.portfolio.mexc_parity import assess_mexc_spot_parity
from app.portfolio.models import RealAccount, RealMexcParityObservation
from app.portfolio.repository import create_account
from app.portfolio.vault import RealVault, VaultError


def observation(*, btc_free="0.5", btc_locked="0", usdt="100", at=None):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "observed_at": (at or datetime.now(timezone.utc)).isoformat(),
            "balances": [
                {"asset": "BTC", "free": btc_free, "locked": btc_locked,
                 "total": str(Decimal(btc_free) + Decimal(btc_locked))},
                {"asset": "USDT", "free": usdt, "locked": "0", "total": usdt},
            ]}


def test_pure_parity_requires_stable_nonempty_exact_unlocked_units():
    book = AccountSnapshot("book", cash={"USDT": Decimal("100")})
    book.positions[("BTC", "USDT")] = Position("BTC", "USDT", Decimal("0.5"))
    first = observation(btc_free="0.4", btc_locked="0.1")
    second = observation(btc_free="0.4", btc_locked="0.1")
    blocked = assess_mexc_spot_parity(book, first, second,
                                      book_revision_before="a", book_revision_after="a")
    assert blocked["blockers"] == ["MEXC_LOCKED_FUNDS"]
    assert blocked["locked_assets"] == [{"asset": "BTC", "locked": "0.1"}]
    assert blocked["reconciled"] is False and blocked["orders_enabled"] is False
    changed = assess_mexc_spot_parity(book, first, observation(),
                                      book_revision_before="a", book_revision_after="b")
    assert "BOOK_CHANGED_DURING_CHECK" in changed["blockers"]
    assert "MEXC_CHANGED_DURING_CHECK" in changed["blockers"]
    clean = assess_mexc_spot_parity(book, observation(), observation(),
                                    book_revision_before="a", book_revision_after="a")
    assert clean["status"] == "PARITY_OBSERVED_NOT_RECONCILED"
    assert clean["blockers"] == []
    assert clean["external_orders_or_transfers_excluded"] is True
    with pytest.raises(PortfolioError, match="retrocedió"):
        assess_mexc_spot_parity(book, observation(),
                                observation(at=datetime.now(timezone.utc) - timedelta(seconds=1)),
                                book_revision_before="a", book_revision_after="a")


def test_parity_api_checks_link_uid_book_and_two_view_reads(tmp_path, monkeypatch):
    state = {"mode": "stable", "balance_calls": 0, "uid_calls": 0, "on_second": None}

    class View:
        def __init__(self, key, secret):
            assert (key, secret) == ("parity-view-key", "parity-view-secret")

        def account_uid(self):
            state["uid_calls"] += 1
            return "22334455" if state["mode"] != "uid_drift" or state["uid_calls"] == 1 else "55443322"

        def balances(self):
            state["balance_calls"] += 1
            if state["balance_calls"] == 2 and state["on_second"] is not None:
                state["on_second"]()
            if state["mode"] == "drift" and state["balance_calls"] == 2:
                return observation(btc_free="0.6")
            if state["mode"] == "reversed_time" and state["balance_calls"] == 2:
                return observation(at=datetime.now(timezone.utc) - timedelta(seconds=1))
            if state["mode"] == "locked":
                return observation(btc_free="0.4", btc_locked="0.1")
            return observation()

    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db", mexc_factory=View))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "parity test password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "MEXC", "base_currency": "USDT"},
                          headers=headers).json()["id"]
    events_path = f"/real/accounts/{account}/events"
    for item in ({"kind": "CONTRIBUTION", "amount": "150"},
                 {"kind": "BUY", "asset": "BTC", "quantity": "0.5", "unit_price": "100"}):
        response = client.post(events_path, json={
            "request_id": str(uuid4()), "quote_currency": "USDT",
            "occurred_at": datetime.now(timezone.utc).isoformat(), **item}, headers=headers)
        assert response.status_code == 200, response.text
    url = f"/real/accounts/{account}/mexc-spot-parity"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers).status_code == 409
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": "parity-view-key", "secret_key": "parity-view-secret"},
        headers=headers).status_code == 200
    assert client.get(url, headers=headers).status_code == 409
    assert client.post(f"/real/accounts/{account}/mexc-link", json={
        "confirmation": "VINCULAR"}, headers=headers).status_code == 200
    original_snapshot = portfolio_api.account_snapshot

    def snapshot_in_read_transaction(db, account_id):
        assert db.connection().connection.driver_connection.in_transaction
        return original_snapshot(db, account_id)

    monkeypatch.setattr(portfolio_api, "account_snapshot", snapshot_in_read_transaction)
    state.update(mode="stable", balance_calls=0, uid_calls=0)
    clean = client.get(url, headers=headers)
    assert clean.status_code == 200, clean.text
    assert clean.headers["cache-control"] == "no-store"
    assert state["balance_calls"] == 2 and state["uid_calls"] == 3
    assert clean.json()["status"] == "PARITY_OBSERVED_NOT_RECONCILED"
    assert clean.json()["blockers"] == [] and clean.json()["reconciled"] is False
    assert "22334455" not in clean.text and "parity-view-key" not in clean.text

    state.update(mode="drift", balance_calls=0, uid_calls=0)
    drift = client.get(url, headers=headers)
    assert drift.status_code == 200
    assert drift.json()["status"] == "BLOCKED"
    assert "MEXC_CHANGED_DURING_CHECK" in drift.json()["blockers"]
    assert "QUANTITY_DIFFERENCE" in drift.json()["blockers"]

    state.update(mode="locked", balance_calls=0, uid_calls=0)
    locked = client.get(url, headers=headers)
    assert locked.status_code == 200 and locked.json()["blockers"] == ["MEXC_LOCKED_FUNDS"]

    state.update(mode="reversed_time", balance_calls=0, uid_calls=0)
    reversed_time = client.get(url, headers=headers)
    assert reversed_time.status_code == 409
    assert reversed_time.json()["detail"] == "Lecturas MEXC no comparables"

    state.update(mode="uid_drift", balance_calls=0, uid_calls=0)
    assert client.get(url, headers=headers).status_code == 409

    def write_during_second_read():
        response = client.post(events_path, json={
            "request_id": str(uuid4()), "kind": "CONTRIBUTION", "quote_currency": "USDT",
            "amount": "1", "occurred_at": datetime.now(timezone.utc).isoformat()}, headers=headers)
        assert response.status_code == 200, response.text

    state.update(mode="stable", balance_calls=0, uid_calls=0, on_second=write_during_second_read)
    book_changed = client.get(url, headers=headers)
    assert book_changed.status_code == 200, book_changed.text
    assert "BOOK_CHANGED_DURING_CHECK" in book_changed.json()["blockers"]
    assert book_changed.json()["orders_enabled"] is False


def test_parity_observation_is_encrypted_idempotent_and_invalidated_by_book_change(tmp_path):
    state = {"locked": True, "balance_calls": 0}

    class View:
        def __init__(self, key, secret):
            assert (key, secret) == ("parity-view-key", "parity-view-secret")

        def account_uid(self):
            return "22334455"

        def balances(self):
            state["balance_calls"] += 1
            return observation(btc_free="0.4", btc_locked="0.1") if state["locked"] else observation()

    path = tmp_path / "real.db"
    app = FastAPI()
    app.include_router(create_real_router(path, mexc_factory=View))
    client = TestClient(app)
    passphrase = "parity archive password"
    token = client.post("/real/vault/create", json={"passphrase": passphrase}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "MEXC", "base_currency": "USDT"},
                          headers=headers).json()["id"]
    other = client.post("/real/accounts", json={"name": "Otra"}, headers=headers).json()["id"]
    events = f"/real/accounts/{account}/events"
    for item in ({"kind": "CONTRIBUTION", "amount": "150"},
                 {"kind": "BUY", "asset": "BTC", "quantity": "0.5", "unit_price": "100"}):
        assert client.post(events, json={"request_id": str(uuid4()), "quote_currency": "USDT",
            "occurred_at": datetime.now(timezone.utc).isoformat(), **item}, headers=headers).status_code == 200
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": "parity-view-key", "secret_key": "parity-view-secret"},
        headers=headers).status_code == 200
    assert client.post(f"/real/accounts/{account}/mexc-link", json={
        "confirmation": "VINCULAR"}, headers=headers).status_code == 200
    url = f"/real/accounts/{account}/mexc-spot-parity/observations"
    request_id = str(uuid4())
    blocked = client.post(url, json={"request_id": request_id}, headers=headers)
    assert blocked.status_code == 200 and blocked.json()["saved"] is False
    assert "MEXC_LOCKED_FUNDS" in blocked.json()["blockers"]
    assert client.get(url, headers=headers).json() == []
    state["locked"] = False
    saved = client.post(url, json={"request_id": request_id}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] is True
    assert saved.json()["status"] == "PARITY_OBSERVED_NOT_RECONCILED"
    assert saved.json()["orders_enabled"] is False and saved.json()["reconciled"] is False
    reads = state["balance_calls"]
    again = client.post(url, json={"request_id": request_id}, headers=headers)
    assert again.status_code == 200 and again.json() == saved.json()
    assert state["balance_calls"] == reads
    assert client.post(f"/real/accounts/{other}/mexc-spot-parity/observations",
                       json={"request_id": request_id}, headers=headers).status_code == 409
    listed = client.get(url, headers=headers)
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert len(listed.json()) == 1
    assert listed.json()[0]["book_current"] is True and listed.json()[0]["recent"] is True
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert b"PARITY_OBSERVED_NOT_RECONCILED" not in path.read_bytes()
    backed = RealVault(path)
    backed.unlock(passphrase)
    copy = backed.backup(tmp_path / "valid-parity-copy.db")
    backed.lock()
    restored_read = RealVault(copy)
    restored_read.unlock(passphrase)
    with restored_read.session() as db:
        assert db.get(RealMexcParityObservation, request_id) is not None
    restored_read.lock()
    assert client.post(events, json={"request_id": str(uuid4()), "kind": "CONTRIBUTION",
        "quote_currency": "USDT", "amount": "1",
        "occurred_at": datetime.now(timezone.utc).isoformat()}, headers=headers).status_code == 200
    assert client.get(url, headers=headers).json()[0]["book_current"] is False
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    next_token = client.post("/real/vault/unlock", json={"passphrase": passphrase}).json()["token"]
    next_headers = {"X-Orbis-Vault-Token": next_token}
    assert client.get(url, headers=next_headers).json()[0]["book_current"] is False
    retried = client.post(url, json={"request_id": request_id}, headers=next_headers)
    assert retried.status_code == 200
    assert retried.json()["id"] == saved.json()["id"]
    assert retried.json()["book_current"] is False
    assert retried.json()["orders_enabled"] is False

    observer = RealVault(path)
    observer.unlock(passphrase)
    with observer.session() as db:
        row = db.get(RealMexcParityObservation, request_id)
        row.report_json = row.report_json.replace("PARITY_OBSERVED_NOT_RECONCILED", "BLOCKED")
        db.commit()
    observer.lock()
    assert client.get(url, headers=next_headers).status_code == 409
    vault = RealVault(path)
    vault.unlock(passphrase)
    with pytest.raises(VaultError, match="parity evidence"):
        vault.backup(tmp_path / "tampered-backup.db")
    vault.lock()


def test_v19_vault_migrates_to_v21_with_parity_table_and_preserves_book(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock("migration parity password", create=True)
    with vault.session() as db:
        account_id = create_account(db, "Histórica", "USDT").id
        db.connection().exec_driver_sql("DROP TABLE real_mexc_parity_observations")
        db.connection().exec_driver_sql("PRAGMA user_version = 19")
        db.commit()
    vault.lock()

    migrated = RealVault(path)
    migrated.unlock("migration parity password")
    assert migrated.last_migration_backup is not None
    assert migrated.last_migration_backup.read_bytes()[:16] != b"SQLite format 3\x00"
    with migrated.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_mexc_parity_observations").scalar_one() == 0
        assert db.get(RealAccount, account_id).name == "Histórica"
    migrated.lock()
