"""La lectura privada de MEXC no modifica la cartera REAL ni guarda claves."""

import time
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.mexc_readonly import MexcReadOnlyError
from app.live_execution.journal import stop_spot_account
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.portfolio.api import RealAccess, create_real_router
from app.portfolio.vault import RealVault


KEY = "test-access-key-only"
SECRET = "test-secret-for-fixtures"


class FakeMexc:
    calls = []

    def __init__(self, key, secret):
        self.calls.append((key, secret))
        if key == "rejected-key":
            raise MexcReadOnlyError("La clave tiene permisos de escritura")

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": "2026-10-13T00:00:00+00:00",
                "balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1", "total": "0.6"}]}

    def account_uid(self):
        return "209302839"

    def account_reference(self):
        return hmac.new(SECRET.encode(), KEY.encode(), hashlib.sha256).hexdigest()


    def trades(self, symbol):
        if symbol != "BTCUSDT":
            raise MexcReadOnlyError("Par Spot MEXC no válido")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE", "symbol": symbol,
                "limit": 100, "observed_at": "2026-10-13T00:00:00+00:00", "trades": [
                    {"id": "fill-1", "order_id": "order-1", "side": "BUY",
                     "time_ms": 1_800_000_000_000, "price": "100", "quantity": "0.5",
                     "quote_quantity": "50", "commission": "0.01", "commission_asset": "USDT"}]}

    def trades_window(self, symbol, *, days):
        result = self.trades(symbol)
        return {**result, "coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT",
                "limit": 1000, "start_ms": 1_799_000_000_000,
                "end_ms": 1_801_000_000_000, "pages": 1}

    def transfers_recent(self, *, days, include_source_ids=False):
        assert days in (1, 7, 30, 90)
        assert include_source_ids is True
        end_ms = 1_800_000_000_000
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT", "start_ms": end_ms - days * 86_400_000 + (60_000 if days == 90 else 0),
                "end_ms": end_ms, "requests": 26 if days == 90 else 10 if days == 30 else 2,
                "observed_at": "2026-10-13T00:00:00+00:00",
                "deposits": [{"direction": "deposit", "asset": "BTC", "network": "BTC",
                              "amount": "0.1", "fee": "0", "status": 5,
                              "time_ms": 1_800_000_000_000,
                              "source_identity": "private-deposit-tx"}], "withdrawals": []}


class FakeMexcUidFail(FakeMexc):
    def account_uid(self):
        raise MexcReadOnlyError("Identidad MEXC no disponible")


class FakeMexcEthCatalog(FakeMexc):
    def transfers_recent(self, *, days, include_source_ids=False):
        result = super().transfers_recent(days=days, include_source_ids=include_source_ids)
        result["deposits"] = [{"direction": "deposit", "asset": "USDT", "network": "ERC20",
                               "amount": "25", "fee": "0", "status": 5,
                               "time_ms": 1_800_000_000_000,
                               "source_identity": "0x" + "ab" * 32}]
        return result

    def ethereum_token_contracts(self):
        return {"USDT": "0x" + "44" * 20}


class FakeMexcEthCatalogUnavailable(FakeMexcEthCatalog):
    def ethereum_token_contracts(self):
        raise MexcReadOnlyError("Catálogo temporalmente no disponible")


class FakeMexcEthBoth(FakeMexcEthCatalog):
    def transfers_recent(self, *, days, include_source_ids=False):
        result = super().transfers_recent(days=days, include_source_ids=include_source_ids)
        result["withdrawals"] = [{"direction": "withdrawal", "asset": "USDT", "network": "ERC20",
                                  "amount": "25.1", "fee": "0.1", "status": 7,
                                  "time_ms": 1_800_000_000_000, "transfer_type": 0,
                                  "source_identity": "withdrawal-id",
                                  "chain_identity": "0x" + "cd" * 32,
                                  "destination_identity": "0x" + "11" * 20}]
        return result

    def ethereum_token_contracts_by_direction(self):
        return {"deposit": {"USDT": "0x" + "44" * 20},
                "withdrawal": {"USDT": "0x" + "55" * 20}}


