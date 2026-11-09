"""La conexión Trade verifica identidad, pero no habilita órdenes REAL."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.mexc_trade_identity import MexcTradeIdentityError
from app.portfolio.api import create_real_router


VIEW_KEY = "fixture-view-key"
VIEW_SECRET = "fixture-view-secret"
TRADE_KEY = "fixture-trade-key"
TRADE_SECRET = "fixture-trade-secret"


class View:
    uid = "209302839"

    def __init__(self, api_key, secret_key):
        assert (api_key, secret_key) == (VIEW_KEY, VIEW_SECRET)

    def balances(self):
        return {"balances": [], "source": "fixture"}

    def account_uid(self):
        return self.uid


class TradeProbe:
    calls = []
    uid = "209302839"
    reject = False

    def __init__(self, api_key, secret_key):
        assert (api_key, secret_key) == (TRADE_KEY, TRADE_SECRET)
        self.calls.append("created")

    def account_uid(self):
        self.calls.append("get-only-identity")
        if self.reject:
            raise MexcTradeIdentityError("Permisos Trade no verificables")
        return self.uid


def setup(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db", mexc_factory=View,
                                          mexc_trade_factory=TradeProbe))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "strong test passphrase"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    return client, headers


def connect_view(client, headers):
    result = client.post("/real/exchanges/mexc/connect", headers=headers,
                         json={"api_key": VIEW_KEY, "secret_key": VIEW_SECRET})
    assert result.status_code == 200, result.text


def connect_trade(client, headers):
    return client.post("/real/exchanges/mexc/trade/connect", headers=headers,
                       json={"api_key": TRADE_KEY, "secret_key": TRADE_SECRET})


def test_trade_requires_view_and_same_uid_then_clears_on_disconnect_and_lock(tmp_path):
    TradeProbe.calls = []
    TradeProbe.uid = View.uid = "209302839"
    TradeProbe.reject = False
    client, headers = setup(tmp_path)
    endpoint = "/real/exchanges/mexc/trade"
    assert client.get(endpoint + "/status").status_code == 401
    assert connect_trade(client, headers).status_code == 409
    assert TradeProbe.calls == []
    connect_view(client, headers)
    assert client.post(endpoint + "/connect",
                       headers={**headers, "Origin": "https://untrusted.example"},
                       json={"api_key": TRADE_KEY, "secret_key": TRADE_SECRET}).status_code == 403
    assert TradeProbe.calls == []
    response = connect_trade(client, headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"verified": True, "mode": "GET_ONLY_VERIFICATION",
                               "execution_enabled": False,
                               "verification_scope": "AT_CONNECTION_ONLY",
                               "credential_storage": "MEMORY_ONLY"}
    assert TradeProbe.calls == ["created", "get-only-identity"]
    assert all(secret not in response.text for secret in (VIEW_KEY, VIEW_SECRET, TRADE_KEY, TRADE_SECRET))
    identity = client.get("/real/exchanges/mexc/identity", headers=headers)
    assert identity.status_code == 200, identity.text
    assert client.get(endpoint + "/status", headers=headers).json()["verified"] is True
    assert client.post(endpoint + "/disconnect", headers=headers).json()["verified"] is False
    assert connect_trade(client, headers).json()["verified"] is True
    connect_view(client, headers)
    assert client.get(endpoint + "/status", headers=headers).json()["verified"] is False
    assert connect_trade(client, headers).json()["verified"] is True
    assert client.post("/real/exchanges/mexc/disconnect", headers=headers).json() == {"connected": False}
    assert client.get(endpoint + "/status", headers=headers).json()["verified"] is False
    connect_view(client, headers)
    assert connect_trade(client, headers).json()["verified"] is True
    client.post("/real/vault/lock", headers=headers)
    assert client.get(endpoint + "/status", headers=headers).status_code == 401
    reopened = client.post("/real/vault/unlock", json={"passphrase": "strong test passphrase"}).json()
    assert client.get(endpoint + "/status", headers={"X-Orbis-Vault-Token": reopened["token"]}).json()["verified"] is False


def test_trade_rejects_other_uid_and_permission_failure_without_retaining_key(tmp_path):
    TradeProbe.calls = []
    TradeProbe.uid = "111111"
    TradeProbe.reject = False
    client, headers = setup(tmp_path)
    connect_view(client, headers)
    wrong = connect_trade(client, headers)
    assert wrong.status_code == 422
    assert "cuentas distintas" in wrong.json()["detail"]
    assert client.get("/real/exchanges/mexc/trade/status", headers=headers).json()["verified"] is False
    TradeProbe.uid = View.uid
    TradeProbe.reject = True
    denied = connect_trade(client, headers)
    assert denied.status_code == 422
    assert client.get("/real/exchanges/mexc/trade/status", headers=headers).json()["verified"] is False
    assert all(secret not in denied.text for secret in (VIEW_KEY, VIEW_SECRET, TRADE_KEY, TRADE_SECRET))


def test_trade_reconnect_requires_fresh_view_identity(tmp_path):
    TradeProbe.calls = []
    TradeProbe.uid = View.uid = "209302839"
    TradeProbe.reject = False
    client, headers = setup(tmp_path)
    connect_view(client, headers)
    assert connect_trade(client, headers).json()["verified"] is True
    View.uid = "888888"
    assert client.get("/real/exchanges/mexc/identity", headers=headers).status_code == 200
    assert client.get("/real/exchanges/mexc/trade/status", headers=headers).json()["verified"] is False
    assert connect_trade(client, headers).status_code == 422
    assert client.get("/real/exchanges/mexc/trade/status", headers=headers).json()["verified"] is False
    View.uid = "209302839"
