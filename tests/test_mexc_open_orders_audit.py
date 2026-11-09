"""Conciliación de órdenes abiertas contra bóveda aislada, sin red ni claves."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event
import time
from uuid import UUID, uuid4

import pytest

import app.live_execution.mexc_open_orders_audit as open_orders_audit
from app.live_execution.journal import (
    begin_spot_submission, register_spot_policy, reserve_spot_intent,
)
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.mexc_live_observation import MexcLiveEvidence, MexcObservedBalance
from app.live_execution.mexc_open_orders_audit import (
    audit_spot_open_orders, scan_unfiltered_spot_open_orders,
)
from app.live_execution.mexc_settlement import MexcSettlementError, _balance_evidence
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.portfolio.models import RealLiveOrderObservation
from app.portfolio.vault import RealVault


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ACCOUNT = "a" * 64


def setup(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("isolated audit password", create=True)
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=30, expires_at=NOW + timedelta(hours=1), enabled=True,
    )
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
    rules = MexcSpotRules(
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", observed_at=NOW,
        buy_allowed=True, sell_allowed=True, base_precision=6, quote_precision=2,
        base_step=Decimal("0.000001"), min_quote=Decimal("1"),
        max_quote=Decimal("10000"), fee_rate_upper=Decimal("0.001"),
        bid_multiplier_up=None, ask_multiplier_down=None,
    )
    evidence = MexcLiveEvidence(
        account_ref=ACCOUNT, view_uid="123456789", symbol="BTCUSDT",
        account_at=NOW, open_orders_at=NOW, quote_at=NOW, rules=rules,
        open_orders=(), balances=(MexcObservedBalance("USDT", Decimal("200"), Decimal(0)),),
        bid=Decimal("99999"), ask=Decimal("100000"),
        last=Decimal("99999.5"), free_base=Decimal(0), locked_base=Decimal(0),
        free_quote=Decimal("200"), locked_quote=Decimal(0),
    )
    register_spot_policy(vault, policy, now=NOW)
    return vault, policy, intent, state, evidence


def open_order(intent):
    return {"symbol": "BTCUSDT", "order_id": "external-1",
            "client_order_id": UUID(intent.intent_id).hex, "side": "BUY",
            "type": "LIMIT", "status": "NEW", "price": "100000",
            "quantity": "0.001", "executed_quantity": "0",
            "remaining_quantity": "0.001"}


def archive(vault, intent, status="NEW", executed=Decimal(0), observed_at=NOW):
    with vault.session() as db:
        db.add(RealLiveOrderObservation(
            id=str(uuid4()), intent_id=intent.intent_id, account_ref=ACCOUNT,
            symbol="BTCUSDT", external_order_id="external-1", status=status,
            executed_base=executed, cumulative_quote=Decimal(0),
            fee_json="[]", fill_count=0, payload_digest="b" * 64,
            observed_at=observed_at,
        ))
        db.commit()


@pytest.mark.parametrize("unfiltered", [False, True])
def test_open_order_audit_reads_one_snapshot_during_competing_archive_change(
        tmp_path, monkeypatch, unfiltered):
    vault, policy, intent, state, evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent)
    writer = RealVault(vault.path)
    writer.unlock("isolated audit password")
    started = Event()
    pending = []
    original_select = open_orders_audit.select
    calls = 0

    def change_archive():
        with writer.session() as db:
            db.query(RealLiveOrderObservation).one().status = "FILLED"
            started.set()
            db.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        def competing_select(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                pending.append(pool.submit(change_archive))
                assert started.wait(timeout=2)
                time.sleep(0.1)
                assert not pending[0].done()
            return original_select(*args, **kwargs)

        monkeypatch.setattr(open_orders_audit, "select", competing_select)
        own = open_order(intent)
        if unfiltered:
            before = scan(vault, unfiltered_snapshot(own))
            assert before.reason == "UNFILTERED_COVERAGE_UNPROVEN"
        else:
            before = audit_spot_open_orders(
                vault, policy, replace(evidence, open_orders=(own,)), now=NOW)
            assert before.clear_for_symbol
        pending[0].result(timeout=5)
        if unfiltered:
            after = scan(vault, unfiltered_snapshot(own))
        else:
            after = audit_spot_open_orders(
                vault, policy, replace(evidence, open_orders=(own,)), now=NOW)
    assert after.reason == "OPEN_ORDER_NOT_ARCHIVED"
    writer.lock()
    vault.lock()


def test_empty_symbol_is_clear_only_when_no_unknown_submission(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    clear = audit_spot_open_orders(vault, policy, evidence, now=NOW)
    assert clear.clear_for_symbol and clear.matched_open_orders == 0
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    unknown = audit_spot_open_orders(vault, policy, evidence, now=NOW)
    assert not unknown.clear_for_symbol
    assert unknown.reason == "SUBMISSION_OUTCOME_UNKNOWN"
    vault.lock()


def test_foreign_or_unarchived_open_order_blocks_and_archived_one_matches(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    foreign = replace(evidence, open_orders=({**open_order(intent),
                                              "client_order_id": "foreign-1"},))
    assert audit_spot_open_orders(vault, policy, foreign, now=NOW).reason == "EXTERNAL_OPEN_ORDER"
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    own = replace(evidence, open_orders=(open_order(intent),))
    assert audit_spot_open_orders(vault, policy, own, now=NOW).reason == "OPEN_ORDER_NOT_ARCHIVED"
    archive(vault, intent)
    matched = audit_spot_open_orders(vault, policy, own, now=NOW)
    assert matched.clear_for_symbol and matched.matched_open_orders == 1
    assert not audit_spot_open_orders(
        vault, policy, replace(own, open_orders_at=NOW - timedelta(seconds=31)),
        now=NOW,
    ).clear_for_symbol
    vault.lock()


def test_final_observation_without_closed_reservation_still_blocks(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent, status="CANCELED")
    result = audit_spot_open_orders(vault, policy, evidence, now=NOW)
    assert result.reason == "FINAL_RESERVATION_NOT_CLOSED"
    vault.lock()


def test_open_order_audit_requires_current_matching_archive(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent)
    own = replace(evidence, open_orders=(open_order(intent),))
    assert audit_spot_open_orders(vault, policy, own, now=NOW).clear_for_symbol

    later = replace(own, open_orders_at=NOW + timedelta(seconds=1))
    assert audit_spot_open_orders(vault, policy, later, now=NOW + timedelta(seconds=1)).reason == "OPEN_ORDER_ARCHIVE_STALE"
    partial = {**open_order(intent), "status": "PARTIALLY_FILLED",
               "executed_quantity": "0.0005", "remaining_quantity": "0.0005"}
    changed = replace(own, open_orders=(partial,))
    assert audit_spot_open_orders(vault, policy, changed, now=NOW).reason == "OPEN_ORDER_PROGRESS_MISMATCH"
    assert audit_spot_open_orders(vault, policy, replace(own, open_orders=(open_order(intent), open_order(intent))),
                                  now=NOW).reason == "OPEN_ORDER_SNAPSHOT_INVALID"
    malformed = {**open_order(intent), "executed_quantity": "NaN"}
    assert audit_spot_open_orders(vault, policy, replace(own, open_orders=(malformed,)),
                                  now=NOW).reason == "OPEN_ORDER_SNAPSHOT_INVALID"
    vault.lock()


def test_open_order_audits_reject_other_symbol_and_future_archive(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent, observed_at=NOW + timedelta(seconds=1))
    own = open_order(intent)
    symbol_audit = audit_spot_open_orders(
        vault, policy, replace(evidence, open_orders=({**own, "symbol": "ETHUSDT"},)), now=NOW,
    )
    assert not symbol_audit.clear_for_symbol
    assert symbol_audit.reason == "OPEN_ORDER_JOURNAL_MISMATCH"
    future = audit_spot_open_orders(vault, policy, replace(evidence, open_orders=(own,)), now=NOW)
    assert not future.clear_for_symbol and future.reason == "OPEN_ORDER_NOT_ARCHIVED"
    unfiltered = scan(vault, unfiltered_snapshot(own))
    assert not unfiltered.clear_for_account and unfiltered.reason == "OPEN_ORDER_NOT_ARCHIVED"
    vault.lock()


def test_symbol_audit_rejects_reciprocal_order_terms(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent)
    changed = {**open_order(intent), "price": "50000", "quantity": "0.002",
               "remaining_quantity": "0.002"}
    result = audit_spot_open_orders(
        vault, policy, replace(evidence, open_orders=(changed,)), now=NOW)
    assert result.reason == "OPEN_ORDER_TERMS_MISMATCH"
    assert not result.clear_for_symbol
    vault.lock()


def unfiltered_snapshot(*orders, observed_at=NOW):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY",
            "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "UNFILTERED_COVERAGE_UNPROVEN", "symbol": None,
            "observed_at": observed_at.isoformat(), "orders": list(orders)}


def scan(vault, snapshot):
    return scan_unfiltered_spot_open_orders(vault, ACCOUNT, snapshot,
                                            now=NOW, max_age_seconds=30)


def test_unfiltered_scan_detects_other_pair_but_never_authorizes_account(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    empty = scan(vault, unfiltered_snapshot())
    assert not empty.clear_for_account
    assert empty.reason == "UNFILTERED_COVERAGE_UNPROVEN"
    foreign = {**open_order(intent), "symbol": "ETHUSDT", "order_id": "eth-order",
               "client_order_id": "external-eth"}
    assert scan(vault, unfiltered_snapshot(foreign)).reason == "EXTERNAL_OPEN_ORDER_OTHER_PAIR"
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    assert scan(vault, unfiltered_snapshot()).reason == "SUBMISSION_OUTCOME_UNKNOWN"
    assert scan(vault, unfiltered_snapshot(open_order(intent))).reason == "OPEN_ORDER_NOT_ARCHIVED"
    archive(vault, intent)
    matched = scan(vault, unfiltered_snapshot(open_order(intent)))
    assert matched.reason == "UNFILTERED_COVERAGE_UNPROVEN"
    assert not matched.clear_for_account and matched.matched_open_orders == 1
    assert scan(vault, unfiltered_snapshot(open_order(intent), foreign)).reason == "EXTERNAL_OPEN_ORDER_OTHER_PAIR"
    vault.lock()


def test_unfiltered_scan_rejects_stale_mislabeled_and_duplicate_snapshot(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    stale = unfiltered_snapshot(observed_at=NOW - timedelta(seconds=31))
    assert scan(vault, stale).reason == "UNFILTERED_SNAPSHOT_STALE"
    mislabeled = {**unfiltered_snapshot(), "coverage": "ALL_OPEN_ORDERS_FOR_ONE_SYMBOL"}
    assert scan(vault, mislabeled).reason == "UNFILTERED_SNAPSHOT_INVALID"
    duplicate = unfiltered_snapshot(open_order(intent), open_order(intent))
    assert scan(vault, duplicate).reason == "UNFILTERED_SNAPSHOT_INVALID"
    vault.lock()


def test_unfiltered_scan_rejects_outdated_or_changed_archive(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent)
    changed = {**open_order(intent), "status": "PARTIALLY_FILLED",
               "executed_quantity": "0.0005", "remaining_quantity": "0.0005"}
    assert scan(vault, unfiltered_snapshot(changed)).reason == "OPEN_ORDER_PROGRESS_MISMATCH"
    later = unfiltered_snapshot(open_order(intent), observed_at=NOW + timedelta(seconds=1))
    assert scan_unfiltered_spot_open_orders(vault, ACCOUNT, later, now=NOW + timedelta(seconds=1),
                                            max_age_seconds=30).reason == "OPEN_ORDER_ARCHIVE_STALE"
    vault.lock()


def test_unfiltered_scan_rejects_reciprocal_order_terms(tmp_path):
    vault, _policy, intent, state, _evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent)
    changed = {**open_order(intent), "price": "50000", "quantity": "0.002",
               "remaining_quantity": "0.002"}
    result = scan(vault, unfiltered_snapshot(changed))
    assert result.reason == "OPEN_ORDER_TERMS_MISMATCH"
    assert not result.clear_for_account
    vault.lock()


def test_tiny_positive_remaining_does_not_round_into_fully_executed_order(tmp_path):
    vault, policy, intent, state, evidence = setup(tmp_path)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    archive(vault, intent, status="PARTIALLY_FILLED", executed=Decimal("0.001"))
    inconsistent = {**open_order(intent), "status": "PARTIALLY_FILLED",
                    "executed_quantity": "0.001", "remaining_quantity": "0.0000000000000000000000000000000000000001"}
    filtered = audit_spot_open_orders(vault, policy,
                                       replace(evidence, open_orders=(inconsistent,)), now=NOW)
    assert filtered.reason == "OPEN_ORDER_PROGRESS_MISMATCH"
    unfiltered = scan(vault, unfiltered_snapshot(inconsistent))
    assert unfiltered.reason == "OPEN_ORDER_PROGRESS_MISMATCH"
    assert unfiltered.matched_open_orders == 0
    vault.lock()


def test_settlement_rejects_balance_total_hiding_tiny_locked_amount():
    payload = {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
               "observed_at": NOW.isoformat(), "balances": [
                   {"asset": "BTC", "free": "1", "locked": "1e-40", "total": "1"},
               ]}
    with pytest.raises(MexcSettlementError, match="no cuadra"):
        _balance_evidence(payload, "BTC", "USDT", now=NOW)