class FakeMexcEthBothCatalogUnavailable(FakeMexcEthBoth):
    def ethereum_token_contracts_by_direction(self):
        raise MexcReadOnlyError("Catálogo temporalmente no disponible")


def test_mexc_catalog_archives_direction_specific_withdrawal_contract(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "both-vault.db", mexc_factory=FakeMexcEthBoth))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "catalog both test secret"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET},
                       headers=headers).status_code == 200
    response = client.get("/real/exchanges/mexc/transfers", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["deposits"][0]["token_contract"] == "0x" + "44" * 20
    assert response.json()["withdrawals"][0]["token_contract"] == "0x" + "55" * 20
    archive_id = response.json()["archive"]["id"]
    archived = client.get(f"/real/exchanges/mexc/transfers/archive/{archive_id}", headers=headers)
    assert archived.status_code == 200
    assert archived.json()["withdrawals"][0]["token_contract"] == "0x" + "55" * 20


def test_mexc_withdrawal_archive_survives_catalog_failure_without_contract(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "both-unavailable.db",
                                          mexc_factory=FakeMexcEthBothCatalogUnavailable))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "catalog unavailable both secret"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET},
                       headers=headers).status_code == 200
    response = client.get("/real/exchanges/mexc/transfers", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["archive"]["withdrawal_count"] == 1
    assert "token_contract" not in response.json()["withdrawals"][0]


def test_mexc_catalog_contract_is_archived_with_eth_deposit_only(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "catalog-vault.db", mexc_factory=FakeMexcEthCatalog))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "catalog browser test secret"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    connected = client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET},
                            headers=headers)
    assert connected.status_code == 200, connected.text
    response = client.get("/real/exchanges/mexc/transfers", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["deposits"][0]["token_contract"] == "0x" + "44" * 20
    archive_id = response.json()["archive"]["id"]
    archived = client.get(f"/real/exchanges/mexc/transfers/archive/{archive_id}", headers=headers)
    assert archived.status_code == 200
    assert archived.json()["deposits"][0]["token_contract"] == "0x" + "44" * 20


def test_mexc_transfer_archive_remains_readable_when_catalog_fails(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "catalog-unavailable.db",
                                          mexc_factory=FakeMexcEthCatalogUnavailable))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "catalog unavailable test secret"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET},
                       headers=headers).status_code == 200
    response = client.get("/real/exchanges/mexc/transfers", headers=headers)
    assert response.status_code == 200, response.text
    assert "token_contract" not in response.json()["deposits"][0]
    assert response.json()["archive"]["deposit_count"] == 1


