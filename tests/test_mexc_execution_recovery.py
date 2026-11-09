"""Reservas no enviadas y reapertura con barreras frente a reenvío y carreras."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from app.live_execution.journal import (
    LiveJournalError, begin_spot_submission, register_spot_policy,
    release_unsent_spot_intent, reserve_spot_intent, stop_spot_account,
)
from app.live_execution.mexc_execution import MexcExecutionError
from app.live_execution.mexc_execution_recovery import resume_spot_execution
from app.live_execution.mexc_execution_state import MexcExecutionStateError
from app.portfolio.mexc_parity import assess_mexc_spot_parity
from app.portfolio.mexc_parity_archive import add_observation
from app.portfolio.models import RealLiveSpotIntent, RealLiveSpotPolicy, RealLiveStop
from app.portfolio.repository import account_book_revision, account_snapshot
from app.portfolio.vault import RealVault
from tests.test_mexc_execution import ExchangeOrders, send
from tests.test_mexc_execution_state import ACCOUNT, PASSWORD, setup, inputs, buy


def opening(vault, account, venue, clock):
    first, second = venue.balances(), venue.balances()
    identifier = str(uuid4())
    with vault.session() as db:
        revision = account_book_revision(db, account)
        report = assess_mexc_spot_parity(
            account_snapshot(db, account), first, second, now=clock(),
            book_revision_before=revision, book_revision_after=revision)
        add_observation(db, identifier, account, ACCOUNT, report)
        db.commit()
    return identifier


def draft(vault, policy, clock):
    item = replace(policy, policy_id=str(uuid4()), enabled=False,
                   reconciliation_anchor_id=None, allow_risk_reducing_exits=False,
                   resumed_after_stop_at=None)
    register_spot_policy(vault, item, now=clock())
    return item


def resume(vault, policy, anchor_id, venue, clock, trade, *, request_id=None):
    return resume_spot_execution(
        vault, venue, trade, policy.policy_id, anchor_id, request_id or str(uuid4()),
        clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)


def test_release_unsent_reservation_retains_id_and_allows_a_different_order(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    state = inputs(vault, policy, venue, clock).state
    intent = buy(policy, clock)
    reserve_spot_intent(vault, intent, state, now=clock())
    venue.calls.clear()
    released = release_unsent_spot_intent(vault, intent.intent_id, now=clock())
    assert released["status"] == "RELEASED_UNSENT" and not released["order_sent"]
    assert release_unsent_spot_intent(vault, intent.intent_id, now=clock())["status"] == "ALREADY_RELEASED"
    assert venue.calls == []
    with vault.session() as db:
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        assert row.status == "STOPPED" and row.reason == "ABANDONED_BEFORE_SUBMISSION"
        assert row.reserved_base == 0 and row.reserved_quote == 0
        assert row.request_json is not None and db.get(RealLiveStop, ACCOUNT) is None
    trader = ExchangeOrders(venue, clock)
    assert send(vault, policy, venue, clock, trader, intent.intent_id)["replayed_without_post"]
    assert trader.posts == []
    assert send(vault, policy, venue, clock, trader, str(uuid4()))["journal_status"] == "RESERVATION_CLOSED"
    assert len(trader.posts) == 1
    vault.lock()


def test_cannot_release_a_submission_even_if_the_exchange_has_not_been_observed(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    state = inputs(vault, policy, venue, clock).state
    intent = buy(policy, clock)
    reserve_spot_intent(vault, intent, state, now=clock())
    begin_spot_submission(vault, intent, state, now=clock())
    with pytest.raises(LiveJournalError, match="nunca inició"):
        release_unsent_spot_intent(vault, intent.intent_id, now=clock())
    with vault.session() as db:
        assert db.get(RealLiveSpotIntent, intent.intent_id).reserved_quote > 0
    vault.lock()


def test_release_and_submission_gate_serialize_between_independent_vault_instances(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    state = inputs(vault, policy, venue, clock).state
    intent = buy(policy, clock)
    at = clock()
    reserve_spot_intent(vault, intent, state, now=at)
    other = RealVault(tmp_path / "real.db")
    other.unlock(PASSWORD)
    barrier = Barrier(2)

    def run(action):
        barrier.wait()
        try:
            action()
            return True
        except LiveJournalError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        released = pool.submit(run, lambda: release_unsent_spot_intent(vault, intent.intent_id, now=at))
        submitted = pool.submit(run, lambda: begin_spot_submission(other, intent, state, now=at))
        assert released.result() != submitted.result()
    with vault.session() as db:
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        assert (row.status == "STOPPED" and row.reserved_quote == 0) or (
            row.status == "SUBMISSION_UNKNOWN" and row.reserved_quote > 0)
    other.lock()
    vault.lock()


def test_reopen_creates_new_policy_and_does_not_revive_old_policy_at_lower_send_gates(tmp_path):
    vault, previous, account, venue, clock = setup(tmp_path)
    limits = draft(vault, previous, clock)
    stop_spot_account(vault, ACCOUNT, now=clock())
    anchor_id = opening(vault, account, venue, clock)
    trader = ExchangeOrders(venue, clock)
    policy = resume(vault, limits, anchor_id, venue, clock, trader)
    assert policy.resumed_after_stop_at is not None and trader.posts == []
    fresh = inputs(vault, policy, venue, clock)
    assert not fresh.state.stopped
    with pytest.raises(MexcExecutionStateError, match="anterior"):
        inputs(vault, previous, venue, clock)
    old_order = buy(previous, clock)
    assert reserve_spot_intent(vault, old_order, fresh.state, now=clock()).reason == "POLICY_REACTIVATION_REQUIRED"
    assert send(vault, policy, venue, clock, trader, str(uuid4()))["journal_status"] == "RESERVATION_CLOSED"
    assert len(trader.posts) == 1
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        from app.live_execution.journal import _stored_policy
        assert _stored_policy(db, policy.policy_id) == policy
        assert db.get(RealLiveStop, ACCOUNT).stopped is False
    vault.lock()


def test_reopen_rejects_an_old_opening_and_a_pending_uncertain_order_before_network(tmp_path):
    vault, previous, account, venue, clock = setup(tmp_path)
    limits = draft(vault, previous, clock)
    state = inputs(vault, previous, venue, clock).state
    intent = buy(previous, clock)
    reserve_spot_intent(vault, intent, state, now=clock())
    begin_spot_submission(vault, intent, state, now=clock())
    stop_spot_account(vault, ACCOUNT, now=clock())
    trader = ExchangeOrders(venue, clock)
    venue.calls.clear()
    with pytest.raises(MexcExecutionError, match="apertura nueva"):
        resume(vault, limits, previous.reconciliation_anchor_id, venue, clock, trader)
    assert venue.calls == []
    anchor_id = opening(vault, account, venue, clock)
    venue.calls.clear()
    with pytest.raises(MexcExecutionError, match="órdenes pendientes"):
        resume(vault, limits, anchor_id, venue, clock, trader)
    assert venue.calls == [] and trader.posts == []
    with vault.session() as db:
        assert db.get(RealLiveStop, ACCOUNT).stopped is True
    vault.lock()


def test_review_of_stopped_account_remains_unusable_for_submission(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    stop_spot_account(vault, ACCOUNT, now=clock())
    anchor_id = opening(vault, account, venue, clock)
    review_policy = replace(policy, policy_id=str(uuid4()), reconciliation_anchor_id=anchor_id)
    result = inputs(vault, review_policy, venue, clock, review_stopped=True)
    assert result.state.stopped is True
    from app.live_execution.policy import evaluate_spot_intent
    assert not evaluate_spot_intent(review_policy, buy(review_policy, clock), result.state, now=clock()).allowed
    vault.lock()


def test_balance_change_during_reopening_keeps_stop_and_does_not_create_policy(tmp_path):
    vault, previous, account, venue, clock = setup(tmp_path)
    limits = draft(vault, previous, clock)
    stop_spot_account(vault, ACCOUNT, now=clock())
    anchor_id = opening(vault, account, venue, clock)
    venue.quote = "701"
    identifier = str(uuid4())
    with pytest.raises(MexcExecutionStateError):
        resume(vault, limits, anchor_id, venue, clock, ExchangeOrders(venue, clock), request_id=identifier)
    with vault.session() as db:
        assert db.get(RealLiveStop, ACCOUNT).stopped and db.get(RealLiveSpotPolicy, identifier) is None
    vault.lock()


def test_another_stop_during_trade_verification_wins_over_reopening(tmp_path):
    vault, previous, account, venue, clock = setup(tmp_path)
    limits = draft(vault, previous, clock)
    stop_spot_account(vault, ACCOUNT, now=clock())
    anchor_id = opening(vault, account, venue, clock)
    trader = ExchangeOrders(venue, clock)
    trader.pair_view_account = lambda _: stop_spot_account(vault, ACCOUNT, now=clock())
    with pytest.raises(MexcExecutionError, match="cambiaron"):
        resume(vault, limits, anchor_id, venue, clock, trader)
    with vault.session() as db:
        assert db.get(RealLiveStop, ACCOUNT).stopped is True
    vault.lock()


def test_policy_and_stop_change_roll_back_together_if_registration_fails(tmp_path, monkeypatch):
    vault, previous, account, venue, clock = setup(tmp_path)
    limits = draft(vault, previous, clock)
    stop_spot_account(vault, ACCOUNT, now=clock())
    anchor_id = opening(vault, account, venue, clock)
    from app.live_execution import mexc_execution_recovery as recovery
    original = recovery._register_spot_policy_in_transaction

    def fail(db, policy, **kwargs):
        original(db, policy, **kwargs)
        raise RuntimeError("isolated transaction fault")

    monkeypatch.setattr(recovery, "_register_spot_policy_in_transaction", fail)
    identifier = str(uuid4())
    with pytest.raises(RuntimeError, match="transaction fault"):
        resume(vault, limits, anchor_id, venue, clock, ExchangeOrders(venue, clock), request_id=identifier)
    with vault.session() as db:
        assert db.get(RealLiveStop, ACCOUNT).stopped and db.get(RealLiveSpotPolicy, identifier) is None
    vault.lock()


def test_reopen_request_replays_without_network_and_cannot_override_a_later_stop(tmp_path):
    vault, previous, account, venue, clock = setup(tmp_path)
    limits = draft(vault, previous, clock)
    stop_spot_account(vault, ACCOUNT, now=clock())
    anchor_id = opening(vault, account, venue, clock)
    trader = ExchangeOrders(venue, clock)
    identifier = str(uuid4())
    policy = resume(vault, limits, anchor_id, venue, clock, trader, request_id=identifier)
    venue.calls.clear()
    assert resume(vault, limits, anchor_id, venue, clock, trader, request_id=identifier) == policy
    assert venue.calls == [] and trader.posts == []
    stop_spot_account(vault, ACCOUNT, now=clock())
    with pytest.raises(MexcExecutionError, match="parada posterior"):
        resume(vault, limits, anchor_id, venue, clock, trader, request_id=identifier)
    with vault.session() as db:
        assert db.get(RealLiveStop, ACCOUNT).stopped is True
    vault.lock()
