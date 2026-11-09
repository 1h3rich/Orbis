"""Acceso local, liberación sin View y reapertura confirmada tras STOP."""

from dataclasses import replace
from decimal import Decimal
import hashlib
import hmac
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.store import LabStore
from app.live_execution.journal import begin_spot_submission, register_spot_policy, reserve_spot_intent
from app.portfolio.api import RealAccess, create_real_router
from app.portfolio.vault import RealVault
from tests import test_mexc_execution_state as fixtures
from tests.mexc_cancel_fixture import CancelableExchangeOrders, controlled_view
from tests.test_mexc_execution import ExchangeOrders
from tests.test_mexc_execution_api import RealtimeClock, Probe
from tests.test_mexc_policy_drafts_api import definition


def setup_api(tmp_path, monkeypatch, *, trader_factory=ExchangeOrders, **balances):
    monkeypatch.setattr(fixtures, "Clock", RealtimeClock)
    monkeypatch.setattr(fixtures, "ACCOUNT", hmac.new(b"fixture-secret", b"fixture-key", hashlib.sha256).hexdigest())
    vault, policy, account, venue, clock = fixtures.setup(tmp_path, **balances)
    lab_path = tmp_path / "lab.db"
    version = LabStore(lab_path).create_strategy("Límites de recuperación", definition())["version_id"]
    draft = replace(policy, policy_id=str(uuid4()), strategy_version_id=version, enabled=False,
                    reconciliation_anchor_id=None, allow_risk_reducing_exits=False)
    register_spot_policy(vault, draft, now=clock())
    vault.lock()
    trader = trader_factory(venue, clock)
    app = FastAPI()
    app.include_router(create_real_router(
        tmp_path / "real.db", lab_path=lab_path, mexc_factory=lambda key,secret: controlled_view(venue,key,secret),
        mexc_trade_factory=Probe, mexc_live_factory=lambda *_: trader,
        mexc_quote_get=venue.quote_get,
        mexc_rules_fetch=lambda symbol, now: venue.rules_fetch(symbol, now)))
    client = TestClient(app)
    token = client.post("/real/vault/unlock", json={"passphrase": fixtures.PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    return client, headers, vault, policy, draft, account, venue, trader, clock


def connect(client, headers):
    assert client.post("/real/exchanges/mexc/connect", headers=headers,
                       json={"api_key":"fixture-key", "secret_key":"fixture-secret"}).status_code == 200
    assert client.post("/real/exchanges/mexc/trade/connect", headers=headers,
                       json={"api_key":"fixture-trade", "secret_key":"fixture-secret"}).status_code == 200


def new_anchor(client, headers, account):
    response = client.post(f"/real/accounts/{account}/mexc-spot-parity/observations", headers=headers,
                           json={"request_id":str(uuid4())})
    assert response.status_code == 200 and response.json()["saved"], response.text
    return response.json()["id"]


def reserve(vault, policy, venue, clock, *, submitted=False):
    vault.unlock(fixtures.PASSWORD)
    state = fixtures.inputs(vault, policy, venue, clock).state
    intent = fixtures.buy(policy, clock)
    reserve_spot_intent(vault, intent, state, now=clock())
    if submitted:
        begin_spot_submission(vault, intent, state, now=clock())
    vault.lock()
    return intent


def test_manual_order_review_reads_risk_and_never_reserves_or_posts(tmp_path, monkeypatch):
    from app.portfolio.models import RealLiveSpotIntent, RealMexcOrderTestAttempt

    client, headers, vault, policy, draft, account, venue, trader, clock = setup_api(tmp_path, monkeypatch)
    base = f"/real/accounts/{account}"
    path = f"{base}/mexc-trading-sessions/{policy.policy_id}/orders/review"
    buy = {"side":"BUY","quantity_base":"0.001","limit_price_quote":"65000"}
    assert client.post(path, json=buy).status_code == 401
    assert client.post(path, json=buy, headers={**headers,"Origin":"https://outside.invalid"}).status_code == 403
    assert client.post(path, json=buy, headers=headers).status_code == 409
    connect(client, headers)
    assert client.post(path, json={**buy,"request_id":str(uuid4())},headers=headers).status_code == 422
    trader.after_test = lambda: pytest.fail("La revisión local llamó al endpoint de prueba")
    movements = client.get(base+"/events",headers=headers).json()
    accepted = client.post(path,json=buy,headers=headers)
    assert accepted.is_success and accepted.headers["cache-control"] == "no-store", accepted.text
    result = accepted.json()
    assert result["compatible"] and result["reason"] == "LOCAL_PREFLIGHT_ONLY"
    assert Decimal(result["notional_quote"]) == Decimal("65")
    assert Decimal(result["estimated_max_fee_quote"]) == Decimal("0.0325")
    for terms, reason in (
        ({**buy,"quantity_base":"0.000001"}, "MIN_NOTIONAL"),
        ({**buy,"quantity_base":"0.003"}, "ORDER_LIMIT"),
        ({**buy,"side":"SELL"}, "INSUFFICIENT_BASE"),
    ):
        rejected = client.post(path,json=terms,headers=headers)
        assert rejected.is_success, rejected.text
        assert not rejected.json()["compatible"] and rejected.json()["reason"] == reason
    assert client.post(path,json={**buy,"quantity_base":"NaN"},headers=headers).status_code == 409
    assert client.post(path.replace(policy.policy_id,draft.policy_id),json=buy,headers=headers).status_code == 409
    assert client.get(base+"/events",headers=headers).json() == movements and not trader.posts
    vault.unlock(fixtures.PASSWORD)
    with vault.session() as db:
        assert db.query(RealLiveSpotIntent).count() == db.query(RealMexcOrderTestAttempt).count() == 0
    vault.lock()
    reserve(vault,policy,venue,clock,submitted=True)
    pending = client.post(path,json=buy,headers=headers)
    assert pending.status_code == 409 and "pendiente" in pending.json()["detail"]


def test_manual_order_review_builds_exact_limit_from_quote_budget(tmp_path, monkeypatch):
    from app.portfolio.models import RealLiveSpotIntent, RealMexcOrderTestAttempt

    client, headers, vault, policy, _, account, _, trader, _ = setup_api(tmp_path, monkeypatch)
    connect(client, headers)
    path = f"/real/accounts/{account}/mexc-trading-sessions/{policy.policy_id}/orders/review"
    body = {"side": "BUY", "budget_quote": "65.09"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={**headers, "Origin": "https://outside.invalid"}).status_code == 403
    assert client.post(path, json={**body,"quantity_base":"0.001"}, headers=headers).status_code == 422
    trader.after_test = lambda: pytest.fail("Calcular una orden no usa el endpoint de prueba")
    response = client.post(path, json=body, headers=headers)
    assert response.is_success and response.headers["cache-control"] == "no-store", response.text
    result = response.json()
    assert result["compatible"] and result["quantity_base"] == "0.001"
    assert result["limit_price_quote"] == "65001" and result["reference_side"] == "ASK"
    assert Decimal(result["estimated_total_quote"]) == Decimal("65.0335005")
    assert Decimal(result["budget_quote"]) == Decimal("65.09")
    assert Decimal(result["available_quote"]) == Decimal("700")
    sale = client.post(path, json={**body,"side":"SELL"}, headers=headers).json()
    assert not sale["compatible"] and sale["reason"] == "INSUFFICIENT_BASE"
    assert sale["limit_price_quote"] == "65000" and sale["reference_side"] == "BID"
    assert Decimal(sale["estimated_total_quote"]) < Decimal(sale["notional_quote"])
    assert client.post(path, json={**body,"budget_quote":"NaN"}, headers=headers).status_code == 409
    assert client.post(path, json={**body,"budget_quote":"0.01"}, headers=headers).status_code == 409
    small = client.post(path, json={**body,"budget_quote":"0.10"}, headers=headers).json()
    assert not small["compatible"] and small["reason"] == "MIN_NOTIONAL"
    assert not trader.posts
    vault.unlock(fixtures.PASSWORD)
    with vault.session() as db:
        assert db.query(RealLiveSpotIntent).count() == db.query(RealMexcOrderTestAttempt).count() == 0
    vault.lock()


@pytest.mark.parametrize("free_base,quantity,outside,next_reason", [
    ("0.000300000000000001", "0.000299", "0.000001000000000001", "INSUFFICIENT_BASE"),
    ("0.005", "0.002307", "0.002693", "ORDER_LIMIT"),
    ("0.000003", "0.000002", "0.000001", "MIN_NOTIONAL"),
])
def test_maximum_sale_reviews_exact_balance_and_caps_without_submitting(
        tmp_path, monkeypatch, free_base, quantity, outside, next_reason):
    from app.portfolio.models import RealLiveSpotIntent, RealMexcOrderTestAttempt

    movement = {"kind": "ADJUSTMENT_IN", "asset": "BTC", "quote_currency": "USDT",
                "quantity": Decimal(free_base), "unit_price": Decimal("65000"), "note": "Inventario de ensayo"}
    client, headers, vault, policy, _, account, venue, trader, _ = setup_api(
        tmp_path, monkeypatch, base=free_base, book_movements=(movement,))
    path = f"/real/accounts/{account}/mexc-trading-sessions/{policy.policy_id}/orders/review"
    body = {"side": "SELL", "size_mode": "MAX_AVAILABLE"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={**headers, "Origin":"https://outside.invalid"}).status_code == 403
    connect(client, headers)
    assert client.post(path, json={**body,"side":"BUY"}, headers=headers).status_code == 422
    assert client.post(path, json={**body,"budget_quote":"100"}, headers=headers).status_code == 422
    assert client.post(path.removesuffix("/review"), json=body, headers=headers).status_code == 422
    trader.after_test = lambda: pytest.fail("La revisión de venta llamó al endpoint de prueba")
    accepted = client.post(path, json=body, headers=headers)
    assert accepted.is_success and accepted.headers["cache-control"] == "no-store", accepted.text
    result = accepted.json()
    assert result["size_mode"] == "MAX_AVAILABLE" and result["reference_side"] == "BID"
    assert result["compatible"] == (next_reason != "MIN_NOTIONAL")
    assert result["quantity_base"] == quantity and result["limit_price_quote"] == "65000"
    assert result["available_base"] == free_base and result["base_outside_order"] == outside
    assert Decimal(result["estimated_max_fee_base"]) == Decimal(quantity) * Decimal("0.0005")
    assert Decimal(result["estimated_total_quote"]) == Decimal(quantity) * Decimal("65000") * Decimal("0.9995")
    next_lot = {"side": "SELL", "quantity_base": str(Decimal(quantity) + Decimal("0.000001")),
                "limit_price_quote": result["limit_price_quote"]}
    denied = client.post(path, json=next_lot, headers=headers)
    assert denied.is_success and not denied.json()["compatible"] and denied.json()["reason"] == next_reason
    assert not trader.posts
    vault.unlock(fixtures.PASSWORD)
    with vault.session() as db:
        assert db.query(RealLiveSpotIntent).count() == db.query(RealMexcOrderTestAttempt).count() == 0
    vault.lock()
    venue.base = "0"
    assert client.post(path, json=body, headers=headers).status_code == 409


def test_maximum_sale_combines_lot_precision_exchange_and_legacy_risk(tmp_path):
    from app.live_execution.mexc_execution import _maximum_sell_terms

    vault, policy, _, venue, clock = fixtures.setup(tmp_path)
    inputs = fixtures.inputs(vault, policy, venue, clock)
    now = clock()
    state = replace(inputs.state, free_base=Decimal("0.001"), pending_base=Decimal("0.0001"),
                    average_cost_quote_per_base=Decimal("100000"))
    rules = replace(inputs.rules, base_step=Decimal("0.0000015"), max_quote=Decimal("20"))
    modified = replace(inputs, state=state, rules=rules)
    assert _maximum_sell_terms(policy, modified, now) == (Decimal("0.000306"), Decimal("65000"))
    legacy = replace(policy, allow_risk_reducing_exits=False, max_daily_turnover_quote=Decimal("13"))
    assert _maximum_sell_terms(legacy, modified, now) == (Decimal("0.000198"), Decimal("65000"))
    legacy = replace(legacy, max_daily_loss_quote=Decimal("3.505"))
    assert _maximum_sell_terms(legacy, modified, now) == (Decimal("0.000099"), Decimal("65000"))
    # The next exact lot loses 3.5751, while 0.000099 loses 3.46995.
    free = replace(modified, state=replace(state, free_base=Decimal("0.000109"),
                                          pending_base=Decimal("0.00001")),
                   rules=replace(rules, fee_rate_upper=Decimal("0")))
    assert _maximum_sell_terms(policy, free, now) == (Decimal("0.000099"), Decimal("65000"))
    vault.lock()


def test_workflow_is_passive_local_and_scoped_to_the_current_view_key(tmp_path, monkeypatch):
    client, headers, _, _, _, account, venue, trader, _ = setup_api(tmp_path, monkeypatch)
    other = client.post("/real/accounts", headers=headers,
                        json={"name": "Otro libro", "base_currency": "USDT"}).json()["id"]
    endpoint = f"/real/accounts/{account}/mexc-workflow"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={**headers, "Origin": "https://outside.invalid"}).status_code == 403
    assert client.get(endpoint, headers=headers).json() == {"view": "DISCONNECTED"}
    view = "/real/exchanges/mexc/connect"
    assert client.post(view, headers=headers, json={"api_key": "another-key", "secret_key": "fixture-secret"}).status_code == 200
    assert client.get(endpoint, headers=headers).json() == {"view": "DIFFERENT_CONNECTION"}
    other_endpoint = f"/real/accounts/{other}/mexc-workflow"
    assert client.get(other_endpoint, headers=headers).json() == {"view": "UNLINKED", "book_empty": True}
    connect(client, headers)

    def unexpected(*args, **kwargs):
        pytest.fail("The local workflow contacted MEXC or extended the session")

    with monkeypatch.context() as local:
        for method in ("balances", "account_uid", "open_orders", "open_orders_unfiltered"):
            local.setattr(venue, method, unexpected)
        local.setattr(RealAccess, "_touch", unexpected)
        response = client.get(endpoint, headers=headers)
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"view": "LINKED", "book_empty": False, "trade_verified": True, "stopped": False,
                                   "pending_orders": False, "withdrawal_pending": False, "bot_active": False}
        assert client.get(other_endpoint, headers=headers).json() == {"view": "OTHER_ACCOUNT"}
        assert client.get(f"/real/accounts/{uuid4()}/mexc-workflow", headers=headers).status_code == 404
        assert trader.posts == []
    stopped = client.post(f"/real/accounts/{account}/mexc-stop", headers=headers, json={"confirmation": "DETENER"})
    assert stopped.status_code == 200
    assert client.get(endpoint, headers=headers).json()["stopped"] is True
    assert client.post("/real/exchanges/mexc/trade/disconnect", headers=headers).status_code == 200
    assert client.get(endpoint, headers=headers).json()["trade_verified"] is False
    client.post("/real/vault/lock", headers=headers)
    assert client.get(endpoint, headers=headers).status_code == 401


