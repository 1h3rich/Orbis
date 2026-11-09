"""Orden propia → fills → libro → reserva, con View ficticio y sin red."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.live_execution.journal import begin_spot_submission, register_spot_policy, reserve_spot_intent
from app.live_execution.mexc_order_lifecycle import MexcLifecycleError, synchronize_spot_order
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.live_execution.reconciliation_view import list_spot_execution_intents
from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.models import RealEvent, RealLiveSpotIntent, RealLiveSpotSettlement, RealMexcAccountLink
from app.portfolio.repository import append_event, create_account, account_snapshot
from app.portfolio.vault import RealVault


PASSWORD = "isolated lifecycle vault password"
ACCOUNT = "a" * 64
UID = "123456789"
SOURCE = {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED"}


class Venue:
    """Sólo ofrece lecturas: no existe transporte para enviar órdenes/retiros."""

    def __init__(self, intent, *, capital="700", fee_asset="USDT", fee="0.05"):
        self.intent = intent
        self.calls = []
        self.uid = UID
        self.ref = ACCOUNT
        self.status = "FILLED"
        self.capital = Decimal(capital)
        self.fee_asset, self.fee = fee_asset, Decimal(fee)
        self.quote_adjustment = Decimal(0)
        self.extra_balances = []
        self.fail_balances = False
        self.after_order = None

    def account_reference(self):
        self.calls.append("reference")
        return self.ref

    def account_uid(self):
        self.calls.append("uid")
        return self.uid

    def fills(self):
        if self.status in ("NEW", "CANCELED"):
            return []
        quantity = "0.002" if self.status == "PARTIALLY_FILLED" else "0.01"
        quote = "130" if self.status == "PARTIALLY_FILLED" else str(
            self.intent.quantity_base * self.intent.limit_price_quote)
        if self.status != "PARTIALLY_FILLED":
            quantity = str(self.intent.quantity_base)
        return [{"id": "fill-1", "order_id": "order-1", "side": self.intent.side,
                 "time_ms": int(self.intent.signal_at.timestamp() * 1000),
                 "price": "65000", "quantity": quantity, "quote_quantity": quote,
                 "commission": str(self.fee), "commission_asset": self.fee_asset}]

    def order_by_client_id(self, symbol, client_id):
        self.calls.append("order")
        assert symbol == self.intent.symbol and client_id == self.intent.intent_id.replace("-", "")
        fills = self.fills()
        if self.after_order:
            self.after_order()
        return {**SOURCE, "symbol": symbol, "client_order_id": client_id,
                "order_id": "order-1", "type": "LIMIT", "side": self.intent.side,
                "price": str(self.intent.limit_price_quote),
                "quantity": str(self.intent.quantity_base), "status": self.status,
                "executed_quantity": fills[0]["quantity"] if fills else "0",
                "cumulative_quote": fills[0]["quote_quantity"] if fills else "0"}

    def trades_for_order(self, symbol, order_id):
        self.calls.append("fills")
        return {**SOURCE, "coverage": "ORDER_ID_BELOW_PAGE_LIMIT", "symbol": symbol,
                "order_id": order_id, "trades": self.fills()}

    def balances(self):
        self.calls.append("balances")
        if self.fail_balances:
            raise RuntimeError("private upstream response fixture-secret")
        base = Decimal("0.01") if self.fills() else Decimal(0)
        quote = self.capital - (Decimal("650") if self.fills() else Decimal(0))
        if self.fills():
            if self.fee_asset == "USDT":
                quote -= self.fee
            elif self.fee_asset == "BTC":
                base -= self.fee
        quote += self.quote_adjustment
        return {**SOURCE, "observed_at": datetime.now(timezone.utc).isoformat(), "balances": [
            {"asset": "BTC", "free": str(base), "locked": "0", "total": str(base)},
            {"asset": "USDT", "free": str(quote), "locked": "0", "total": str(quote)},
            *self.extra_balances,
        ]}


def setup(tmp_path, *, capital="700", fee_asset="USDT", fee="0.05", submitted=True):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    now = datetime.now(timezone.utc) - timedelta(seconds=2)
    with vault.session() as db:
        account = create_account(db, "MEXC", "USDT")
        account_id = account.id
        if Decimal(capital):
            append_event(db, account_id, str(uuid4()), EventInput(
                kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal(capital),
                occurred_at=now - timedelta(days=1)))
        db.add(RealMexcAccountLink(account_ref=ACCOUNT, account_id=account_id, created_at=now))
        db.commit()
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("1000"), max_daily_turnover_quote=Decimal("5000"),
        max_exposure_quote=Decimal("5000"), max_daily_loss_quote=Decimal("500"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=now + timedelta(hours=1), enabled=True)
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id, strategy_version_id="v1",
        account_ref=ACCOUNT, symbol="BTCUSDT", side="BUY", quantity_base=Decimal("0.01"),
        limit_price_quote=Decimal("65000"), estimated_fee_quote=Decimal("0.65"), signal_at=now)
    state = LiveSpotState(
        account_ref=ACCOUNT, symbol="BTCUSDT", reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True, observed_at=now, quote_at=now,
        best_bid=Decimal("64999"), best_ask=Decimal("65000"), free_base=Decimal(0),
        free_quote=Decimal("1000"), pending_base=Decimal(0), pending_quote=Decimal(0),
        daily_turnover_quote=Decimal(0), pending_turnover_quote=Decimal(0),
        exposure_quote=Decimal(0), daily_realized_pnl_quote=Decimal(0))
    register_spot_policy(vault, policy, now=now)
    bind_uid_for_spot_submission(vault, ACCOUNT, UID, now=now)
    reserve_spot_intent(vault, intent, state, now=now)
    if submitted:
        begin_spot_submission(vault, intent, state, now=now)
    return vault, intent, account_id, Venue(intent, capital=capital, fee_asset=fee_asset, fee=fee)


def test_sync_final_order_books_once_closes_matching_balance_and_survives_restart(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    result = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert result["stage"] == "COMPLETED"
    assert result["booked"] and result["reservation_closed"] and result["book_inventory_matched"]
    assert result["orders_enabled"] is False
    with vault.session() as db:
        book = account_snapshot(db, account)
        assert book.cash["USDT"] == Decimal("49.95")
        assert book.positions[("BTC", "USDT")].quantity == Decimal("0.01")
        assert db.query(RealLiveSpotSettlement).count() == 1
        assert db.query(RealEvent).count() == 2
    vault.lock()
    vault.unlock(PASSWORD)
    venue.calls.clear()
    replay = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert replay["stage"] == "ALREADY_COMPLETED" and replay["replayed_without_exchange"]
    assert replay["book_inventory_matched"] is None  # No lectura nueva del saldo.
    assert venue.calls == []
    with vault.session() as db:
        assert db.query(RealEvent).count() == 2
    vault.lock()


@pytest.mark.parametrize("status", ["NEW", "PARTIALLY_FILLED"])
def test_open_order_only_archives_observation_and_retains_reservation(tmp_path, status):
    vault, intent, account, venue = setup(tmp_path)
    venue.status = status
    result = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert result["stage"] == "ORDER_OPEN" and result["external_status"] == status
    assert not result["booked"] and not result["reservation_closed"]
    progress = list_spot_execution_intents(vault)[0]["progress"]
    assert progress == {"requested_base":"0.01", "executed_base":"0.002" if status == "PARTIALLY_FILLED" else "0",
                        "unfilled_base":"0.008" if status == "PARTIALLY_FILLED" else "0.01", "unfilled_state":"OPEN"}
    with vault.session() as db:
        assert db.query(RealEvent).count() == 1
        assert db.get(RealLiveSpotIntent, intent.intent_id).reserved_quote > 0
    vault.lock()


def test_balance_difference_keeps_fill_booking_and_retry_does_not_duplicate_it(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    venue.quote_adjustment = Decimal("0.00000000000000000001")
    result = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert result["stage"] == "SETTLEMENT_REVIEW_REQUIRED" and result["booked"]
    assert "no coincide" in result["detail"] and not result["reservation_closed"]
    with vault.session() as db:
        assert db.query(RealEvent).count() == 2
        assert db.query(RealLiveSpotSettlement).count() == 0
    venue.quote_adjustment = Decimal(0)
    assert synchronize_spot_order(vault, venue, intent.intent_id, account)["stage"] == "COMPLETED"
    with vault.session() as db:
        assert db.query(RealEvent).count() == 2
    vault.lock()


def test_tampered_order_terms_hide_progress(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    venue.status = "PARTIALLY_FILLED"
    synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert list_spot_execution_intents(vault)[0]["progress"] is not None
    with vault.session() as db:
        db.get(RealLiveSpotIntent, intent.intent_id).quantity_base = Decimal("0.03")
        db.commit()
    assert list_spot_execution_intents(vault)[0]["progress"] is None
    vault.lock()


def test_read_failure_after_booking_is_recoverable_without_repeated_purchase(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    venue.fail_balances = True
    result = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert result["stage"] == "SETTLEMENT_REVIEW_REQUIRED" and result["booked"]
    assert "fixture-secret" not in str(result)
    venue.fail_balances = False
    assert synchronize_spot_order(vault, venue, intent.intent_id, account)["stage"] == "COMPLETED"
    with vault.session() as db:
        assert db.query(RealEvent).count() == 2
    vault.lock()


def test_final_order_without_fills_closes_without_trade_event(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    venue.status = "CANCELED"
    result = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert result["stage"] == "COMPLETED" and not result["booked"]
    assert list_spot_execution_intents(vault)[0]["progress"] == {
        "requested_base":"0.01", "executed_base":"0", "unfilled_base":"0.01", "unfilled_state":"CANCELLED"}
    with vault.session() as db:
        assert db.query(RealEvent).count() == 1
    vault.lock()


def test_base_asset_fee_books_exact_net_units(tmp_path):
    vault, intent, account, venue = setup(tmp_path, fee_asset="BTC", fee="0.00000001")
    result = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert result["stage"] == "COMPLETED"
    with vault.session() as db:
        book = account_snapshot(db, account)
        assert book.positions[("BTC", "USDT")].quantity == Decimal("0.00999999")
        assert book.cash["USDT"] == Decimal("50")
    vault.lock()


def test_buy_then_sell_updates_cash_position_and_realized_pnl(tmp_path):
    vault, buy, account, venue = setup(tmp_path)
    assert synchronize_spot_order(vault, venue, buy.intent_id, account)["stage"] == "COMPLETED"
    now = datetime.now(timezone.utc)
    sell = replace(buy, intent_id=str(uuid4()), side="SELL", quantity_base=Decimal("0.009"),
                   estimated_fee_quote=Decimal("0.05"), signal_at=now)
    state = LiveSpotState(
        account_ref=ACCOUNT, symbol="BTCUSDT", reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True, observed_at=now, quote_at=now,
        best_bid=Decimal("65000"), best_ask=Decimal("65001"), free_base=Decimal("0.01"),
        free_quote=Decimal("49.95"), pending_base=Decimal(0), pending_quote=Decimal(0),
        daily_turnover_quote=Decimal("650"), pending_turnover_quote=Decimal(0),
        exposure_quote=Decimal("650"), daily_realized_pnl_quote=Decimal(0),
        average_cost_quote_per_base=Decimal("65005"))
    assert reserve_spot_intent(vault, sell, state, now=now).status == "RESERVED"
    begin_spot_submission(vault, sell, state, now=now)

    class SellVenue(Venue):
        def fills(self):
            return [{**row, "id": "sell-fill", "order_id": "sell-order"}
                    for row in super().fills()]

        def order_by_client_id(self, symbol, client_id):
            return {**super().order_by_client_id(symbol, client_id), "order_id": "sell-order"}

        def balances(self):
            return {**SOURCE, "observed_at": datetime.now(timezone.utc).isoformat(), "balances": [
                {"asset": "BTC", "free": "0.001", "locked": "0", "total": "0.001"},
                {"asset": "USDT", "free": "634.90", "locked": "0", "total": "634.90"}]}

    result = synchronize_spot_order(vault, SellVenue(sell), sell.intent_id, account)
    assert result["stage"] == "COMPLETED"
    with vault.session() as db:
        book = account_snapshot(db, account)
        position = book.positions[("BTC", "USDT")]
        assert position.quantity == Decimal("0.001")
        assert position.realized_pnl == Decimal("-0.095")
        assert book.cash["USDT"] == Decimal("634.90")
        assert db.query(RealEvent).count() == 3
        assert db.query(RealLiveSpotSettlement).count() == 2
    vault.lock()


def test_other_asset_mismatch_also_keeps_reservation(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    venue.extra_balances = [{"asset": "ETH", "free": "1", "locked": "0", "total": "1"}]
    assert synchronize_spot_order(vault, venue, intent.intent_id, account)["stage"] == "SETTLEMENT_REVIEW_REQUIRED"
    vault.lock()


def test_wrong_account_or_unsubmitted_intent_is_rejected_before_network(tmp_path):
    vault, intent, account, venue = setup(tmp_path, submitted=False)
    for target in (account, str(uuid4())):
        with pytest.raises(MexcLifecycleError):
            synchronize_spot_order(vault, venue, intent.intent_id, target)
    assert venue.calls == []
    vault.lock()


def test_tampered_book_is_rejected_before_network(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    with vault.session() as db:
        db.query(RealEvent).update({RealEvent.note: "changed after recording"})
        db.commit()
    with pytest.raises(MexcLifecycleError, match="libro REAL"):
        synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert venue.calls == []
    vault.lock()


def test_uid_change_during_order_read_cannot_archive_or_book(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    venue.after_order = lambda: setattr(venue, "uid", "999999999")
    with pytest.raises(MexcLifecycleError, match="identidad"):
        synchronize_spot_order(vault, venue, intent.intent_id, account)
    with vault.session() as db:
        assert db.query(RealEvent).count() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_order_observations").scalar_one() == 0
    vault.lock()


def test_insufficient_book_cash_requests_review_then_resumes_after_funding_is_recorded(tmp_path):
    vault, intent, account, venue = setup(tmp_path, capital="0")
    venue.capital = Decimal("700")
    result = synchronize_spot_order(vault, venue, intent.intent_id, account)
    assert result["stage"] == "BOOK_REVIEW_REQUIRED" and not result["booked"]
    with vault.session() as db:
        append_event(db, account, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=intent.signal_at - timedelta(days=1)))
    assert synchronize_spot_order(vault, venue, intent.intent_id, account)["stage"] == "COMPLETED"
    vault.lock()


def test_api_sync_requires_local_origin_vault_view_and_exact_account(tmp_path):
    vault, intent, account, venue = setup(tmp_path)
    vault.lock()
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db", mexc_factory=lambda *_: venue))
    client = TestClient(app)
    url = f"/real/execution/mexc/intents/{intent.intent_id}/synchronize"
    assert client.post(url, json={"account_id": account}).status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post(url, headers=headers, json={"account_id": account}).status_code == 409
    assert client.post("/real/exchanges/mexc/connect", headers=headers,
                       json={"api_key": "fixture-key", "secret_key": "fixture-secret"}).status_code == 200
    assert client.post(url, headers={**headers, "Origin": "https://outside.invalid"},
                       json={"account_id": account}).status_code == 403
    assert client.post(url, headers=headers, json={"account_id": str(uuid4())}).status_code == 409
    result = client.post(url, headers=headers, json={"account_id": account})
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["stage"] == "COMPLETED"
    assert client.post(url, headers=headers, json={"account_id": account}).json()["stage"] == "ALREADY_COMPLETED"
    client.post("/real/vault/lock", headers=headers)
