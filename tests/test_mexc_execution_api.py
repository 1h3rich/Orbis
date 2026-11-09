"""API local: activación explícita y compra/venta usando transporte controlado."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import hmac
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.store import LabStore
from app.live_execution.journal import register_spot_policy
from app.portfolio.api import create_real_router
from tests import test_mexc_execution_state as fixtures
from tests.test_mexc_execution import ExchangeOrders
from tests.test_mexc_policy_drafts_api import definition, draft as limits_payload


class RealtimeClock:
    @property
    def value(self):
        return datetime.now(timezone.utc)

    def __call__(self):
        return self.value


class Probe:
    def __init__(self, *_):
        pass

    def account_uid(self):
        return fixtures.UID


@pytest.mark.parametrize("manual_only", [False, True], ids=["lab_version", "standalone_manual"])
def test_explicit_api_activation_and_real_order_route_use_exactly_one_controlled_post(
        tmp_path, monkeypatch, manual_only):
    monkeypatch.setattr(fixtures, "Clock", RealtimeClock)
    monkeypatch.setattr(fixtures, "ACCOUNT", hmac.new(
        b"fixture-secret", b"fixture-key", hashlib.sha256).hexdigest())
    vault, policy, account, venue, clock = fixtures.setup(tmp_path)
    lab_path = None
    draft_id = str(uuid4())
    if not manual_only:
        lab_path = tmp_path / "lab.db"
        version = LabStore(lab_path).create_strategy("Prueba manual", definition())["version_id"]
        draft = replace(policy, policy_id=draft_id, strategy_version_id=version,
                        enabled=False, reconciliation_anchor_id=None, allow_risk_reducing_exits=False)
        register_spot_policy(vault, draft, now=clock())
    vault.lock()
    trader = ExchangeOrders(venue, clock)
    app = FastAPI()
    app.include_router(create_real_router(
        tmp_path / "real.db", lab_path=lab_path, mexc_factory=lambda *_: venue,
        mexc_trade_factory=Probe, mexc_live_factory=lambda *_: trader,
        mexc_quote_get=venue.quote_get,
        mexc_rules_fetch=lambda symbol, now: venue.rules_fetch(symbol, now)))
    client = TestClient(app)
    account_path = f"/real/accounts/{account}"
    activation = f"{account_path}/mexc-policy-drafts/{draft_id}/activate"
    body = {"request_id": str(uuid4()), "anchor_id": policy.reconciliation_anchor_id,
            "confirmation": "ACTIVAR_ORDENES_REALES"}
    assert client.post(activation, json=body).status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase":fixtures.PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    assert client.post(activation, headers=headers, json=body).status_code == 409
    if manual_only:
        limits_path = f"{account_path}/mexc-manual-limits"
        candidate = limits_payload(None, request_id=draft_id)
        del candidate["strategy_version_id"]
        assert client.get(limits_path).status_code == 401
        assert client.post(limits_path, headers=headers, json=candidate).status_code == 409
    assert client.post("/real/exchanges/mexc/connect", headers=headers,
                       json={"api_key":"fixture-key", "secret_key":"fixture-secret"}).status_code == 200
    if manual_only:
        assert client.post(limits_path, headers={**headers,"Origin":"https://outside.invalid"},
                           json=candidate).status_code == 403
        assert client.post(limits_path, headers=headers, json={**candidate, "strategy_version_id":"arbitrary"}).status_code == 422
        assert client.post(limits_path, headers=headers, json={**candidate, "max_order_quote":"NaN"}).status_code == 422
        saved = client.post(limits_path, headers=headers, json=candidate)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["manual_only"] and saved.json()["strategy_version_id"] is None
        assert not saved.json()["enabled"] and not saved.json()["execution_enabled"]
        assert client.post(limits_path, headers=headers, json=candidate).json() == saved.json()
        assert client.post(limits_path, headers=headers, json={**candidate, "max_order_quote":"101"}).status_code == 409
        assert client.get(limits_path, headers=headers).json() == [saved.json()]
        assert client.get(f"{account_path}/mexc-policy-drafts", headers=headers).json() == []
        assert client.post(activation, headers=headers, json=body).status_code == 409
        assert trader.posts == []
    assert client.post("/real/exchanges/mexc/trade/connect", headers=headers,
                       json={"api_key":"fixture-trade", "secret_key":"fixture-secret"}).status_code == 200
    assert client.post(activation, headers={**headers,"Origin":"https://outside.invalid"}, json=body).status_code == 403
    assert client.post(activation, headers=headers, json={**body,"confirmation":""}).status_code == 422
    activated = client.post(activation, headers=headers, json=body)
    assert activated.status_code == 200, activated.text
    assert activated.json()["mode"] == "MANUAL" and activated.json()["bot_active"] is False
    assert activated.json().get("manual_only", False) == manual_only
    assert trader.posts == []
    active_id = activated.json()["id"]
    orders_url = f"/real/accounts/{account}/mexc-trading-sessions/{active_id}/orders"
    order = {"request_id":str(uuid4()), "side":"BUY", "quantity_base":"0.001",
             "limit_price_quote":"65000", "confirmation":"ENVIAR_ORDEN_REAL"}
    assert client.post(orders_url, headers=headers, json={**order,"confirmation":""}).status_code == 422
    first = client.post(orders_url, headers=headers, json=order)
    assert first.status_code == 200, first.text
    assert first.json()["journal_status"] == "RESERVATION_CLOSED", first.text
    assert first.headers["cache-control"] == "no-store"
    assert client.post(orders_url, headers=headers, json=order).json()["replayed_without_post"]
    assert len(trader.posts) == 1
    sale = client.post(orders_url, headers=headers, json={**order, "request_id":str(uuid4()),
                                                       "side":"SELL", "quantity_base":"0.0009"})
    assert sale.status_code == 200 and sale.json()["journal_status"] == "RESERVATION_CLOSED", sale.text
    assert len(trader.posts) == 2
    sessions = client.get(f"/real/accounts/{account}/mexc-trading-sessions", headers=headers)
    assert any(item["id"] == active_id for item in sessions.json())
    assert client.post(orders_url, headers=headers, json={**order,"quantity_base":"NaN"}).status_code == 409
    if manual_only:
        stopped = client.post(f"{account_path}/mexc-stop", headers=headers, json={"confirmation":"DETENER"})
        assert stopped.status_code == 200 and stopped.json()["stopped"]
        assert client.post(orders_url, headers=headers,
                           json={**order,"request_id":str(uuid4())}).status_code == 409
        checked = client.post(f"{account_path}/mexc-spot-parity/observations", headers=headers,
                              json={"request_id":str(uuid4())})
        assert checked.status_code == 200 and checked.json()["saved"], checked.text
        resumed = client.post(f"{account_path}/mexc-policy-drafts/{draft_id}/resume", headers=headers, json={
            "request_id":str(uuid4()), "anchor_id":checked.json()["id"],
            "confirmation":"REABRIR_OPERATIVA_REAL"})
        assert resumed.status_code == 200 and resumed.json()["manual_only"], resumed.text
        assert len(trader.posts) == 2
        assert not (tmp_path / "lab.db").exists()
    client.post("/real/vault/lock", headers=headers)
    assert client.post(orders_url, headers=headers, json=order).status_code == 401
