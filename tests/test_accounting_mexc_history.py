"""A later DEX acceptance must not reinterpret an earlier MEXC causal record."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from app.live_execution.journal import register_spot_policy
from app.live_execution.mexc_continuity_review import record_continuity_review, read_continuity_review
from app.live_execution.mexc_execution_state import fetch_spot_execution_inputs, MexcExecutionStateError
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.policy import LiveSpotPolicy
from app.portfolio.mexc_parity import assess_mexc_spot_parity
from app.portfolio.mexc_parity_archive import add_observation
from app.portfolio.models import RealMexcAccountLink
from app.portfolio.repository import (
    create_account, transfer_cash_between_accounts, account_snapshot, account_book_revision,
)
from tests.test_accounting_consumers import corrected_case
from tests.test_mexc_execution_state import Clock, Venue, ACCOUNT, UID


def test_old_mexc_causal_review_remains_replayable_after_connected_wallet_method_changes(tmp_path):
    saved = {}
    def before_accept(case, at):
        vault, _, wallet_account, _, _, _, _, _ = case
        clock = Clock()
        clock.value = max(clock.value, at) + timedelta(seconds=10)
        venue = Venue(clock)
        venue.quote = '20'
        with vault.session() as db:
            account = create_account(db, 'MEXC controlado', 'USDT').id
            db.add(RealMexcAccountLink(account_ref=ACCOUNT, account_id=account, created_at=clock()))
            db.commit()
        with vault.session() as db:
            transfer_cash_between_accounts(db, wallet_account, account, str(uuid4()), currency='USDT',
                amount=Decimal('20'), occurred_at=at + timedelta(seconds=2))
        bind_uid_for_spot_submission(vault, ACCOUNT, UID, now=clock())
        first, second = venue.balances(), venue.balances()
        anchor_id = str(uuid4())
        with vault.session() as db:
            revision = account_book_revision(db, account)
            report = assess_mexc_spot_parity(account_snapshot(db, account), first, second,
                book_revision_before=revision, book_revision_after=revision, now=clock())
            add_observation(db, anchor_id, account, ACCOUNT, report)
            db.commit()
        policy = LiveSpotPolicy(policy_id=str(uuid4()), strategy_version_id='manual-v1', account_ref=ACCOUNT,
            symbol='BTCUSDT', base_asset='BTC', quote_asset='USDT', max_order_quote=Decimal('15'),
            max_daily_turnover_quote=Decimal('50'), max_exposure_quote=Decimal('100'),
            max_daily_loss_quote=Decimal('5'), max_fee_bps=Decimal('20'), max_slippage_bps=Decimal('50'),
            max_data_age_seconds=60, expires_at=clock.value + timedelta(hours=1), enabled=True,
            reconciliation_anchor_id=anchor_id, allow_risk_reducing_exits=True)
        register_spot_policy(vault, policy, now=clock())
        review = record_continuity_review(vault, venue, policy.policy_id, review_id=str(uuid4()),
            assert_active=lambda: None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
        saved.update(vault=vault, account=account, revision=revision, review=review,
            venue=venue, clock=clock, policy=policy)
        return clock.value
    with corrected_case(tmp_path, before_accept=before_accept) as (case, _, _, _, _, _, _, accepted_at):
        vault = case[0]
        # These are two authentic, different methods over unchanged peer rows:
        # the earlier causal report used the raw ledger; the current view also
        # authenticates the connected wallet's accepted economic prefix.
        with vault.session() as db:
            assert account_book_revision(db, saved['account']) != saved['revision']
            boundary = datetime.fromisoformat(saved['review']['causal']['boundary_at'])
            historical_revision = account_book_revision(db, saved['account'], recorded_through=boundary)
            assert historical_revision == saved['review']['book_revision']
        assert read_continuity_review(vault, saved['review']['id']) == saved['review']
        saved['clock'].value = accepted_at + timedelta(seconds=1)
        with pytest.raises(MexcExecutionStateError, match='conciliación contable'):
            fetch_spot_execution_inputs(vault, saved['venue'], saved['policy'], clock=saved['clock'],
                quote_get=saved['venue'].quote_get, rules_fetch=saved['venue'].rules_fetch)
        # A method change requires a fresh live reconciliation; it cannot turn
        # historical evidence into new order authority.
        assert read_continuity_review(vault, saved['review']['id']) == saved['review']
        # A fresh observed opening explicitly adopts the new method; the
        # existing policy is not silently promoted to it.
        first, second = saved['venue'].balances(), saved['venue'].balances()
        anchor_id = str(uuid4())
        with vault.session() as db:
            revision = account_book_revision(db, saved['account'])
            report = assess_mexc_spot_parity(account_snapshot(db, saved['account']), first, second,
                book_revision_before=revision, book_revision_after=revision, now=saved['clock']())
            add_observation(db, anchor_id, saved['account'], ACCOUNT, report)
            db.commit()
        refreshed = replace(saved['policy'], policy_id=str(uuid4()), reconciliation_anchor_id=anchor_id,
            expires_at=saved['clock'].value + timedelta(hours=1))
        register_spot_policy(vault, refreshed, now=saved['clock']())
        inputs = fetch_spot_execution_inputs(vault, saved['venue'], refreshed, clock=saved['clock'],
            quote_get=saved['venue'].quote_get, rules_fetch=saved['venue'].rules_fetch)
        assert inputs.state.reconciled and inputs.state.free_quote == Decimal('20')
        assert read_continuity_review(vault, saved['review']['id']) == saved['review']
        vault.backup(tmp_path / 'old-causal-report-and-new-accounting.db')
