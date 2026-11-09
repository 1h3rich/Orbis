"""Estado de envío derivado de apertura, libro y MEXC controlado, nunca de flags UI."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.live_execution.journal import register_spot_policy, reserve_spot_intent, _stored_policy
from app.live_execution.mexc_execution_state import MexcExecutionStateError, fetch_spot_execution_inputs
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, evaluate_spot_intent
from app.portfolio.domain import EventInput
from app.portfolio.mexc_parity import assess_mexc_spot_parity
from app.portfolio.mexc_parity_archive import add_observation
from app.portfolio.models import RealEvent, RealLiveSpotIntent
from app.portfolio.repository import account_book_revision, account_snapshot, append_event, create_account
from app.portfolio.vault import RealVault


ACCOUNT = "a" * 64
UID = "123456789"
PASSWORD = "isolated execution opening password"
SOURCE = {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED"}
ETH_OPENING = {"kind": "ADJUSTMENT_IN", "quote_currency": "USDT", "asset": "ETH",
               "quantity": Decimal("1.25"), "unit_price": Decimal("2400"), "note": "Saldo anterior"}
ETH_BALANCE = {"asset": "ETH", "free": "1.25", "locked": "0", "total": "1.25"}
MX_OPENING = {**ETH_OPENING, "asset": "MX", "unit_price": Decimal("2")}
MX_BALANCE = {**ETH_BALANCE, "asset": "MX"}


class Clock:
    def __init__(self):
        self.value = datetime.now(timezone.utc) + timedelta(seconds=1)

    def __call__(self):
        self.value += timedelta(milliseconds=100)
        return self.value


class Venue:
    def __init__(self, clock):
        self.clock = clock
        self.ref, self.uid = ACCOUNT, UID
        self.base, self.quote = "0", "700"
        self.locked = "0"
        self.other = []
        self.orders = []
        self.trades = []
        self.deposits = []
        self.withdrawals = []
        self.calls = []
        self.on_window = None
        self.pay_fees_with_mx = False

    def account_reference(self):
        return self.ref

    def account_uid(self):
        return self.uid

    def mx_deduct_enabled(self):
        self.calls.append("mx_deduct")
        return self.pay_fees_with_mx

    def balances(self):
        self.calls.append("balances")
        return {**SOURCE, "observed_at": self.clock().isoformat(), "balances": [
            {"asset": "BTC", "free": self.base, "locked": "0", "total": self.base},
            {"asset": "USDT", "free": self.quote, "locked": self.locked,
             "total": str(Decimal(self.quote) + Decimal(self.locked))}, *self.other]}

    def open_orders(self, symbol):
        return {**SOURCE, "symbol": symbol, "coverage": "ALL_OPEN_ORDERS_FOR_ONE_SYMBOL",
                "observed_at": self.clock().isoformat(), "orders": self.orders}

    def open_orders_unfiltered(self):
        return {**SOURCE, "symbol": None, "coverage": "UNFILTERED_COVERAGE_UNPROVEN",
                "observed_at": self.clock().isoformat(), "orders": self.orders}

    def trades_window(self, symbol, *, days):
        now = self.clock()
        if self.on_window:
            self.on_window()
        return {**SOURCE, "symbol": symbol, "limit": 1000, "pages": 1,
                "coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT",
                "start_ms": int((now - timedelta(days=days)).timestamp() * 1000),
                "end_ms": int(now.timestamp() * 1000), "observed_at": now.isoformat(),
                "trades": list(self.trades)}

    def transfers_recent(self, *, days):
        now = self.clock()
        return {**SOURCE, "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
                "start_ms": int((now - timedelta(days=days)).timestamp() * 1000),
                "end_ms": int(now.timestamp() * 1000), "observed_at": now.isoformat(),
                "deposits": list(self.deposits), "withdrawals": list(self.withdrawals)}

    def quote_get(self, symbol):
        return {"symbol": symbol, "closeTime": int(self.clock.value.timestamp() * 1000),
                "bidPrice": "65000", "askPrice": "65001", "lastPrice": "65000"}

    def rules_fetch(self, symbol, now):
        return MexcSpotRules(
            symbol=symbol, base_asset="BTC", quote_asset="USDT", observed_at=now,
            buy_allowed=True, sell_allowed=True, base_precision=6, quote_precision=2,
            base_step=Decimal("0.000001"), min_quote=Decimal("1"), max_quote=Decimal("10000"),
            fee_rate_upper=Decimal("0.0005"), bid_multiplier_up=None, ask_multiplier_down=None)


def setup(tmp_path, *, book_movements=(), base="0", quote="700", other=()):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    clock = Clock()
    venue = Venue(clock)
    venue.base, venue.quote, venue.other = base, quote, list(other)
    with vault.session() as db:
        account = create_account(db, "MEXC", "USDT")
        account_id = account.id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=clock.value - timedelta(days=1)))
        for movement in book_movements:
            append_event(db, account_id, str(uuid4()), EventInput(
                occurred_at=clock.value - timedelta(hours=23), **movement))
    # La API real crea este vínculo tras comprobar el UID; aquí usamos el mismo guardián.
    from app.portfolio.models import RealMexcAccountLink
    with vault.session() as db:
        db.add(RealMexcAccountLink(account_ref=ACCOUNT, account_id=account_id, created_at=clock()))
        db.commit()
    bind_uid_for_spot_submission(vault, ACCOUNT, UID, now=clock())
    first, second = venue.balances(), venue.balances()
    anchor_id = str(uuid4())
    with vault.session() as db:
        revision = account_book_revision(db, account_id)
        report = assess_mexc_spot_parity(
            account_snapshot(db, account_id), first, second,
            book_revision_before=revision, book_revision_after=revision, now=clock())
        add_observation(db, anchor_id, account_id, ACCOUNT, report)
        db.commit()
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="manual-v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"), max_data_age_seconds=60,
        expires_at=clock.value + timedelta(hours=1), enabled=True,
        reconciliation_anchor_id=anchor_id, allow_risk_reducing_exits=True)
    register_spot_policy(vault, policy, now=clock())
    venue.calls.clear()
    return vault, policy, account_id, venue, clock


def inputs(vault, policy, venue, clock, **kwargs):
    return fetch_spot_execution_inputs(vault, venue, policy, clock=clock,
                                        quote_get=venue.quote_get, rules_fetch=venue.rules_fetch, **kwargs)


def buy(policy, clock, *, request_id=None):
    return LiveSpotIntent(
        intent_id=request_id or str(uuid4()), policy_id=policy.policy_id,
        strategy_version_id=policy.strategy_version_id, account_ref=ACCOUNT, symbol="BTCUSDT",
        side="BUY", quantity_base=Decimal("0.001"), limit_price_quote=Decimal("65000"),
        estimated_fee_quote=Decimal("0.05"), signal_at=clock())


def test_state_is_built_from_opening_book_and_two_fresh_venue_reads(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    fresh = inputs(vault, policy, venue, clock)
    assert fresh.state.reconciled and fresh.state.account_verified
    assert fresh.state.free_quote == Decimal("700")
    assert fresh.state.exposure_quote == 0 and fresh.state.unrealized_loss_quote == 0
    assert evaluate_spot_intent(policy, buy(policy, clock), fresh.state, now=clock()).allowed
    with vault.session() as db:
        assert _stored_policy(db, policy.policy_id) == policy
        assert db.query(RealLiveSpotIntent).count() == 0
    vault.lock()


def test_missing_opening_is_not_authorization_even_if_balances_match(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    with pytest.raises(MexcExecutionStateError, match="apertura"):
        inputs(vault, replace(policy, reconciliation_anchor_id=None), venue, clock)
    assert venue.calls == []
    vault.lock()


def test_book_change_before_opening_is_detected_before_any_exchange_read(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    with vault.session() as db:
        # Reescribir incluso recorded_at invalida el prefijo comprometido.
        row = db.query(RealEvent).one()
        row.recorded_at = clock()
        db.commit()
    with pytest.raises(MexcExecutionStateError, match="apertura cambió"):
        inputs(vault, policy, venue, clock)
    assert venue.calls == []
    vault.lock()


def test_new_manual_funding_requires_another_reviewed_opening(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    with vault.session() as db:
        event = append_event(db, account, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("100"),
            occurred_at=clock()))
        db.get(RealEvent, event.id).recorded_at = clock()
        db.commit()
    venue.quote = "800"
    with pytest.raises(MexcExecutionStateError, match="movimientos nuevos"):
        inputs(vault, policy, venue, clock)
    vault.lock()


@pytest.mark.parametrize("change", ["balance", "locked", "other", "order", "uid"])
def test_conflicting_venue_state_never_authorizes(tmp_path, change):
    vault, policy, account, venue, clock = setup(tmp_path)
    if change == "balance": venue.quote = "700.0000000000000000001"
    elif change == "locked": venue.locked = "1"
    elif change == "other": venue.other = [{"asset":"ETH", "free":"1", "locked":"0", "total":"1"}]
    elif change == "order": venue.orders = [{"symbol":"BTCUSDT", "order_id":"external", "client_order_id":"other", "status":"NEW"}]
    else: venue.uid = "987654321"
    with pytest.raises(MexcExecutionStateError):
        inputs(vault, policy, venue, clock)
    vault.lock()


def test_other_mexc_asset_is_verified_without_increasing_pair_budget(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path, book_movements=(ETH_OPENING,), other=(ETH_BALANCE.copy(),))
    fresh = inputs(vault, policy, venue, clock).state
    assert fresh.reconciled and fresh.free_quote == Decimal("700")
    assert fresh.free_base == 0 and fresh.exposure_quote == 0
    assert "mx_deduct" not in venue.calls
    venue.other[0].update(free="1.26", total="1.26")
    with pytest.raises(MexcExecutionStateError, match="no coincide con el libro REAL"):
        inputs(vault, policy, venue, clock)
    venue.other[0].update(free="1.00", locked="0.25", total="1.25")
    with pytest.raises(MexcExecutionStateError, match="fondos libres"):
        inputs(vault, policy, venue, clock)
    vault.lock()


def test_mx_as_an_unrelated_asset_requires_fee_payment_disabled(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path, book_movements=(MX_OPENING,), other=(MX_BALANCE.copy(),))
    assert inputs(vault, policy, venue, clock).state.reconciled
    assert venue.calls.count("mx_deduct") == 1
    venue.pay_fees_with_mx = True
    with pytest.raises(MexcExecutionStateError, match="Desactiva el pago"):
        inputs(vault, policy, venue, clock)
    venue.pay_fees_with_mx = None
    with pytest.raises(MexcExecutionStateError, match="no confirmó el ajuste"):
        inputs(vault, policy, venue, clock)
    vault.lock()


@pytest.mark.parametrize("movement,base,quote", [
    ({"kind": "CONTRIBUTION", "quote_currency": "BTC", "amount": Decimal("1")}, "1", "700"),
    ({"kind": "ADJUSTMENT_IN", "quote_currency": "EUR", "asset": "BTC", "quantity": Decimal("1"),
      "unit_price": Decimal("60000"), "note": "Coste en euros"}, "1", "700"),
    ({"kind": "ADJUSTMENT_IN", "quote_currency": "EUR", "asset": "USDT", "quantity": Decimal("1"),
      "unit_price": Decimal("1"), "note": "Lote en euros"}, "0", "701"),
], ids=["base-as-cash", "base-in-other-quote", "quote-as-position"])
def test_pair_balances_must_remain_in_their_accounting_buckets(tmp_path, movement, base, quote):
    vault, policy, _, venue, clock = setup(tmp_path, book_movements=(movement,), base=base, quote=quote)
    with pytest.raises(MexcExecutionStateError, match="no coincide con el libro REAL"):
        inputs(vault, policy, venue, clock)
    vault.lock()


def test_external_fill_after_opening_is_blocked_even_if_balance_returns_to_same_value(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    venue.trades = [{"id":"external", "order_id":"external-order", "side":"BUY",
                     "time_ms": int(clock().timestamp()*1000), "price":"65000", "quantity":"0.001",
                     "quote_quantity":"65", "commission":"0", "commission_asset":"USDT"}]
    with pytest.raises(MexcExecutionStateError, match="Ejecuciones MEXC pendientes"):
        inputs(vault, policy, venue, clock)
    vault.lock()


def test_current_unsent_reservation_is_refreshable_but_other_orders_are_blocked(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    first_state = inputs(vault, policy, venue, clock).state
    first, second = buy(policy, clock), buy(policy, clock)
    assert reserve_spot_intent(vault, first, first_state, now=clock()).status == "RESERVED"
    assert reserve_spot_intent(vault, second, first_state, now=clock()).reason == "ACCOUNT_ORDER_PENDING"
    with pytest.raises(MexcExecutionStateError, match="orden pendiente"):
        inputs(vault, policy, venue, clock)
    assert inputs(vault, policy, venue, clock, current_intent_id=first.intent_id).state.reconciled
    vault.lock()


def test_completed_deposit_before_opening_is_accepted_but_new_or_pending_is_not(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    before = clock.value - timedelta(hours=1)
    row = {"direction":"deposit", "asset":"USDT", "network":"ERC20", "amount":"700",
           "fee":"0", "status":5, "time_ms":int(before.timestamp()*1000)}
    venue.deposits = [row]
    assert inputs(vault, policy, venue, clock).state.reconciled
    row["status"] = 0
    with pytest.raises(MexcExecutionStateError, match="ingresos o retiradas"):
        inputs(vault, policy, venue, clock)
    row.update(status=5, time_ms=int(clock().timestamp()*1000))
    with pytest.raises(MexcExecutionStateError, match="ingresos o retiradas"):
        inputs(vault, policy, venue, clock)
    vault.lock()


def test_account_history_coverage_must_include_the_opening(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    clock.value += timedelta(days=1, seconds=1)
    with pytest.raises(MexcExecutionStateError):
        inputs(vault, policy, venue, clock)
    vault.lock()
