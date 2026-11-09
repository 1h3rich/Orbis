"""Auditoría MEXC por ventana con bóveda temporal; sin red ni claves."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event
import time
from uuid import uuid4

import pytest

from app.live_execution.journal import (
    begin_spot_submission, register_spot_policy, reserve_spot_intent,
)
from app.live_execution.mexc_booking import MexcBookingError, book_observed_spot_order
from app.live_execution.mexc_order_observer import (
    ObservedMexcOrder, archive_observed_spot_order,
)
from app.live_execution.mexc_trade_window_audit import (
    audit_spot_trade_window, observe_and_audit_spot_trade_window,
)
import app.live_execution.mexc_trade_window_audit as trade_audit
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.live_execution.reconciliation_view import list_spot_execution_intents
from app.portfolio.domain import EventInput
from app.portfolio.mexc_cost_provenance import audit_mexc_cost_provenance
from app.portfolio.mexc_observations import save_observation
from app.portfolio.models import MexcObservedFill, RealEvent, RealMexcAccountLink, RealMexcFillBooking
from app.portfolio.repository import append_event, create_account
from app.portfolio.vault import RealVault


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
FILL_TIME = int((NOW - timedelta(seconds=1)).timestamp() * 1000)


def setup(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("isolated trade window audit password", create=True)
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=30, expires_at=NOW + timedelta(hours=1), enabled=True,
    )
    return vault, policy


def window(*rows):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT", "symbol": "BTCUSDT",
            "limit": 1000, "pages": 1,
            "start_ms": int((NOW - timedelta(days=1)).timestamp() * 1000),
            "end_ms": int(NOW.timestamp() * 1000), "observed_at": NOW.isoformat(),
            "trades": list(rows)}


def fill():
    return {"id": "fill-1", "order_id": "external-1", "side": "BUY",
            "time_ms": FILL_TIME, "price": "100000", "quantity": "0.001",
            "quote_quantity": "100", "commission": "0.05",
            "commission_asset": "USDT"}


def archive_own_fill(vault, policy):
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id,
        strategy_version_id="v1", account_ref=ACCOUNT, symbol="BTCUSDT",
        side="BUY", quantity_base=Decimal("0.001"),
        limit_price_quote=Decimal("100000"), estimated_fee_quote=Decimal("0.1"),
        signal_at=NOW,
    )
    state = LiveSpotState(
        account_ref=ACCOUNT, symbol="BTCUSDT", reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True, observed_at=NOW,
        quote_at=NOW, best_bid=Decimal("99999"), best_ask=Decimal("100000"),
        free_base=Decimal(0), free_quote=Decimal("200"), pending_base=Decimal(0),
        pending_quote=Decimal(0), daily_turnover_quote=Decimal(0),
        pending_turnover_quote=Decimal(0), exposure_quote=Decimal(0),
        daily_realized_pnl_quote=Decimal(0),
    )
    register_spot_policy(vault, policy, now=NOW)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "RESERVED"
    begin_spot_submission(vault, intent, state, now=NOW)
    observed = ObservedMexcOrder(
        intent.intent_id, "external-1", "FILLED", Decimal("0.001"), Decimal("100"),
        (fill(),), (("USDT", Decimal("0.05")),), True,
    )
    archive_observed_spot_order(vault, intent, observed, now=NOW)
    return intent


def test_empty_recent_window_is_consistent_only_for_its_own_scope(tmp_path):
    vault, policy = setup(tmp_path)
    result = audit_spot_trade_window(vault, policy, window(), now=NOW)
    assert result.consistent_for_window and result.fill_count == 0
    assert result.reason == "WINDOW_ARCHIVE_AND_BOOKING_MATCH"
    assert not audit_spot_trade_window(vault, policy, window() | {
        "coverage": "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE"}, now=NOW).consistent_for_window
    assert audit_spot_trade_window(vault, policy, window() | {
        "end_ms": int((NOW - timedelta(minutes=1)).timestamp() * 1000)},
        now=NOW).reason == "TRADE_WINDOW_STALE"
    vault.lock()


def test_own_fill_requires_exact_archive_and_real_booking(tmp_path):
    vault, policy = setup(tmp_path)
    intent = archive_own_fill(vault, policy)
    current = window(fill())
    assert audit_spot_trade_window(vault, policy, current, now=NOW).reason == "FILL_BOOKING_PENDING"
    assert audit_spot_trade_window(vault, policy, window(), now=NOW).reason == "ARCHIVED_FILL_MISSING"
    changed = {**fill(), "price": "100001"}
    assert audit_spot_trade_window(vault, policy, window(changed), now=NOW).reason == "FILL_CONTENT_CHANGED"
    extra = {**fill(), "id": "external-fill", "order_id": "external-order"}
    assert audit_spot_trade_window(vault, policy, window(fill(), extra), now=NOW).reason == "FILL_NOT_ARCHIVED"
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("200"),
            occurred_at=NOW - timedelta(days=2),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)["status"] == "BOOKED"
    result = audit_spot_trade_window(vault, policy, current, now=NOW)
    assert result.consistent_for_window and result.fill_count == 1
    with vault.session() as db:
        source = audit_mexc_cost_provenance(
            db, account_id, ACCOUNT, "BTC", "USDT", now=NOW,
        )
    assert source.status == "LOCAL_FILL_CHAIN_MATCHES"
    assert source.asset_event_count == source.verified_fill_count == 1
    assert source.reconciled_with_venue is False and source.execution_ready is False
    with vault.session() as db:
        db.query(MexcObservedFill).one().price = Decimal("100001")
        db.commit()
        changed = audit_mexc_cost_provenance(db, account_id, ACCOUNT, "BTC", "USDT", now=NOW)
    assert changed.status == "UNLINKED_OR_CHANGED_TRADE"
    assert audit_spot_trade_window(vault, policy, current, now=NOW).reason == "ARCHIVE_INTEGRITY_FAILED"
    vault.lock()


def test_trade_window_audit_reads_one_snapshot_during_competing_booking_change(tmp_path, monkeypatch):
    vault, policy = setup(tmp_path)
    intent = archive_own_fill(vault, policy)
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("200"),
            occurred_at=NOW - timedelta(days=2),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)["status"] == "BOOKED"
    writer = RealVault(vault.path)
    writer.unlock("isolated trade window audit password")
    started = Event()
    original = trade_audit.observed_fill_digest_matches
    pending = []

    def change_event():
        with writer.session() as db:
            booking = db.query(RealMexcFillBooking).one()
            db.get(RealEvent, booking.event_id).fee = Decimal("0.06")
            started.set()
            db.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        def checked_then_competing_write(fill):
            result = original(fill)
            if not pending:
                pending.append(pool.submit(change_event))
                assert started.wait(timeout=2)
                time.sleep(0.1)
                assert not pending[0].done()
            return result

        monkeypatch.setattr(trade_audit, "observed_fill_digest_matches", checked_then_competing_write)
        before = audit_spot_trade_window(vault, policy, window(fill()), now=NOW)
        assert before.consistent_for_window is True
        pending[0].result(timeout=5)
        after = audit_spot_trade_window(vault, policy, window(fill()), now=NOW)
    assert after.reason == "FILL_BOOKING_MISMATCH"
    writer.lock()
    vault.lock()


def test_archived_external_fill_cannot_be_mistaken_for_own_order(tmp_path):
    vault, policy = setup(tmp_path)
    current = window(fill())
    with vault.session() as db:
        save_observation(db, ACCOUNT, current)
    assert audit_spot_trade_window(vault, policy, current, now=NOW).reason == "FILL_ORDER_NOT_OWNED"
    vault.lock()


def test_booked_fill_requires_matching_account_link_and_ledger_event(tmp_path):
    vault, policy = setup(tmp_path)
    intent = archive_own_fill(vault, policy)
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("200"),
            occurred_at=NOW - timedelta(days=2),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)["status"] == "BOOKED"
    current = window(fill())
    assert audit_spot_trade_window(vault, policy, current, now=NOW).consistent_for_window

    with vault.session() as db:
        other_id = create_account(db, "Otra cuenta", "USDT").id
        db.get(RealMexcAccountLink, ACCOUNT).account_id = other_id
        db.commit()
    assert audit_spot_trade_window(vault, policy, current, now=NOW).reason == "FILL_BOOKING_MISMATCH"

    with vault.session() as db:
        db.get(RealMexcAccountLink, ACCOUNT).account_id = account_id
        booking = db.query(RealMexcFillBooking).one()
        db.get(RealEvent, booking.event_id).fee = Decimal("0.06")
        db.commit()
    assert audit_spot_trade_window(vault, policy, current, now=NOW).reason == "FILL_BOOKING_MISMATCH"
    with vault.session() as db:
        source = audit_mexc_cost_provenance(db, account_id, ACCOUNT, "BTC", "USDT", now=NOW)
    assert source.status == "BOOK_EVENT_CHANGED"
    vault.lock()


def test_declared_cost_is_not_mistaken_for_verified_mexc_fill(tmp_path):
    vault, policy = setup(tmp_path)
    intent = archive_own_fill(vault, policy)
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("200"),
            occurred_at=NOW - timedelta(days=2),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)["status"] == "BOOKED"
    with vault.session() as db:
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="ADJUSTMENT_IN", quote_currency="USDT", asset="BTC",
            quantity=Decimal("0.001"), unit_price=Decimal("95000"),
            occurred_at=NOW, note="Coste inicial declarado",
        ))
        source = audit_mexc_cost_provenance(db, account_id, ACCOUNT, "BTC", "USDT", now=NOW)
    assert source.status == "DECLARED_OR_TRANSFERRED_COST"
    assert source.asset_event_count == 2 and source.verified_fill_count == 1
    assert source.execution_ready is False
    vault.lock()


@pytest.mark.parametrize("field,bad", [
    ("request_id", str(uuid4())),
    ("payload_digest", "0" * 64),
    ("note", "Asiento alterado"),
    ("gross_amount", Decimal("100.01")),
])
def test_repeated_booking_refuses_changed_ledger_event(tmp_path, field, bad):
    vault, policy = setup(tmp_path)
    intent = archive_own_fill(vault, policy)
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("200"),
            occurred_at=NOW - timedelta(days=2),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)["status"] == "BOOKED"
    assert audit_spot_trade_window(vault, policy, window(fill()), now=NOW).consistent_for_window
    with vault.session() as db:
        booking = db.query(RealMexcFillBooking).one()
        setattr(db.get(RealEvent, booking.event_id), field, bad)
        db.commit()
    with pytest.raises(MexcBookingError, match="ya no coincide"):
        book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)
    assert audit_spot_trade_window(vault, policy, window(fill()), now=NOW).reason == "FILL_BOOKING_MISMATCH"
    assert list_spot_execution_intents(vault)[0]["stage"] == "RECONCILIATION_INCONSISTENT"
    assert list_spot_execution_intents(vault)[0]["booked_to_portfolio"] is False
    vault.lock()


def test_view_fetch_failure_is_not_interpreted_as_empty_window(tmp_path):
    vault, policy = setup(tmp_path)
    bind_uid_for_spot_submission(vault, ACCOUNT, "22334455", now=NOW)

    class View:
        def account_reference(self):
            return ACCOUNT

        def account_uid(self):
            return "22334455"

        def trades_window(self, symbol, *, days):
            assert symbol == "BTCUSDT" and days == 1
            raise RuntimeError("private response that must not leak")

    result = observe_and_audit_spot_trade_window(vault, View(), policy, days=1, now=NOW)
    assert result.reason == "VIEW_UNAVAILABLE" and not result.consistent_for_window
    vault.lock()


def test_window_audit_requires_same_known_uid_on_both_sides_of_fetch(tmp_path):
    vault, policy = setup(tmp_path)
    seen = {"uid_calls": 0, "window_calls": 0, "uid_after": "22334455"}

    class View:
        def account_reference(self):
            return ACCOUNT

        def account_uid(self):
            seen["uid_calls"] += 1
            return "22334455" if seen["uid_calls"] % 2 else seen["uid_after"]

        def trades_window(self, symbol, *, days):
            seen["window_calls"] += 1
            assert symbol == policy.symbol and days == 1
            return window()

    view = View()
    absent = observe_and_audit_spot_trade_window(vault, view, policy, days=1, now=NOW)
    assert not absent.consistent_for_window and absent.reason == "VIEW_UID_UNVERIFIED"
    assert seen["window_calls"] == 0
    bind_uid_for_spot_submission(vault, ACCOUNT, "22334455", now=NOW)
    seen["uid_calls"] = 0
    seen["uid_after"] = "55443322"
    drift = observe_and_audit_spot_trade_window(vault, view, policy, days=1, now=NOW)
    assert not drift.consistent_for_window and drift.reason == "VIEW_UID_CHANGED"
    assert seen["window_calls"] == 1
    seen["uid_after"] = "22334455"
    clean = observe_and_audit_spot_trade_window(vault, view, policy, days=1, now=NOW)
    assert clean.consistent_for_window and clean.reason == "WINDOW_ARCHIVE_AND_BOOKING_MATCH"
    assert seen["window_calls"] == 2
    vault.lock()
