"""La observación GRVT requiere bóveda y no se importa al libro REAL."""

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.grvt_readonly import GrvtReadOnlyError
from app.portfolio.api import RealAccess, create_real_router


KEY = "fixture-grvt-view-key"


class FakeGrvt:
    calls = []

    def __init__(self, key):
        self.calls.append(key)
        if key == "bad-grvt-key":
            raise GrvtReadOnlyError("GRVT rechazó el acceso a la cuenta")

    def snapshot(self):
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "permission_verified": False, "observed_at": "2026-10-13T00:00:00+00:00",
                "total_equity_usd": "100.123456789012345678", "trading_equity_usd": "90",
                "spot_balances_aggregated": [{"asset": "USDT", "balance": "10"}],
                "trading_accounts": [{"sub_account_id": "123", "settle_currency": "USDT",
                                      "total_equity": "90", "maintenance_margin": "1",
                                      "unrealized_pnl": "2", "balances": [], "event_time_ns": "1", "fresh": True,
                                      "positions": [{"instrument": "BTC_USDT_Perp", "side": "LONG",
                                                     "size": "0.1", "entry_price": "100",
                                                     "mark_price": "110", "unrealized_pnl": "1"}]}]}

    def open_orders(self):
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "complete_for_execution": False, "in_flight_orders_excluded_possible": True,
                "orders": [{"sub_account_id": "123", "order_id": "0x123", "status": "OPEN"}]}

    def order(self, sub_account_id, client_order_id):
        assert (sub_account_id, client_order_id) == ("123", "9223372036854775808")
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "complete_for_execution": False,
                "order": {"sub_account_id": sub_account_id, "client_order_id": client_order_id,
                          "status": "FILLED"}}

    def order_fills(self, sub_account_id, client_order_id):
        assert (sub_account_id, client_order_id) == ("123", "9223372036854775808")
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": "2026-10-15T12:00:00+00:00", "account_ref": "a" * 64,
                "history_pages_drained": True, "fill_sizes_match_order_state": True,
                "complete_for_execution": False,
                "order": {"sub_account_id": sub_account_id, "client_order_id": client_order_id,
                          "order_id": "0x123", "status": "FILLED", "reduce_only": False,
                          "create_time_ns": "1790000000000000000",
                          "update_time_ns": "1790000000000000001",
                          "legs": [{"instrument": "BTC_USDT_Perp", "side": "BUY",
                                    "size": "0.1", "traded_size": "0.1", "limit_price": "65000"}]},
                "fills": [{"trade_id": "trade-1", "event_time_ns": "1790000000000000001",
                           "instrument": "BTC_USDT_Perp", "side": "BUY", "role": "TAKER",
                           "size": "0.1", "price": "65000", "fee": "0.12",
                           "builder_fee": "0", "fee_currency": "USDT", "realized_pnl": "1.5"}]}