def test_mexc_session_is_readonly_ephemeral_and_separate(tmp_path):
    FakeMexc.calls = []
    vault = tmp_path / "vault.db"
    app = FastAPI()
    app.include_router(create_real_router(vault, mexc_factory=FakeMexc))
    client = TestClient(app)
    endpoint = "/real/exchanges/mexc"
    assert client.get(endpoint + "/status").status_code == 401
    assert client.get(endpoint + "/identity").status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "Principal"}, headers=headers).json()
    events = f"/real/accounts/{account['id']}/events"
    before = client.get(events, headers=headers).json()

    assert client.get(endpoint + "/status", headers=headers).json() == {
        "connected": False, "mode": "READ_ONLY", "credential_storage": "MEMORY_ONLY"}
    assert client.get(endpoint + "/balances", headers=headers).status_code == 409
    assert client.get(endpoint + "/identity", headers=headers).status_code == 409
    assert client.get(endpoint + "/trades/BTCUSDT", headers=headers).status_code == 409
    assert client.get(endpoint + "/transfers", headers=headers).status_code == 409
    assert client.post(endpoint + "/connect", json={"api_key": KEY, "secret_key": SECRET},
                       headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    response = client.post(endpoint + "/connect", json={"api_key": KEY, "secret_key": SECRET}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["accounting"] == "OBSERVED_NOT_IMPORTED"
    assert response.json()["balances"][0]["total"] == "0.6"
    assert KEY not in response.text and SECRET not in response.text
    assert client.get(endpoint + "/status", headers=headers).json()["connected"] is True
    identity = client.get(endpoint + "/identity", headers=headers)
    assert identity.status_code == 200 and identity.headers["cache-control"] == "no-store"
    assert identity.json() == {"view_uid_observed": True, "trade_uid_matched": False,
                              "storage": "MEMORY_ONLY", "local_identity_status": "NOT_RECORDED",
                              "local_stopped": False, "unresolved_intents": 0}
    assert "209302839" not in identity.text
    assert client.get(endpoint + "/identity", headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.get(endpoint + "/balances", headers=headers).json()["balances"][0]["free"] == "0.5"
    transfers = client.get(endpoint + "/transfers", headers=headers)
    assert transfers.status_code == 200 and transfers.headers["cache-control"] == "no-store"
    assert transfers.json()["identity_verified"] is False
    assert transfers.json()["deposits"][0]["amount"] == "0.1"
    assert "private-deposit-tx" not in transfers.text
    assert len(transfers.json()["deposits"][0]["source_ref"]) == 64
    transfer_id = transfers.json()["archive"]["id"]
    assert transfers.json()["archive"]["deposit_count"] == 1
    assert client.get(endpoint + "/transfers/archive", headers=headers).json()[0]["id"] == transfer_id
    assert client.get(endpoint + f"/transfers/archive/{transfer_id}", headers=headers).json()["deposits"][0]["amount"] == "0.1"
    assert client.get(endpoint + "/transfers?days=7", headers=headers).status_code == 200
    assert client.get(endpoint + "/transfers?days=30", headers=headers).json()["archive"]["requests"] == 10
    assert client.get(endpoint + "/transfers?days=90", headers=headers).json()["archive"]["requests"] == 26
    assert client.get(endpoint + "/transfers?days=91", headers=headers).status_code == 422
    trades = client.get(endpoint + "/trades/BTCUSDT", headers=headers)
    assert trades.status_code == 200 and trades.headers["cache-control"] == "no-store"
    assert trades.json()["trades"][0]["id"] == "fill-1"
    assert trades.json()["archive"]["new_fills"] == 1
    assert client.get(endpoint + "/trades/BTCUSDT", headers=headers).json()["archive"]["new_fills"] == 0
    window = client.post(endpoint + "/trades/BTCUSDT/window", json={"days": 7}, headers=headers)
    assert window.status_code == 200, window.text
    assert window.json()["archive"]["new_fills"] == 0
    assert window.json()["pages"] == 1 and "trades" not in window.json()
    assert client.post(endpoint + "/trades/BTCUSDT/window", json={"days": 31}, headers=headers).status_code == 422
    saved = client.get(endpoint + "/observations", headers=headers)
    assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
    assert saved.json()[0]["fill_count"] == 1 and saved.json()[0]["batch_count"] == 3
    reference = saved.json()[0]["account_ref"]
    assert client.get(endpoint + f"/observations/{reference}/BTCUSDT", headers=headers).json()["trades"][0]["id"] == "fill-1"
    coverage_url = endpoint + f"/observations/{reference}/BTCUSDT/coverage"
    coverage = client.get(coverage_url, params={"start_ms": 1_799_000_000_000,
                                               "end_ms": 1_801_000_000_000}, headers=headers)
    assert coverage.status_code == 200 and coverage.headers["cache-control"] == "no-store"
    assert coverage.json()["fully_covered"] is True
    assert coverage.json()["reconciled"] is False and coverage.json()["can_submit_real_order"] is False
    uncovered = client.get(coverage_url, params={"start_ms": 1_798_000_000_000,
                                                "end_ms": 1_801_000_000_000}, headers=headers)
    assert uncovered.json()["gaps"] == [{"start_ms": 1_798_000_000_000,
                                          "end_ms": 1_798_999_999_999}]
    assert client.get(coverage_url, params={"start_ms": 0, "end_ms": 1}, headers=headers).status_code == 422
    assert client.get(coverage_url, params={"start_ms": 1, "end_ms": 2}).status_code == 401
    assert client.get(endpoint + "/trades/BAD", headers=headers).status_code == 422
    assert client.get(events, headers=headers).json() == before
    assert KEY.encode() not in vault.read_bytes() and SECRET.encode() not in vault.read_bytes()
    assert b"209302839" not in vault.read_bytes()
    assert FakeMexc.calls == [(KEY, SECRET)]

    assert client.post(endpoint + "/disconnect", headers=headers).json() == {"connected": False}
    assert client.get(endpoint + "/identity", headers=headers).status_code == 409
    assert client.get(endpoint + "/balances", headers=headers).status_code == 409
    assert client.get(endpoint + "/trades/BTCUSDT", headers=headers).status_code == 409
    assert client.get(endpoint + "/transfers", headers=headers).status_code == 409
    assert client.post(endpoint + "/connect", json={"api_key": KEY, "secret_key": SECRET}, headers=headers).status_code == 200
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    assert client.get(endpoint + "/status", headers=headers).status_code == 401
    token2 = client.post("/real/vault/unlock", json={"passphrase": "long secret password"}).json()["token"]
    headers2 = {"X-Orbis-Vault-Token": token2}
    assert client.get(endpoint + "/status", headers=headers2).json()["connected"] is False
    assert client.get(events, headers=headers2).json() == before
    assert client.get(endpoint + "/observations", headers=headers2).json()[0]["fill_count"] == 1
    assert client.get(endpoint + f"/transfers/archive/{transfer_id}", headers=headers2).json()["deposits"][0]["amount"] == "0.1"

    reopened = FastAPI()
    reopened.include_router(create_real_router(vault, mexc_factory=FakeMexc))
    second = TestClient(reopened)
    token3 = second.post("/real/vault/unlock", json={"passphrase": "long secret password"}).json()["token"]
    assert second.get(endpoint + "/status", headers={"X-Orbis-Vault-Token": token3}).json()["connected"] is False
    assert second.get(endpoint + "/observations", headers={"X-Orbis-Vault-Token": token3}).json()[0]["fill_count"] == 1
    assert second.get(endpoint + f"/transfers/archive/{transfer_id}", headers={"X-Orbis-Vault-Token": token3}).json()["deposits"][0]["amount"] == "0.1"


def test_mexc_api_refuses_altered_archived_fill(tmp_path):
    path = tmp_path / "vault.db"
    app = FastAPI()
    app.include_router(create_real_router(path, mexc_factory=FakeMexc))
    client = TestClient(app)
    password = "archived fill integrity secret"
    token = client.post("/real/vault/create", json={"passphrase": password}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post("/real/exchanges/mexc/connect",
                       json={"api_key": KEY, "secret_key": SECRET}, headers=headers).status_code == 200
    assert client.get("/real/exchanges/mexc/trades/BTCUSDT", headers=headers).status_code == 200
    reference = client.get("/real/exchanges/mexc/observations", headers=headers).json()[0]["account_ref"]
    second = RealVault(path)
    second.unlock(password)
    with second.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE mexc_observed_fills SET quantity='0.75' WHERE trade_id='fill-1'")
        db.commit()
    second.lock()
    saved = client.get(f"/real/exchanges/mexc/observations/{reference}/BTCUSDT", headers=headers)
    assert saved.status_code == 422 and "integrity failed" in saved.text
    assert client.get("/real/exchanges/mexc/trades/BTCUSDT", headers=headers).status_code == 409


def test_uid_failure_keeps_readonly_connection_but_clears_previous_identity(tmp_path):
    access = RealAccess(tmp_path / "vault.db", mexc_factory=FakeMexcUidFail)
    token = access.unlock("long secret password", create=True)
    access.connect_mexc(token, KEY, SECRET)
    access._mexc_uid = "209302839"
    try:
        access.mexc_identity(token)
    except MexcReadOnlyError:
        pass
    else:
        raise AssertionError("Expected unavailable MEXC identity")
    assert access._mexc_uid is None
    assert access.mexc_status(token)["connected"] is True
    assert access.mexc_balances(token)["balances"][0]["free"] == "0.5"
    access.lock()
    assert access._mexc_uid is None


def test_view_identity_diagnoses_rotated_key_and_old_stop_without_uid_leak(tmp_path):
    access = RealAccess(tmp_path / "vault.db", mexc_factory=FakeMexc)
    token = access.unlock("long secret password", create=True)
    access.connect_mexc(token, KEY, SECRET)
    reference = hmac.new(SECRET.encode(), KEY.encode(), hashlib.sha256).hexdigest()
    bind_uid_for_spot_submission(access.vault, reference, "209302839")
    stop_spot_account(access.vault, reference, now=datetime.now(timezone.utc))
    same = access.mexc_identity(token)
    assert same["local_identity_status"] == "SAME_KEY"
    assert same["local_stopped"] is True
    access._mexc.account_uid = lambda: "987654321"
    changed = access.mexc_identity(token)
    assert changed["local_identity_status"] == "KEY_UID_CHANGED"
    assert changed["local_stopped"] is True
    access.connect_mexc(token, "rotated-view-key", "rotated-view-secret")
    rotated = access.mexc_identity(token)
    assert rotated["local_identity_status"] == "ROTATED_KEY_MATCH"
    assert rotated["local_stopped"] is True
    assert rotated["unresolved_intents"] == 0
    assert "209302839" not in str(rotated)
    assert reference not in str(rotated)
    with access.vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_mexc_uid_links"
        ).scalar_one() == 1
    access.lock()


def test_failed_connect_does_not_keep_a_previous_key(tmp_path):
    access = RealAccess(tmp_path / "vault.db", mexc_factory=FakeMexc)
    token = access.unlock("long secret password", create=True)
    access.connect_mexc(token, KEY, SECRET)
    try:
        access.connect_mexc(token, "rejected-key", SECRET)
    except MexcReadOnlyError:
        pass
    else:
        raise AssertionError("Expected rejected credentials")
    assert access.mexc_status(token)["connected"] is False
    access.connect_mexc(token, KEY, SECRET)
    access._deadline = time.monotonic() - 1
    try:
        access.mexc_status(token)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
    else:
        raise AssertionError("Expected expired vault token")
    assert access._mexc is None
    assert not access.vault.unlocked


def test_mexc_valuation_is_protected_readonly_and_never_books_portfolio(tmp_path):
    class FreshMexc(FakeMexc):
        def balances(self):
            return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "balances": [{"asset": "BTC", "free": "0.01", "locked": "0.005", "total": "0.015"},
                                 {"asset": "USDT", "free": "100", "locked": "0", "total": "100"}]}

    def quote(symbol):
        assert symbol == "BTCUSDT"
        # El evento ficticio debe ser anterior al `now` tomado por el motor.
        return {"symbol": symbol, "closeTime": int((datetime.now(timezone.utc) - timedelta(milliseconds=100)).timestamp() * 1000),
                "bidPrice": "100000", "askPrice": "100001", "lastPrice": "100000"}

    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db", mexc_factory=FreshMexc,
                                          mexc_quote_get=quote))
    client = TestClient(app)
    endpoint = "/real/exchanges/mexc/valuation"
    assert client.get(endpoint).status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "Principal"}, headers=headers).json()
    before = client.get(f"/real/accounts/{account['id']}/events", headers=headers).json()
    assert client.get(endpoint, headers=headers).status_code == 409
    client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET},
                headers=headers)
    response = client.get(endpoint + "?quote=USDT", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["complete"] is True
    assert response.json()["total_quote"] == "1600.000"
    assert KEY not in response.text and SECRET not in response.text
    assert client.get(endpoint, headers={**headers, "Origin": "https://outside.invalid"}).status_code == 403
    assert client.get(f"/real/accounts/{account['id']}/events", headers=headers).json() == before
    client.post("/real/exchanges/mexc/disconnect", headers=headers)
    assert client.get(endpoint, headers=headers).status_code == 409


def test_transfer_archive_rejects_view_uid_change_without_persisting_snapshot(tmp_path):
    class ChangingUidMexc(FakeMexc):
        def __init__(self, key, secret):
            super().__init__(key, secret)
            self.uid_reads = 0

        def account_uid(self):
            self.uid_reads += 1
            return "209302839" if self.uid_reads == 1 else "987654321"

    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db", mexc_factory=ChangingUidMexc))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": KEY, "secret_key": SECRET}, headers=headers).status_code == 200
    response = client.get("/real/exchanges/mexc/transfers", headers=headers)
    assert response.status_code == 409
    assert "987654321" not in response.text
    assert client.get("/real/exchanges/mexc/transfers/archive", headers=headers).json() == []