def test_release_api_needs_exact_account_and_confirmation_but_not_view_or_trade(tmp_path, monkeypatch):
    client, headers, vault, policy, draft, account, venue, trader, clock = setup_api(tmp_path, monkeypatch)
    intent = reserve(vault, policy, venue, clock)
    url = f"/real/execution/mexc/intents/{intent.intent_id}/release-unsent"
    body = {"account_id":account, "confirmation":"LIBERAR_RESERVA_NO_ENVIADA"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, headers={**headers,"Origin":"https://outside.invalid"}, json=body).status_code == 403
    assert client.post(url, headers=headers, json={**body,"confirmation":""}).status_code == 422
    assert client.post(url, headers=headers, json={**body,"account_id":str(uuid4())}).status_code == 409
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 200 and response.json()["status"] == "RELEASED_UNSENT", response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.post(url, headers=headers, json=body).json()["status"] == "ALREADY_RELEASED"
    assert trader.posts == []
    client.post("/real/vault/lock", headers=headers)


@pytest.mark.parametrize("mode", ["open", "partial", "lost", "already_final"])
def test_selected_mexc_order_cancel_and_reconcile_without_resending(tmp_path, monkeypatch, mode):
    client, headers, _, policy, _, account, venue, trader, _ = setup_api(
        tmp_path, monkeypatch, trader_factory=CancelableExchangeOrders)
    connect(client, headers)
    trader.open_next = "partial" if mode == "partial" else "open"
    trader.lose_cancel_reply = mode == "lost"
    identifier = str(uuid4())
    account_path = f"/real/accounts/{account}"
    execution_path = f"/real/execution/mexc/intents/{identifier}"
    submitted = client.post(f"{account_path}/mexc-trading-sessions/{policy.policy_id}/orders", headers=headers, json={
        "request_id": identifier, "side":"BUY", "quantity_base":"0.001",
        "limit_price_quote":"65000", "confirmation":"ENVIAR_ORDEN_REAL",
    })
    assert submitted.status_code == 200 and submitted.json()["synchronization"]["stage"] == "ORDER_OPEN", submitted.text
    body = {"account_id":account,"confirmation":"CANCELAR_ORDEN_MEXC"}
    path = execution_path + "/cancel"
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers={**headers,"Origin":"https://outside.invalid"}, json=body).status_code == 403
    assert client.post(path, headers=headers, json={"account_id":account}).status_code == 422
    assert client.post(path, headers=headers, json=body).status_code == 409
    stopped = client.post(account_path+"/mexc-stop", headers=headers, json={"confirmation":"DETENER"})
    assert stopped.status_code == 200 and stopped.json()["pending_orders"] == 1
    assert client.post(path, headers=headers, json={**body,"account_id":str(uuid4())}).status_code == 409
    assert trader.cancellations == []
    if mode == "already_final":
        trader.finish(identifier)

    cancelled = client.post(path, headers=headers, json=body)
    assert cancelled.status_code == 200 and cancelled.headers["cache-control"] == "no-store", cancelled.text
    result = cancelled.json()
    if mode == "lost":
        assert result["request_status"] == "OUTCOME_UNKNOWN" and result["synchronization"] is None
        still_pending = client.get(account_path+"/mexc-execution-status", headers=headers).json()
        assert still_pending["pending_orders"] == 1
        assert client.post(path, headers=headers, json=body).json() == result
        assert trader.cancellations == [identifier]
        trader.hide_order = False
        # Read-only reconciliation still works when the trade key is gone.
        assert client.post("/real/exchanges/mexc/trade/disconnect", headers=headers).status_code == 200
        synchronized = client.post(execution_path+"/synchronize", headers=headers, json={"account_id":account})
        assert synchronized.status_code == 200, synchronized.text
        observed = synchronized.json()
    else:
        assert result["request_status"] == ("NOT_NEEDED" if mode == "already_final" else "ACKNOWLEDGED")
        observed = result["synchronization"]
        replay = client.post(path, headers=headers, json=body)
        assert replay.status_code == 200 and replay.json()["synchronization"]["stage"] == "ALREADY_COMPLETED"
    assert observed["stage"] == "COMPLETED" and observed["reservation_closed"]
    assert observed["external_status"] == ("PARTIALLY_CANCELED" if mode == "partial" else "CANCELED")
    assert observed["booked"] is (mode == "partial")
    assert trader.posts == [identifier]
    assert trader.cancellations == ([] if mode == "already_final" else [identifier])
    assert Decimal(venue.locked) == 0
    book = client.get(account_path+"/snapshot", headers=headers).json()
    assert Decimal(book["cash"]["USDT"]) == (Decimal("693.48") if mode == "partial" else Decimal("700"))
    events = client.get(account_path+"/events", headers=headers).json()
    buys = [item for item in events if item["kind"] == "BUY"]
    assert len(buys) == (1 if mode == "partial" else 0)
    assert client.get(account_path+"/mexc-execution-status", headers=headers).json()["pending_orders"] == 0
    assert client.get(account_path+"/mexc-pending-orders", headers=headers).json()["orders"] == []
    assert "spot_order_action" not in client.get(account_path+"/mexc-workflow", headers=headers).json()
    assert client.post("/real/vault/lock", headers=headers).status_code == 200