def test_grvt_session_is_memory_only_separate_and_revoked_with_vault(tmp_path):
    FakeGrvt.calls = []
    vault = tmp_path / "real-vault.db"
    app = FastAPI()
    app.include_router(create_real_router(vault, grvt_factory=FakeGrvt))
    client = TestClient(app)
    endpoint = "/real/exchanges/grvt"
    assert client.get(endpoint + "/status").status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "Principal"}, headers=headers).json()
    events = f"/real/accounts/{account['id']}/events"
    before = client.get(events, headers=headers).json()
    assert client.get(endpoint + "/status", headers=headers).json() == {
        "connected": False, "mode": "READ_ONLY_CLIENT", "permission_verified": False,
        "credential_storage": "MEMORY_ONLY"}
    assert client.get(endpoint + "/snapshot", headers=headers).status_code == 409
    assert client.get(endpoint + "/open-orders", headers=headers).status_code == 409
    assert client.get(endpoint + "/order", params={"sub_account_id": "123",
        "client_order_id": "9223372036854775808"}, headers=headers).status_code == 409
    assert client.get(endpoint + "/order-fills", params={"sub_account_id": "123",
        "client_order_id": "9223372036854775808"}, headers=headers).status_code == 409
    assert client.get(endpoint + "/evidence", headers=headers).status_code == 200
    assert client.post(endpoint + "/connect", json={"api_key": KEY}, headers={
        **headers, "Origin": "https://evil.test"}).status_code == 403
    connected = client.post(endpoint + "/connect", json={"api_key": KEY}, headers=headers)
    assert connected.status_code == 200 and connected.headers["cache-control"] == "no-store"
    assert connected.json()["trading_accounts"][0]["positions"][0]["side"] == "LONG"
    assert KEY not in connected.text
    assert client.get(endpoint + "/snapshot", headers=headers).json()["accounting"] == "OBSERVED_NOT_IMPORTED"
    open_orders = client.get(endpoint + "/open-orders", headers=headers)
    assert open_orders.status_code == 200 and open_orders.headers["cache-control"] == "no-store"
    assert open_orders.json()["complete_for_execution"] is False
    order = client.get(endpoint + "/order", params={"sub_account_id": "123",
        "client_order_id": "9223372036854775808"}, headers=headers)
    assert order.status_code == 200 and order.headers["cache-control"] == "no-store"
    assert order.json()["order"]["status"] == "FILLED"
    fills = client.get(endpoint + "/order-fills", params={"sub_account_id": "123",
        "client_order_id": "9223372036854775808"}, headers=headers)
    assert fills.status_code == 200 and fills.headers["cache-control"] == "no-store"
    assert fills.json()["fill_sizes_match_order_state"] is True
    archived = client.post(endpoint + "/order-fills/archive", json={
        "sub_account_id": "123", "client_order_id": "9223372036854775808"}, headers=headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archive"]["archived"] is True
    again = client.post(endpoint + "/order-fills/archive", json={
        "sub_account_id": "123", "client_order_id": "9223372036854775808"}, headers=headers)
    assert again.status_code == 200 and again.json()["archive"]["archived"] is False
    evidence = client.get(endpoint + "/evidence", headers=headers)
    assert evidence.status_code == 200 and len(evidence.json()) == 1
    evidence_id = evidence.json()[0]["id"]
    assert client.get(endpoint + "/evidence/" + evidence_id, headers=headers).json()["fills"][0]["trade_id"] == "trade-1"
    assert client.get(endpoint + "/order", params={"sub_account_id": "bad",
        "client_order_id": "9223372036854775808"}, headers=headers).status_code == 422
    assert client.get(events, headers=headers).json() == before
    assert KEY.encode() not in vault.read_bytes()
    assert FakeGrvt.calls == [KEY]
    assert client.post(endpoint + "/disconnect", headers=headers).json() == {"connected": False}
    assert client.get(endpoint + "/snapshot", headers=headers).status_code == 409
    assert client.get(endpoint + "/open-orders", headers=headers).status_code == 409
    assert client.get(endpoint + "/order", params={"sub_account_id": "123",
        "client_order_id": "9223372036854775808"}, headers=headers).status_code == 409
    assert client.get(endpoint + "/order-fills", params={"sub_account_id": "123",
        "client_order_id": "9223372036854775808"}, headers=headers).status_code == 409
    assert client.post(endpoint + "/connect", json={"api_key": KEY}, headers=headers).status_code == 200
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    token2 = client.post("/real/vault/unlock", json={"passphrase": "long secret password"}).json()["token"]
    assert client.get(endpoint + "/status", headers={"X-Orbis-Vault-Token": token2}).json()["connected"] is False
    assert len(client.get(endpoint + "/evidence", headers={"X-Orbis-Vault-Token": token2}).json()) == 1


def test_failed_reconnect_and_idle_expiry_discard_grvt_cookie(tmp_path):
    access = RealAccess(tmp_path / "vault.db", grvt_factory=FakeGrvt)
    token = access.unlock("long secret password", create=True)
    access.connect_grvt(token, KEY)
    try:
        access.connect_grvt(token, "bad-grvt-key")
    except GrvtReadOnlyError:
        pass
    else:
        raise AssertionError("Se esperaba rechazo")
    assert access.grvt_status(token)["connected"] is False
    access.connect_grvt(token, KEY)
    access._deadline = time.monotonic() - 1
    try:
        access.grvt_status(token)
    except Exception as error:
        assert getattr(error, "status_code", None) == 401
    else:
        raise AssertionError("Se esperaba caducidad")
    assert access._grvt is None and not access.vault.unlocked
