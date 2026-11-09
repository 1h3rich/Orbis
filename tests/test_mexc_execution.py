"""Apertura → orden única → fill → libro → venta, con exchange controlado."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from app.live_execution.journal import recover_spot_intent, register_spot_policy, stop_spot_account
from app.live_execution.mexc_execution import (
    MexcExecutionError, _limit_terms_from_quote_budget, activate_spot_execution, submit_anchored_spot_limit,
)
from app.live_execution.mexc_live_order import MexcOrderAcknowledgement, MexcOrderSendError
from app.live_execution.mexc_order_lifecycle import synchronize_spot_order
from app.live_execution.policy import evaluate_spot_intent
from app.portfolio.models import RealEvent, RealLiveSpotIntent
from app.portfolio.repository import account_snapshot
from tests.test_mexc_execution_state import ACCOUNT, MX_BALANCE, MX_OPENING, PASSWORD, UID, SOURCE, Venue, setup, inputs, buy


class ExchangeOrders:
    """Transporte falso; sólo la capa real testada en otros tests firma HTTP."""

    def __init__(self, venue, clock):
        self.venue, self.clock = venue, clock
        self.posts = []
        self.after_test = None
        self.timeout_after_accepting = False
        self.orders = {}
        self.venue.order_by_client_id = self.order_by_client_id
        self.venue.trades_for_order = self.trades_for_order

    def pair_view_account(self, uid):
        assert uid == UID

    def test_reserved_limit(self, intent, recorded, preflight):
        assert recorded.status == "RESERVED" and preflight.allowed
        if self.after_test:
            self.after_test()

    def place_limit_order(self, intent):
        self.posts.append(intent.intent_id)
        order_id = "order-" + intent.intent_id
        with localcontext() as context:
            context.prec = 300
            quote = intent.quantity_base * intent.limit_price_quote
            fee = Decimal("0.02")
            base_delta = intent.quantity_base if intent.side == "BUY" else -intent.quantity_base
            quote_delta = -quote if intent.side == "BUY" else quote
            self.venue.base = str(Decimal(self.venue.base) + base_delta)
            self.venue.quote = str(Decimal(self.venue.quote) + quote_delta - fee)
        fill = {"id":"fill-" + intent.intent_id, "order_id":order_id, "side":intent.side,
                "time_ms": int(self.clock().timestamp()*1000), "price":str(intent.limit_price_quote),
                "quantity":str(intent.quantity_base), "quote_quantity":str(quote),
                "commission":"0.02", "commission_asset":"USDT"}
        self.venue.trades.append(fill)
        self.orders[intent.intent_id.replace("-", "")] = {
            **SOURCE, "symbol":intent.symbol, "client_order_id":intent.intent_id.replace("-", ""),
            "order_id":order_id, "type":"LIMIT", "side":intent.side,
            "price":str(intent.limit_price_quote), "quantity":str(intent.quantity_base),
            "status":"FILLED", "executed_quantity":str(intent.quantity_base), "cumulative_quote":str(quote)}
        if self.timeout_after_accepting:
            raise MexcOrderSendError("Resultado incierto; consultar la orden")
        return MexcOrderAcknowledgement(intent.intent_id, intent.intent_id.replace("-", ""), order_id)

    def order_by_client_id(self, symbol, client_id):
        assert self.orders[client_id]["symbol"] == symbol
        return self.orders[client_id]

    def trades_for_order(self, symbol, order_id):
        return {**SOURCE, "symbol":symbol, "order_id":order_id,
                "coverage":"ORDER_ID_BELOW_PAGE_LIMIT", "limit":1000,
                "trades":[row for row in self.venue.trades if row["order_id"] == order_id]}


def send(vault, policy, venue, clock, trader, request_id, *, side="BUY", quantity="0.001"):
    return submit_anchored_spot_limit(
        vault, venue, trader, policy.policy_id, request_id, side=side,
        quantity_base=Decimal(quantity), limit_price_quote=Decimal("65000"),
        clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)


def test_buy_and_sell_use_reconciled_inputs_and_record_cash_fees_and_pnl(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    trader = ExchangeOrders(venue, clock)
    purchase = send(vault, policy, venue, clock, trader, str(uuid4()))
    assert purchase["journal_status"] == "RESERVATION_CLOSED", purchase
    assert purchase["synchronization"]["book_inventory_matched"] is True
    sale = send(vault, policy, venue, clock, trader, str(uuid4()), side="SELL", quantity="0.0009")
    assert sale["journal_status"] == "RESERVATION_CLOSED", sale
    assert len(trader.posts) == 2
    with vault.session() as db:
        book = account_snapshot(db, account)
        assert book.cash["USDT"] == Decimal("693.46")
        assert book.positions[("BTC", "USDT")].quantity == Decimal("0.0001")
        assert book.positions[("BTC", "USDT")].realized_pnl == Decimal("-0.038")
        assert db.query(RealEvent).count() == 3
    vault.lock()


def test_quote_budget_rounds_prices_by_side_and_uses_the_common_lot(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path)
    evidence = inputs(vault, policy, venue, clock)
    evidence = replace(evidence, state=replace(evidence.state, best_bid=Decimal("99.998"),
        best_ask=Decimal("100.001")), rules=replace(evidence.rules, base_precision=3,
        base_step=Decimal("0.0025")))
    for side, budget, quantity, limit in (
        ("BUY", "1.000600049999999999", "0.005", "100.01"),
        ("BUY", "1.00060005", "0.01", "100.01"),
        ("SELL", "0.999899999999999999", "0.005", "99.99"),
        ("SELL", "0.9999", "0.01", "99.99"),
    ):
        assert _limit_terms_from_quote_budget(side, Decimal(budget), evidence) == (Decimal(quantity), Decimal(limit))
    vault.lock()


def test_manual_buy_budget_survives_fee_changes_and_idempotent_retries(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path)
    trader = ExchangeOrders(venue, clock)
    request_id = str(uuid4())
    cap = Decimal("65.09")

    def place(identifier, amount, rules_fetch):
        return submit_anchored_spot_limit(vault, venue, trader, policy.policy_id, identifier,
            side="BUY", quantity_base=Decimal("0.001"), limit_price_quote=Decimal("65001"),
            max_spend_quote=amount, clock=clock, quote_get=venue.quote_get, rules_fetch=rules_fetch)

    def higher_fee(symbol, now):
        return replace(venue.rules_fetch(symbol, now), fee_rate_upper=Decimal("0.0015"))

    refused = place(request_id, cap, higher_fee)
    assert refused["stage"] == "REVIEW_REQUIRED" and refused["journal_status"] == "REJECTED"
    assert "supera el presupuesto" in refused["detail"] and not trader.posts
    assert recover_spot_intent(vault, request_id).max_spend_quote == cap
    assert place(request_id, cap, venue.rules_fetch)["replayed_without_post"]
    with pytest.raises(MexcExecutionError, match="otros parámetros"):
        place(request_id, cap + Decimal("1"), venue.rules_fetch)

    accepted_id = str(uuid4())
    assert place(accepted_id, cap, venue.rules_fetch)["journal_status"] == "RESERVATION_CLOSED"
    assert trader.posts == [accepted_id]
    vault.lock()
    vault.unlock(PASSWORD)
    assert recover_spot_intent(vault, accepted_id).max_spend_quote == cap
    vault.lock()


def test_same_request_id_never_sends_again_after_success(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    trader = ExchangeOrders(venue, clock)
    request_id = str(uuid4())
    assert send(vault, policy, venue, clock, trader, request_id)["journal_status"] == "RESERVATION_CLOSED"
    again = send(vault, policy, venue, clock, trader, request_id)
    assert again["replayed_without_post"] and not again["new_order_post_attempted"]
    assert len(trader.posts) == 1
    with pytest.raises(MexcExecutionError, match="otros parámetros"):
        send(vault, policy, venue, clock, trader, request_id, quantity="0.0009")
    vault.lock()


def test_timeout_after_exchange_acceptance_requires_observation_never_repost(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    trader = ExchangeOrders(venue, clock)
    trader.timeout_after_accepting = True
    request_id = str(uuid4())
    result = send(vault, policy, venue, clock, trader, request_id)
    assert result["journal_status"] == "SUBMISSION_UNKNOWN" and result["stage"] == "REVIEW_REQUIRED"
    assert send(vault, policy, venue, clock, trader, request_id)["replayed_without_post"]
    assert len(trader.posts) == 1
    synced = synchronize_spot_order(vault, venue, request_id, account, clock=clock)
    assert synced["stage"] == "COMPLETED"
    with vault.session() as db:
        assert db.query(RealEvent).count() == 2
    vault.lock()


@pytest.mark.parametrize("change", ["balance", "stop"])
def test_fresh_read_after_order_test_blocks_changed_balance_or_stop(tmp_path, change):
    vault, policy, account, venue, clock = setup(tmp_path)
    trader = ExchangeOrders(venue, clock)
    trader.after_test = (lambda: setattr(venue, "quote", "701")) if change == "balance" else (
        lambda: stop_spot_account(vault, ACCOUNT, now=clock()))
    result = send(vault, policy, venue, clock, trader, str(uuid4()))
    assert result["stage"] == "REVIEW_REQUIRED" and trader.posts == []
    vault.lock()


def test_enabling_third_asset_mx_fees_after_order_test_prevents_send(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path, book_movements=(MX_OPENING,), other=(MX_BALANCE.copy(),))
    trader = ExchangeOrders(venue, clock)
    trader.after_test = lambda: setattr(venue, "pay_fees_with_mx", True)
    result = send(vault, policy, venue, clock, trader, str(uuid4()))
    assert result["stage"] == "REVIEW_REQUIRED" and trader.posts == []
    assert venue.calls.count("mx_deduct") == 2
    vault.lock()


def test_activation_preserves_draft_and_links_a_new_daily_policy_to_the_opening(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    draft = replace(policy, policy_id=str(uuid4()), enabled=False,
                    reconciliation_anchor_id=None, allow_risk_reducing_exits=False,
                    expires_at=clock.value + timedelta(days=7))
    register_spot_policy(vault, draft, now=clock())
    trader = ExchangeOrders(venue, clock)
    active = activate_spot_execution(
        vault, venue, trader, draft.policy_id, policy.reconciliation_anchor_id, str(uuid4()),
        clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    assert active.enabled and active.allow_risk_reducing_exits
    assert active.max_order_quote == draft.max_order_quote
    assert active.expires_at < draft.expires_at
    assert active.reconciliation_anchor_id == policy.reconciliation_anchor_id
    assert trader.posts == []
    with vault.session() as db:
        from app.live_execution.journal import _stored_policy
        assert _stored_policy(db, draft.policy_id) == draft
        assert _stored_policy(db, active.policy_id) == active
    vault.lock()


def test_risk_reducing_sale_can_exit_after_loss_or_turnover_limit_but_cannot_oversell(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    state = replace(inputs(vault, policy, venue, clock).state,
                    free_base=Decimal("0.01"), average_cost_quote_per_base=Decimal("65000"),
                    daily_realized_pnl_quote=Decimal("-100"),
                    unrealized_loss_quote=Decimal("100"), daily_turnover_quote=Decimal("5000"))
    order = buy(policy, clock)
    assert not evaluate_spot_intent(policy, order, state, now=clock()).allowed
    sale = replace(order, side="SELL")
    assert evaluate_spot_intent(policy, sale, state, now=clock()).allowed
    assert not evaluate_spot_intent(policy, replace(sale, quantity_base=Decimal("1")), state, now=clock()).allowed
    vault.lock()


def test_unrealized_loss_by_itself_blocks_another_purchase(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    state = replace(inputs(vault, policy, venue, clock).state, unrealized_loss_quote=Decimal("51"))
    decision = evaluate_spot_intent(policy, buy(policy, clock), state, now=clock())
    assert not decision.allowed and decision.reason == "DAILY_LOSS_LIMIT"
    vault.lock()