def test_renewed_view_recovers_only_the_original_order_without_rebinding_or_resending(tmp_path, monkeypatch):
    from app.portfolio.models import RealLiveSpotIntent, RealMexcAccountLink, RealMexcUidLink

    client, headers, vault, policy, _, account, venue, trader, _ = setup_api(
        tmp_path, monkeypatch, trader_factory=CancelableExchangeOrders)
    connect(client, headers)
    trader.open_next = "partial"
    identifier = str(uuid4())
    account_path = f"/real/accounts/{account}"
    path = f"/real/execution/mexc/intents/{identifier}"
    order = {"request_id":identifier, "side":"BUY", "quantity_base":"0.001",
             "limit_price_quote":"65000", "confirmation":"ENVIAR_ORDEN_REAL"}
    sent = client.post(f"{account_path}/mexc-trading-sessions/{policy.policy_id}/orders",headers=headers,json=order)
    assert sent.status_code == 200 and sent.json()["synchronization"]["stage"] == "ORDER_OPEN",sent.text
    recovery = path+"/synchronize-renewed-view"
    body = {"account_id":account,"confirmation":"RECUPERAR_CON_VIEW_RENOVADA"}
    assert client.post(recovery,json=body).status_code == 401
    assert client.post(recovery,headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
    assert client.post(recovery,headers=headers,json={"account_id":account}).status_code == 422
    assert client.post(recovery,headers=headers,json={**body,"api_key":"never-in-this-request"}).status_code == 422

    # The original key continues to use the existing synchronization path.
    assert client.post(recovery,headers=headers,json=body).status_code == 409
    assert client.post("/real/exchanges/mexc/disconnect",headers=headers).status_code == 200
    assert client.post(recovery,headers=headers,json=body).status_code == 409

    def select_view(key):
        response = client.post("/real/exchanges/mexc/connect",headers=headers,
                               json={"api_key":key,"secret_key":"fixture-secret"})
        assert response.status_code == 200,response.text

    def reject_without_order_reads():
        with monkeypatch.context() as local:
            def unexpected(*args, **kwargs):
                pytest.fail("Order/fill read before proving the replacement UID")
            local.setattr(venue,"order_by_client_id",unexpected)
            local.setattr(venue,"trades_for_order",unexpected)
            rejected = client.post(recovery,headers=headers,json=body)
            assert rejected.status_code == 409 and "inequívocamente" in rejected.json()["detail"],rejected.text

    select_view("fixture-wrong-view")
    reject_without_order_reads()
    select_view("fixture-renewed-key")
    # Old or contradictory identity records must never be used as a new alias.
    vault.unlock(fixtures.PASSWORD)
    duplicate_ref = "f"*64
    renewed_ref = hmac.new(b"fixture-secret",b"fixture-renewed-key",hashlib.sha256).hexdigest()
    with vault.session() as db:
        original_uid_link = db.get(RealMexcUidLink,policy.account_ref)
        db.add(RealMexcUidLink(account_ref=duplicate_ref,uid_digest=original_uid_link.uid_digest,
                              observed_at=original_uid_link.observed_at))
        db.commit()
    reject_without_order_reads()
    with vault.session() as db:
        db.delete(db.get(RealMexcUidLink,duplicate_ref))
        db.add(RealMexcAccountLink(account_ref=renewed_ref,account_id=account,
                                  created_at=db.get(RealMexcAccountLink,policy.account_ref).created_at))
        db.commit()
    reject_without_order_reads()
    with vault.session() as db:
        db.delete(db.get(RealMexcAccountLink,renewed_ref))
        db.commit()
    vault.lock()
    assert client.post(path+"/synchronize",headers=headers,json={"account_id":account}).status_code == 409
    assert client.post(recovery,headers=headers,json={**body,"account_id":str(uuid4())}).status_code == 409
    opened = client.post(recovery,headers=headers,json=body)
    assert opened.status_code == 200 and opened.json()["stage"] == "ORDER_OPEN",opened.text
    assert not opened.json()["orders_enabled"] and not opened.json()["booked"]
    assert client.get(account_path+"/mexc-workflow",headers=headers).json()["view"] == "DIFFERENT_CONNECTION"
    assert client.get(account_path+"/mexc-pending-orders",headers=headers).json()["trade_ready"] is False
    assert client.post(f"{account_path}/mexc-trading-sessions/{policy.policy_id}/orders",
                       headers=headers,json={**order,"request_id":str(uuid4())}).status_code == 409

    trader.finish(identifier)
    # The credential must still have the same UID after reading the order and fills.
    original_uid, original_order_read = venue.uid, venue.order_by_client_id
    def switch_uid(symbol, client_id):
        observed = original_order_read(symbol,client_id)
        venue.uid = "987654321"
        return observed
    with monkeypatch.context() as local:
        local.setattr(venue,"order_by_client_id",switch_uid)
        assert client.post(recovery,headers=headers,json=body).status_code == 409
    venue.uid = original_uid
    assert not any(event["kind"] == "BUY" for event in client.get(account_path+"/events",headers=headers).json())
    assert client.get(account_path+"/mexc-pending-orders",headers=headers).json()["orders"][0]["intent_id"] == identifier

    settled = client.post(recovery,headers=headers,json=body)
    assert settled.status_code == 200 and settled.headers["Cache-Control"] == "no-store",settled.text
    assert settled.json()["stage"] == "COMPLETED" and settled.json()["external_status"] == "PARTIALLY_CANCELED"
    assert settled.json()["booked"] and settled.json()["reservation_closed"] and not settled.json()["orders_enabled"]
    assert "fixture-" not in settled.text and original_uid not in settled.text
    assert client.post(recovery,headers=headers,json=body).json()["stage"] == "ALREADY_COMPLETED"
    buys = [event for event in client.get(account_path+"/events",headers=headers).json() if event["kind"]=="BUY"]
    assert len(buys) == 1 and Decimal(buys[0]["quantity"]) == Decimal("0.0001")
    assert client.get(account_path+"/mexc-pending-orders",headers=headers).json()["orders"] == []
    assert trader.posts == [identifier] and trader.cancellations == []
    assert client.post("/real/vault/lock",headers=headers).status_code == 200
    vault.unlock(fixtures.PASSWORD)
    with vault.session() as db:
        assert db.get(RealLiveSpotIntent,identifier).account_ref == policy.account_ref
        assert db.query(RealMexcAccountLink).count() == db.query(RealMexcUidLink).count() == 1
        assert db.get(RealMexcAccountLink,policy.account_ref).account_id == account
    vault.lock()


@pytest.mark.parametrize("submitted", [False, True], ids=["unsent", "uncertain"])
def test_pending_order_guidance_is_local_account_scoped_and_survives_reopening(tmp_path, monkeypatch, submitted):
    client, headers, vault, policy, _, account, venue, trader, clock = setup_api(tmp_path, monkeypatch)
    intent = reserve(vault, policy, venue, clock, submitted=submitted)
    other = client.post("/real/accounts", headers=headers,
                        json={"name":"Otra cuenta", "base_currency":"USDT"}).json()["id"]
    base = f"/real/accounts/{account}"
    path = base + "/mexc-pending-orders"
    workflow = base + "/mexc-workflow"
    expected = "SYNCHRONIZE" if submitted else "REVIEW_RESERVATION"

    def review(view, view_ready, trade_ready):
        with monkeypatch.context() as local:
            def unexpected(*args, **kwargs):
                pytest.fail("A passive recovery read must not contact MEXC or extend the session")
            for method in ("balances", "account_uid", "open_orders", "open_orders_unfiltered"):
                local.setattr(venue, method, unexpected)
            local.setattr(RealAccess, "_touch", unexpected)
            state = client.get(workflow, headers=headers)
            assert state.json()["view"] == view and state.json()["spot_order_action"] == expected
            reply = client.get(path, headers=headers)
            assert reply.status_code == 200 and reply.headers["Cache-Control"] == "no-store",reply.text
            data = reply.json()
            assert data["view_ready"] is view_ready and data["trade_ready"] is trade_ready
            assert len(data["orders"]) == 1
            entry = data["orders"][0]
            assert entry["account_id"] == account and entry["intent_id"] == intent.intent_id
            assert entry["stage"] == ("ORDER_UNVERIFIED" if submitted else "RESERVED_NOT_SUBMITTED")
            assert intent.account_ref not in reply.text
            assert client.get(f"/real/accounts/{other}/mexc-pending-orders", headers=headers).json() == {
                "view_ready":False, "trade_ready":False, "orders":[]}
            assert "spot_order_action" not in client.get(f"/real/accounts/{other}/mexc-workflow", headers=headers).json()

    assert client.get(path).status_code == 401
    assert client.get(path, headers={**headers,"Origin":"https://outside.invalid"}).status_code == 403
    assert client.get(f"/real/accounts/{uuid4()}/mexc-pending-orders", headers=headers).status_code == 404
    review("DISCONNECTED", False, False)
    assert client.post("/real/exchanges/mexc/connect", headers=headers,
                       json={"api_key":"other-key","secret_key":"fixture-secret"}).status_code == 200
    review("DIFFERENT_CONNECTION", False, False)
    connect(client, headers)
    review("LINKED", True, True)
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    assert client.get(path, headers=headers).status_code == 401
    headers = {"X-Orbis-Vault-Token":client.post("/real/vault/unlock",
        json={"passphrase":fixtures.PASSWORD}).json()["token"]}
    review("DISCONNECTED", False, False)
    if not submitted:
        released = client.post(f"/real/execution/mexc/intents/{intent.intent_id}/release-unsent", headers=headers,
                               json={"account_id":account,"confirmation":"LIBERAR_RESERVA_NO_ENVIADA"})
        assert released.status_code == 200 and released.json()["status"] == "RELEASED_UNSENT",released.text
        assert client.get(path, headers=headers).json()["orders"] == []
        assert client.get(workflow, headers=headers).json() == {"view":"DISCONNECTED"}
    assert not trader.posts


def test_renew_view_stops_and_reopens_original_book_with_fresh_trade_and_review(tmp_path, monkeypatch):
    from app.portfolio.models import RealLiveSpotIntent, RealMexcAccountLink, RealMexcUidLink, RealMexcViewBinding
    client, headers, vault, policy, draft, account, venue, trader, _ = setup_api(
        tmp_path, monkeypatch, trader_factory=CancelableExchangeOrders)
    connect(client, headers)
    base = f"/real/accounts/{account}"
    link = base+"/mexc-link"
    renew = link+"/renew-view"
    confirm = {"confirmation":"RENOVAR_VIEW_Y_DETENER"}
    other = client.post("/real/accounts",headers=headers,json={"name":"Otra cuenta","base_currency":"USDT"}).json()["id"]
    sessions = base+"/mexc-trading-sessions"
    order = {"request_id":str(uuid4()),"side":"BUY","quantity_base":"0.001","limit_price_quote":"65000",
             "confirmation":"ENVIAR_ORDEN_REAL"}
    old_anchor = new_anchor(client, headers, account)
    trader.open_next = "open"
    old_order = client.post(f"{sessions}/{policy.policy_id}/orders",headers=headers,json=order)
    assert old_order.status_code == 200 and old_order.json()["synchronization"]["stage"] == "ORDER_OPEN",old_order.text
    assert client.post(renew,json=confirm).status_code == 401
    assert client.post(renew,headers={**headers,"Origin":"https://outside.invalid"},json=confirm).status_code == 403
    assert client.post(renew,headers=headers,json={**confirm,"unexpected":"private"}).status_code == 422
    assert client.post(renew,headers=headers,json=confirm).status_code == 409

    def select_view(key):
        connected = client.post("/real/exchanges/mexc/connect",headers=headers,
                                json={"api_key":key,"secret_key":"fixture-secret"})
        assert connected.is_success,connected.text

    select_view("fixture-wrong-view")
    assert not client.get(link,headers=headers).json().get("can_renew_view")
    assert client.post(renew,headers=headers,json=confirm).status_code == 409
    assert not client.get(base+"/mexc-execution-status",headers=headers).json()["stopped"]
    select_view("fixture-renewed-key")
    assert client.get(link,headers=headers).json()["can_renew_view"]
    assert not client.get(f"/real/accounts/{other}/mexc-link",headers=headers).json().get("can_renew_view")
    assert client.post(f"/real/accounts/{other}/mexc-link/renew-view",headers=headers,json=confirm).status_code == 409
    trade_connect = "/real/exchanges/mexc/trade/connect"
    trade = {"api_key":"fixture-trade","secret_key":"fixture-secret"}
    assert client.post(trade_connect,headers=headers,json=trade).json()["verified"]
    renewed = client.post(renew,headers=headers,json=confirm)
    assert renewed.status_code == 200 and renewed.headers["Cache-Control"] == "no-store",renewed.text
    assert renewed.json() == {"renewed":True,"new_binding":True,"stopped":True}
    assert client.post(renew,headers=headers,json=confirm).json() == {"renewed":True,"new_binding":False,"stopped":True}
    assert client.get(link,headers=headers).json()["session_renewed"]
    assert client.get(base+"/mexc-workflow",headers=headers).json()["view_renewed"]
    assert not client.get("/real/exchanges/mexc/trade/status",headers=headers).json()["verified"]
    assert not any(item["execution_enabled"] for item in client.get(sessions,headers=headers).json())
    assert len(trader.posts)==1 and not trader.cancellations

    trader.finish(order["request_id"])
    synchronized = client.post(f"/real/execution/mexc/intents/{order['request_id']}/synchronize",
                               headers=headers,json={"account_id":account})
    assert synchronized.is_success and synchronized.json()["reservation_closed"],synchronized.text
    assert client.post(trade_connect,headers=headers,json=trade).json()["verified"]
    reopen = base+f"/mexc-policy-drafts/{draft.policy_id}/resume"
    approval = {"request_id":str(uuid4()),"anchor_id":old_anchor,"confirmation":"REABRIR_OPERATIVA_REAL"}
    assert client.post(reopen,headers=headers,json=approval).status_code == 409
    approval["anchor_id"] = new_anchor(client, headers, account)
    reopened = client.post(reopen,headers=headers,json=approval)
    assert reopened.is_success and reopened.json()["execution_enabled"],reopened.text
    assert client.post(renew,headers=headers,json=confirm).json() == {"renewed":True,"new_binding":False,"stopped":False}
    order["request_id"] = str(uuid4())
    assert client.post(f"{sessions}/{policy.policy_id}/orders",headers=headers,json=order).status_code == 409
    active = reopened.json()["id"]
    original_uid = venue.uid
    venue.uid = "987654321"
    assert client.post(f"{sessions}/{active}/orders",headers=headers,json=order).status_code == 409
    assert len(trader.posts)==1
    venue.uid = original_uid
    posted = client.post(f"{sessions}/{active}/orders",headers=headers,json=order)
    assert posted.is_success and posted.json()["journal_status"] == "RESERVATION_CLOSED",posted.text
    assert len(trader.posts)==2 and not trader.cancellations
    assert client.post("/real/vault/lock",headers=headers).is_success
    vault.unlock(fixtures.PASSWORD)
    with vault.session() as db:
        assert db.query(RealMexcAccountLink).count() == db.query(RealMexcUidLink).count() == 1
        assert db.get(RealLiveSpotIntent,order["request_id"]).account_ref == policy.account_ref
        binding = db.get(RealMexcViewBinding,policy.account_ref)
        assert binding.credential_ref == hmac.new(b"fixture-secret",b"fixture-renewed-key",hashlib.sha256).hexdigest()
    backup_dir = vault.path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = vault.backup(backup_dir / "renewed-view-backup.db")
    assert vault.verify_managed_backup(backup)["verified"]
    vault.lock()
    token = client.post("/real/vault/unlock",json={"passphrase":fixtures.PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    assert client.get(base+"/mexc-workflow",headers=headers).json()["view"] == "DISCONNECTED"
    connect_url = "/real/exchanges/mexc/connect"
    old = client.post(connect_url,headers=headers,json={"api_key":"fixture-key","secret_key":"fixture-secret"})
    assert old.status_code == 409 and "reemplazada" in old.json()["detail"]
    venue.uid = "987654321"
    with monkeypatch.context() as local:
        def reject_balance_read():
            pytest.fail("Se consultaron saldos antes de verificar el UID")

        local.setattr(venue, "balances", reject_balance_read)
        wrong_uid = client.post(connect_url,headers=headers,
                                json={"api_key":"fixture-renewed-key","secret_key":"fixture-secret"})
    assert wrong_uid.status_code == 409 and original_uid not in wrong_uid.text
    assert client.get(base+"/mexc-workflow",headers=headers).json()["view"] == "DISCONNECTED"
    venue.uid = original_uid
    select_view("fixture-renewed-key")
    linked = client.get(base+"/mexc-workflow",headers=headers).json()
    assert linked["view"] == "LINKED" and linked["view_renewed"] and not linked["trade_verified"]
    assert client.post(renew,headers=headers,json=confirm).json()["new_binding"] is False
    select_view("fixture-renewed-key-2")
    assert client.get(link,headers=headers).json()["can_renew_view"]
    assert client.post(renew,headers=headers,json=confirm).json()["new_binding"]
    select_view("fixture-renewed-key")
    assert client.get(base+"/mexc-workflow",headers=headers).json()["view"] == "DIFFERENT_CONNECTION"
    select_view("fixture-renewed-key-2")
    current = client.get(base+"/mexc-workflow",headers=headers).json()
    assert current["view"] == "LINKED" and current["stopped"] and not current["trade_verified"]
    assert client.post("/real/vault/lock", headers=headers).is_success
    reopened = FastAPI()
    reopened.include_router(create_real_router(
        vault.path, mexc_factory=lambda key, secret: controlled_view(venue, key, secret),
    ))
    with TestClient(reopened) as fresh_client:
        unlocked = fresh_client.post("/real/vault/unlock", json={"passphrase": fixtures.PASSWORD})
        fresh_headers = {"X-Orbis-Vault-Token": unlocked.json()["token"]}
        connected = fresh_client.post(connect_url, headers=fresh_headers,
                                      json={"api_key": "fixture-renewed-key-2", "secret_key": "fixture-secret"})
        assert connected.is_success, connected.text
        assert fresh_client.get(base+"/mexc-workflow", headers=fresh_headers).json() == current
        assert fresh_client.post("/real/vault/lock", headers=fresh_headers).is_success
    assert len(trader.posts)==2



def test_schema_80_migration_preserves_mexc_book_and_original_view(tmp_path, monkeypatch):
    from app.portfolio.models import RealMexcViewBinding
    from app.portfolio.vault import SCHEMA_VERSION

    client,headers,vault,policy,_,account,_,_,_ = setup_api(tmp_path,monkeypatch)
    base = f"/real/accounts/{account}"
    before = client.get(base+"/events",headers=headers).json()
    assert before and client.post("/real/vault/lock",headers=headers).is_success
    vault.unlock(fixtures.PASSWORD)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_mexc_view_bindings")
        db.connection().exec_driver_sql("PRAGMA user_version = 80")
        db.commit()
    vault.lock()
    vault.unlock(fixtures.PASSWORD)
    backup = vault.last_migration_backup
    assert backup and backup.is_file() and vault.verify_managed_backup(backup)["verified"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.query(RealMexcViewBinding).count()==0
    vault.lock()
    token = client.post("/real/vault/unlock",json={"passphrase":fixtures.PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    connect(client,headers)
    assert client.get(base+"/mexc-workflow",headers=headers).json()["view"] == "LINKED"
    assert client.get(base+"/events",headers=headers).json() == before
    assert any(item["id"] == policy.policy_id for item in client.get(base+"/mexc-trading-sessions",headers=headers).json())


def test_stop_and_resume_api_make_new_policy_and_leave_previous_sessions_disabled(tmp_path, monkeypatch):
    client, headers, vault, policy, draft, account, venue, trader, clock = setup_api(tmp_path, monkeypatch)
    reserve(vault, policy, venue, clock)
    connect(client, headers)
    stop_url = f"/real/accounts/{account}/mexc-stop"
    assert client.post(stop_url, json={"confirmation":"DETENER"}).status_code == 401
    stopped = client.post(stop_url, headers=headers, json={"confirmation":"DETENER"})
    assert stopped.status_code == 200 and stopped.json()["stopped"], stopped.text
    assert stopped.json()["released_unsent"] == 1 and stopped.json()["can_review_resume"]
    sessions_url = f"/real/accounts/{account}/mexc-trading-sessions"
    assert all(not item["execution_enabled"] for item in client.get(sessions_url, headers=headers).json())
    anchor = new_anchor(client, headers, account)
    resume_url = f"/real/accounts/{account}/mexc-policy-drafts/{draft.policy_id}/resume"
    body = {"request_id":str(uuid4()), "anchor_id":anchor, "confirmation":"REABRIR_OPERATIVA_REAL"}
    assert client.post(resume_url, headers=headers, json={**body,"confirmation":""}).status_code == 422
    assert client.post(resume_url, headers={**headers,"Origin":"https://outside.invalid"}, json=body).status_code == 403
    reopened = client.post(resume_url, headers=headers, json=body)
    assert reopened.status_code == 200 and reopened.json()["execution_enabled"], reopened.text
    assert client.post(resume_url, headers=headers, json=body).json() == reopened.json()
    assert trader.posts == []
    listed = client.get(sessions_url, headers=headers).json()
    assert next(item for item in listed if item["id"] == policy.policy_id)["status"] == "REACTIVATION_REQUIRED"
    order = {"request_id":str(uuid4()), "side":"BUY", "quantity_base":"0.001",
             "limit_price_quote":"65000", "confirmation":"ENVIAR_ORDEN_REAL"}
    assert client.post(sessions_url + f"/{policy.policy_id}/orders", headers=headers, json=order).status_code == 409
    result = client.post(sessions_url + f"/{reopened.json()['id']}/orders", headers=headers, json=order)
    assert result.status_code == 200 and result.json()["journal_status"] == "RESERVATION_CLOSED", result.text
    assert len(trader.posts) == 1
    client.post("/real/vault/lock", headers=headers)


def test_uncertain_order_cannot_be_released_or_hidden_by_reopening(tmp_path, monkeypatch):
    client, headers, vault, policy, draft, account, venue, trader, clock = setup_api(tmp_path, monkeypatch)
    intent = reserve(vault, policy, venue, clock, submitted=True)
    connect(client, headers)
    client.post(f"/real/accounts/{account}/mexc-stop", headers=headers, json={"confirmation":"DETENER"})
    status = client.get(f"/real/accounts/{account}/mexc-execution-status", headers=headers).json()
    assert status["stopped"] and status["pending_orders"] == 1 and not status["can_review_resume"]
    released = client.post(f"/real/execution/mexc/intents/{intent.intent_id}/release-unsent", headers=headers,
                           json={"account_id":account, "confirmation":"LIBERAR_RESERVA_NO_ENVIADA"})
    assert released.status_code == 409
    anchor = new_anchor(client, headers, account)
    result = client.post(f"/real/accounts/{account}/mexc-policy-drafts/{draft.policy_id}/resume", headers=headers,
                         json={"request_id":str(uuid4()), "anchor_id":anchor, "confirmation":"REABRIR_OPERATIVA_REAL"})
    assert result.status_code == 409 and trader.posts == []
    assert client.get(f"/real/accounts/{account}/mexc-execution-status", headers=headers).json()["stopped"]
    client.post("/real/vault/lock", headers=headers)
