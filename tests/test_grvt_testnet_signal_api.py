"""La entrada HTTP testnet nunca se confunde con la conexión GRVT View/mainnet."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from app.live_execution.grvt_testnet_journal import prepare_testnet_order
from app.live_execution.grvt_testnet_order import (
    GrvtTestnetLimits, GrvtTestnetOrderError, GrvtTestnetOrderObservation,
)
from app.portfolio.api import create_real_router
from app.portfolio.models import GrvtTestnetOrderIntent
from app.portfolio.vault import RealVault


def _body():
    return {
        "api_key": "TESTNET_KEY_ONLY_123", "session_id": "paper-session",
        "action_request_id": "paper-action", "sub_account_id": "12345",
        "instrument": "BTC_USDT_Perp", "max_order_notional": "100",
        "confirmation": "ENVIAR_GRVT_TESTNET",
        "order": {
            "sub_account_id": "12345", "is_market": False,
            "time_in_force": "IMMEDIATE_OR_CANCEL", "post_only": False,
            "reduce_only": False,
            "legs": [{"instrument": "BTC_USDT_Perp", "size": "0.001",
                      "limit_price": "50000", "is_buying_asset": True}],
            "signature": {"signer": "0x" + "a" * 40, "r": "0x" + "1" * 64,
                          "s": "0x" + "2" * 64, "v": 27, "nonce": 123,
                          "expiration": str(time.time_ns() + 60_000_000_000),
                          "chain_id": "326"},
            "metadata": {"client_order_id": client_id_for_signal("paper-session", "paper-action")},
            "builder": "0x" + "a" * 40, "builder_fee": "0",
        },
    }


def test_grvt_testnet_signal_route_requires_vault_config_and_confirmation(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    body = _body()
    url = "/real/exchanges/grvt/testnet/signals/submit"
    assert client.post(url, json=body).status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post(url, json=body, headers=headers).status_code == 503
    assert client.post(url, json={**body, "confirmation": "TRADE_MAINNET"},
                       headers=headers).status_code == 422


def test_grvt_testnet_signal_route_has_server_cap_and_no_secret_echo(tmp_path, monkeypatch):
    import app.portfolio.api as real_api

    calls = []

    class FakePaper:
        def __init__(self, path, *, allow_create):
            assert allow_create is False

        @contextmanager
        def connect(self):
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: (1,)))

    monkeypatch.setattr(real_api, "PaperPerpStore", FakePaper)

    def factory(key):
        calls.append(("login", key))
        return object()

    def submit(paper, lab, vault, client, order, limits, *, session_id, action_request_id):
        calls.append(("submit", str(limits.max_order_notional)))
        return (SimpleNamespace(session_id=session_id, action_request_id=action_request_id),
                SimpleNamespace(status="ACKNOWLEDGED", external_status=None))

    app = FastAPI()
    app.include_router(create_real_router(
        tmp_path / "vault.db", lab_path=tmp_path / "lab.db",
        paper_perp_path=tmp_path / "paper.db", grvt_testnet_factory=factory,
        grvt_testnet_signal_submitter=submit,
    ))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    url = "/real/exchanges/grvt/testnet/signals/submit"
    body = _body()
    for candidate in ({**body, "max_order_notional": "101"},
                      {**body, "order": {**body["order"], "is_market": True}},
                      {**body, "order": {**body["order"], "metadata": {
                          "client_order_id": str(int(body["order"]["metadata"]["client_order_id"]) + 1)}}},
                      {**body, "order": {**body["order"], "signature": {
                          **body["order"]["signature"], "chain_id": "325"}}}):
        assert client.post(url, json=candidate, headers=headers).status_code == 422
    assert calls == []
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["network"] == "TESTNET"
    assert response.json()["mainnet_order_sent"] is False
    assert response.json()["execution_unverified"] is True
    assert response.json()["client_order_id"] == body["order"]["metadata"]["client_order_id"]
    assert body["api_key"] not in response.text
    assert calls == [("login", body["api_key"]), ("submit", "100")]


def test_grvt_testnet_signal_route_missing_action_never_logs_in(tmp_path, monkeypatch):
    import app.portfolio.api as real_api

    class FakePaper:
        def __init__(self, path, *, allow_create):
            pass

        @contextmanager
        def connect(self):
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None))

    monkeypatch.setattr(real_api, "PaperPerpStore", FakePaper)
    app = FastAPI()
    app.include_router(create_real_router(
        tmp_path / "vault.db", lab_path=tmp_path / "lab.db",
        paper_perp_path=tmp_path / "paper.db",
        grvt_testnet_factory=lambda key: (_ for _ in ()).throw(AssertionError("external login")),
    ))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    response = client.post("/real/exchanges/grvt/testnet/signals/submit", json=_body(),
                           headers={"X-Orbis-Vault-Token": token})
    assert response.status_code == 409
    assert "TESTNET_KEY_ONLY_123" not in response.text


def test_grvt_testnet_stop_is_durable_and_does_not_claim_external_cancel(tmp_path):
    path = tmp_path / "vault.db"
    app = FastAPI()
    app.include_router(create_real_router(path))
    client = TestClient(app)
    url = "/real/exchanges/grvt/testnet/subaccounts/12345/stop"
    assert client.post(url, json={"confirmation": "DETENER_GRVT_TESTNET"}).status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post(url, json={"confirmation": "DETENER"}, headers=headers).status_code == 422
    result = client.post(url, json={"confirmation": "DETENER_GRVT_TESTNET"}, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["stopped"] is True
    assert result.json()["external_orders_cancelled"] is False
    assert client.get("/real/exchanges/grvt/testnet/recovery?sub_account_id=12345",
                      headers=headers).json()["stopped"] is True
    reopened = FastAPI()
    reopened.include_router(create_real_router(path))
    reopened_client = TestClient(reopened)
    reopened_token = reopened_client.post(
        "/real/vault/unlock", json={"passphrase": "a robust local password"},
    ).json()["token"]
    reopened_report = reopened_client.get(
        "/real/exchanges/grvt/testnet/recovery?sub_account_id=12345",
        headers={"X-Orbis-Vault-Token": reopened_token},
    )
    assert reopened_report.status_code == 200
    assert reopened_report.json()["stopped"] is True


def _seed_claimed(path, body):
    vault = RealVault(path)
    vault.unlock("a robust local password")
    limits = GrvtTestnetLimits(body["sub_account_id"], body["instrument"], Decimal("100"))
    prepare_testnet_order(vault, body["order"], limits,
                          now=datetime.now(timezone.utc), now_ns=time.time_ns())
    with vault.session() as db:
        row = db.get(GrvtTestnetOrderIntent, body["order"]["metadata"]["client_order_id"])
        row.status = "POST_CLAIMED"
        db.commit()
    vault.lock()


def test_grvt_testnet_audit_requires_known_post_before_external_login(tmp_path):
    calls = []

    def factory(key):
        calls.append("login")
        return object()

    def auditor(*args):
        calls.append("audit")
        return SimpleNamespace(
            record=SimpleNamespace(status="OBSERVED", external_status="FILLED",
                                   traded_size=Decimal("0.001")),
            evidence=SimpleNamespace(id="audit-local-id"),
        )

    path = tmp_path / "vault.db"
    app = FastAPI()
    app.include_router(create_real_router(path, grvt_testnet_factory=factory,
                                          grvt_testnet_auditor=auditor))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    body = _body()
    request = {"api_key": body["api_key"], "sub_account_id": body["sub_account_id"],
               "client_order_id": body["order"]["metadata"]["client_order_id"],
               "confirmation": "AUDITAR_GRVT_TESTNET"}
    url = "/real/exchanges/grvt/testnet/orders/audit"
    assert client.post(url, json=request).status_code == 401
    assert client.post(url, json=request, headers=headers).status_code == 409
    assert calls == []
    _seed_claimed(path, body)
    result = client.post(url, json=request, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["audit_id"] == "audit-local-id"
    assert result.json()["traded_size"] == "0.001"
    assert result.json()["execution_unverified"] is True
    assert body["api_key"] not in result.text
    assert calls == ["login", "audit"]


def test_grvt_testnet_stop_cancel_stops_before_login_and_never_retries(tmp_path):
    path = tmp_path / "vault.db"
    body = _body()

    class FakeTestnet:
        calls = 0

        def cancel_by_client_id(self, sub_account_id, client_order_id):
            self.calls += 1
            return GrvtTestnetOrderObservation(
                sub_account_id, client_order_id, "0x123", "CANCELLED",
                "BTC_USDT_Perp", "BUY", "0.001", "0", "50000",
                str(time.time_ns()), create_time_ns=str(time.time_ns() - 1_000_000),
            )

    fake = FakeTestnet()
    audit_calls = []

    def cancel_auditor(vault, external, sub_account_id, client_order_id):
        audit_calls.append((sub_account_id, client_order_id))
        return SimpleNamespace(
            cancellation=SimpleNamespace(status="AUDITED"),
            audit=SimpleNamespace(
                record=SimpleNamespace(status="OBSERVED", external_status="CANCELLED",
                                       traded_size=Decimal("0")),
                evidence=SimpleNamespace(id="cancel-audit-id"),
            ),
        )

    app = FastAPI()
    app.include_router(create_real_router(
        path, grvt_testnet_factory=lambda key: fake,
        grvt_testnet_cancel_auditor=cancel_auditor,
    ))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    _seed_claimed(path, body)
    request = {"api_key": body["api_key"], "sub_account_id": body["sub_account_id"],
               "client_order_id": body["order"]["metadata"]["client_order_id"],
               "confirmation": "DETENER_Y_CANCELAR_GRVT_TESTNET"}
    url = "/real/exchanges/grvt/testnet/orders/stop-cancel"
    assert client.post(url, json={**request, "confirmation": "CANCELAR"},
                       headers=headers).status_code == 422
    result = client.post(url, json=request, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["stopped"] is True
    assert result.json()["external_status"] == "CANCELLED"
    assert fake.calls == 1
    assert client.post(url, json=request, headers=headers).status_code == 409
    assert fake.calls == 1
    review = client.post("/real/exchanges/grvt/testnet/orders/audit", json={
        **request, "confirmation": "AUDITAR_GRVT_TESTNET",
    }, headers=headers)
    assert review.status_code == 200, review.text
    assert review.json()["cancel_status"] == "AUDITED"
    assert audit_calls == [(body["sub_account_id"], request["client_order_id"])]
    report = client.get("/real/exchanges/grvt/testnet/recovery?sub_account_id=12345",
                        headers=headers)
    assert report.status_code == 200 and report.json()["stopped"] is True


def test_grvt_testnet_stop_cancel_auth_failure_still_stops(tmp_path):
    path = tmp_path / "vault.db"
    body = _body()
    app = FastAPI()
    app.include_router(create_real_router(
        path, grvt_testnet_factory=lambda key: (_ for _ in ()).throw(
            GrvtTestnetOrderError("testnet auth failed")),
    ))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    _seed_claimed(path, body)
    request = {"api_key": body["api_key"], "sub_account_id": body["sub_account_id"],
               "client_order_id": body["order"]["metadata"]["client_order_id"],
               "confirmation": "DETENER_Y_CANCELAR_GRVT_TESTNET"}
    result = client.post("/real/exchanges/grvt/testnet/orders/stop-cancel",
                         json=request, headers=headers)
    assert result.status_code == 409
    assert "No se envió cancelación" in result.json()["detail"]
    assert body["api_key"] not in result.text
    report = client.get("/real/exchanges/grvt/testnet/recovery?sub_account_id=12345",
                        headers=headers)
    assert report.status_code == 200 and report.json()["stopped"] is True
