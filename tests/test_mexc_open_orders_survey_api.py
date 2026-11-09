"""Sonda MEXC sin par: detecta órdenes, nunca concede cobertura global."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.portfolio.api import create_real_router
from app.portfolio.domain import PortfolioError
from app.portfolio.mexc_open_orders_survey import summarize_open_orders
from app.portfolio.mexc_open_orders_archive import (
    MexcOpenOrdersArchiveError, add_observation, compare_observations,
    normalize_snapshot,
)
from app.portfolio.models import RealMexcOpenOrdersObservation
from app.portfolio.repository import create_account
from app.portfolio.vault import RealVault, VaultError


def snapshot(orders=(), *, observed_at=None):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY",
            "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "UNFILTERED_COVERAGE_UNPROVEN", "symbol": None,
            "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
            "orders": list(orders)}


def test_survey_never_turns_zero_seen_into_coverage_or_permission():
    empty = summarize_open_orders(snapshot(), account_id="book")
    assert empty["status"] == "NO_ORDERS_SEEN_COVERAGE_UNPROVEN"
    assert empty["orders_enabled"] is False and empty["reconciled"] is False
    assert empty["symbols"] == []
    seen = summarize_open_orders(snapshot([
        {"symbol": "BTCUSDT", "status": "NEW", "order_id": "private-1"},
        {"symbol": "ETHUSDT", "status": "PARTIALLY_FILLED", "order_id": "private-2"},
        {"symbol": "BTCUSDT", "status": "NEW", "order_id": "private-3"},
    ]), account_id="book")
    assert seen["status"] == "OPEN_ORDERS_OBSERVED"
    assert seen["symbols"] == [{"symbol": "BTCUSDT", "count": 2},
                               {"symbol": "ETHUSDT", "count": 1}]
    assert "private-1" not in str(seen)
    with pytest.raises(PortfolioError, match="reciente"):
        summarize_open_orders(snapshot(observed_at=datetime.now(timezone.utc) -
                                       timedelta(minutes=1)), account_id="book")
    bad = snapshot()
    bad["coverage"] = "ALL_OPEN_ORDERS_FOR_ONE_SYMBOL"
    with pytest.raises(PortfolioError, match="cobertura"):
        summarize_open_orders(bad, account_id="book")


def test_archive_requires_exact_order_progress_and_distinct_ids():
    order = {"symbol": "BTCUSDT", "order_id": "order-1", "client_order_id": "client-1",
             "side": "BUY", "type": "LIMIT", "status": "PARTIALLY_FILLED",
             "price": "100000", "quantity": "0.00000000000000000003",
             "executed_quantity": "0.00000000000000000001",
             "remaining_quantity": "0.00000000000000000002"}
    payload, report = normalize_snapshot(snapshot([order]), account_id="book")
    assert report["observed_open_orders"] == 1
    assert payload["orders"][0]["remaining_quantity"] == "0.00000000000000000002"
    with pytest.raises(MexcOpenOrdersArchiveError, match="incoherente"):
        normalize_snapshot(snapshot([{**order, "remaining_quantity": "0.000000000000000000019"}]),
                           account_id="book")
    with pytest.raises(MexcOpenOrdersArchiveError, match="duplicada"):
        normalize_snapshot(snapshot([order, {**order, "order_id": "order-2"}]), account_id="book")


def test_archived_order_comparison_counts_transitions_without_claiming_outcomes(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("comparison orders password", create=True)
    base = datetime.now(timezone.utc) - timedelta(seconds=8)
    order = {"symbol": "BTCUSDT", "order_id": "order-1", "client_order_id": "client-1",
             "side": "BUY", "type": "LIMIT", "status": "PARTIALLY_FILLED",
             "price": "100", "quantity": "0.00000000000000000003",
             "executed_quantity": "0.00000000000000000001",
             "remaining_quantity": "0.00000000000000000002"}
    second = {**order, "price": "100.0", "executed_quantity": "0.00000000000000000002",
              "remaining_quantity": "0.00000000000000000001"}
    another = {**order, "symbol": "ETHUSDT", "order_id": "order-2", "client_order_id": "client-2",
               "status": "NEW", "executed_quantity": "0",
               "remaining_quantity": "0.00000000000000000003"}
    with vault.session() as db:
        book = create_account(db, "MEXC", "USDT").id
        ids = [str(uuid4()) for _ in range(5)]
        for idx, orders in enumerate(([order], [second, another], [],
                                      [{**order, "price": "101"}],
                                      [{**order, "price": "100.0"}])):
            add_observation(db, ids[idx], book, "a" * 64,
                            snapshot(orders, observed_at=base + timedelta(seconds=idx)))
        db.commit()
        changed = compare_observations(db, book, ids[0], ids[1])
        assert changed["counts"] == {"new": 1, "disappeared": 0, "progressed": 1,
                                      "unchanged": 0, "inconsistent": 0}
        assert changed["reconciled"] is False and changed["orders_enabled"] is False
        assert "PROGRESS_NEEDS_FILL_AUDIT" in changed["blockers"]
        assert "order-1" not in str(changed)
        disappeared = compare_observations(db, book, ids[1], ids[2])
        assert disappeared["counts"]["disappeared"] == 2
        assert "DISAPPEARED_ORDERS_NEED_FINAL_STATUS_AND_FILLS" in disappeared["blockers"]
        inconsistent = compare_observations(db, book, ids[0], ids[3])
        assert inconsistent["status"] == "INCONSISTENT"
        assert inconsistent["counts"]["inconsistent"] == 1
        unchanged = compare_observations(db, book, ids[0], ids[4])
        assert unchanged["status"] == "NO_CHANGE_OBSERVED"
        assert unchanged["counts"]["unchanged"] == 1
        assert unchanged["blockers"] == ["UNFILTERED_COVERAGE_UNPROVEN"]
        with pytest.raises(MexcOpenOrdersArchiveError, match="temporal"):
            compare_observations(db, book, ids[1], ids[0])
    vault.lock()


def test_survey_api_requires_link_and_stable_uid_without_exposing_order_ids(tmp_path):
    state = {"uid": "same", "orders": [], "drift": False}

    class View:
        def __init__(self, key, secret):
            assert (key, secret) == ("view-key", "view-secret")

        def account_uid(self):
            return "22334455" if state["uid"] == "same" else "55443322"

        def balances(self):
            return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                    "accounting": "OBSERVED_NOT_IMPORTED",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "balances": [{"asset": "USDT", "free": "10", "locked": "0", "total": "10"}]}

        def open_orders_unfiltered(self):
            if state["drift"]:
                state["uid"] = "other"
            return snapshot(state["orders"])

    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db", mexc_factory=View))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "open order survey password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    book = client.post("/real/accounts", json={"name": "MEXC"}, headers=headers).json()["id"]
    other = client.post("/real/accounts", json={"name": "Other"}, headers=headers).json()["id"]
    url = f"/real/accounts/{book}/mexc-open-orders-survey"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers).status_code == 409
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": "view-key", "secret_key": "view-secret"}, headers=headers).status_code == 200
    assert client.get(url, headers=headers).status_code == 409
    assert client.post(f"/real/accounts/{book}/mexc-link", json={
        "confirmation": "VINCULAR"}, headers=headers).status_code == 200
    assert client.get(f"/real/accounts/{other}/mexc-open-orders-survey", headers=headers).status_code == 409
    empty = client.get(url, headers=headers)
    assert empty.status_code == 200 and empty.headers["cache-control"] == "no-store"
    assert empty.json()["observed_open_orders"] == 0
    assert empty.json()["orders_enabled"] is False
    state["orders"] = [{"symbol": "BTCUSDT", "status": "NEW", "order_id": "sensitive-id"}]
    found = client.get(url, headers=headers)
    assert found.status_code == 200
    assert found.json()["symbols"] == [{"symbol": "BTCUSDT", "count": 1}]
    assert "sensitive-id" not in found.text and "22334455" not in found.text
    state["drift"] = True
    assert client.get(url, headers=headers).status_code == 409


def test_encrypted_open_order_archive_survives_restart_and_rejects_tampering(tmp_path):
    state = {"orders": [], "calls": 0}

    class View:
        def __init__(self, key, secret):
            assert (key, secret) == ("view-key", "view-secret")

        def account_uid(self):
            return "22334455"

        def balances(self):
            return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                    "accounting": "OBSERVED_NOT_IMPORTED",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "balances": [{"asset": "USDT", "free": "10", "locked": "0", "total": "10"}]}

        def open_orders_unfiltered(self):
            state["calls"] += 1
            return snapshot(state["orders"])

    path = tmp_path / "real.db"
    app = FastAPI()
    app.include_router(create_real_router(path, mexc_factory=View))
    client = TestClient(app)
    passphrase = "order archive password"
    token = client.post("/real/vault/create", json={"passphrase": passphrase}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    book = client.post("/real/accounts", json={"name": "MEXC"}, headers=headers).json()["id"]
    other = client.post("/real/accounts", json={"name": "Other"}, headers=headers).json()["id"]
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": "view-key", "secret_key": "view-secret"}, headers=headers).status_code == 200
    assert client.post(f"/real/accounts/{book}/mexc-link", json={
        "confirmation": "VINCULAR"}, headers=headers).status_code == 200
    url = f"/real/accounts/{book}/mexc-open-orders-survey/observations"
    request_id = str(uuid4())
    empty = client.post(url, json={"request_id": request_id}, headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json()["saved"] is True
    assert empty.json()["status"] == "NO_ORDERS_SEEN_COVERAGE_UNPROVEN"
    assert empty.json()["orders_enabled"] is False
    assert client.get(url, headers=headers).json()[0]["observed_open_orders"] == 0
    state["orders"] = [{"symbol": "BTCUSDT", "order_id": "secret-order-1",
                        "client_order_id": "secret-client-1", "side": "BUY", "type": "LIMIT",
                        "status": "NEW", "price": "100000", "quantity": "0.00000000000000000002",
                        "executed_quantity": "0", "remaining_quantity": "0.00000000000000000002"}]
    second_id = str(uuid4())
    found = client.post(url, json={"request_id": second_id}, headers=headers)
    assert found.status_code == 200, found.text
    assert found.json()["observed_open_orders"] == 1
    assert "secret-order-1" not in found.text
    reads = state["calls"]
    again = client.post(url, json={"request_id": second_id}, headers=headers)
    assert again.status_code == 200 and again.json() == found.json()
    assert state["calls"] == reads
    assert client.post(f"/real/accounts/{other}/mexc-open-orders-survey/observations",
                       json={"request_id": second_id}, headers=headers).status_code == 409
    comparison = client.get(
        f"{url}/{request_id}/compare/{second_id}", headers=headers)
    assert comparison.status_code == 200, comparison.text
    assert comparison.json()["counts"]["new"] == 1
    assert comparison.json()["coverage"] == "TWO_UNFILTERED_SNAPSHOTS_UNPROVEN"
    assert comparison.json()["orders_enabled"] is False
    assert "secret-order-1" not in comparison.text
    assert client.get(f"{url}/{second_id}/compare/{request_id}", headers=headers).status_code == 409
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert b"secret-order-1" not in path.read_bytes()
    vault = RealVault(path)
    vault.unlock(passphrase)
    copy = vault.backup(tmp_path / "orders-copy.db")
    with vault.session() as db:
        assert "secret-order-1" in db.get(RealMexcOpenOrdersObservation, second_id).payload_json
    vault.lock()
    recovered = RealVault(copy)
    recovered.unlock(passphrase)
    with recovered.session() as db:
        assert db.get(RealMexcOpenOrdersObservation, second_id) is not None
    recovered.lock()
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    token = client.post("/real/vault/unlock", json={"passphrase": passphrase}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert len(client.get(url, headers=headers).json()) == 2
    vault.unlock(passphrase)
    with vault.session() as db:
        row = db.get(RealMexcOpenOrdersObservation, second_id)
        row.payload_json = row.payload_json.replace("secret-order-1", "secret-order-2")
        db.commit()
    vault.lock()
    assert client.get(url, headers=headers).status_code == 409
    vault.unlock(passphrase)
    with pytest.raises(VaultError, match="open-order evidence"):
        vault.backup(tmp_path / "tampered-orders.db")
    vault.lock()


def test_v20_migrates_to_v21_with_open_orders_archive(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock("orders migration password", create=True)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_mexc_open_orders_observations")
        db.connection().exec_driver_sql("PRAGMA user_version = 20")
        db.commit()
    vault.lock()
    vault.unlock("orders migration password")
    assert vault.last_migration_backup is not None
    assert vault.last_migration_backup.read_bytes()[:16] != b"SQLite format 3\x00"
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_mexc_open_orders_observations").scalar_one() == 0
    vault.lock()
