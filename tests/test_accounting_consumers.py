"""Existing portfolio, risk and valuation consumers use accepted economics."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from app.portfolio import economic_acceptance as archive
from app.portfolio.api import _event_payload
from app.portfolio.daily_book_risk import calculate_daily_book_risk
from app.portfolio.domain import AccountingContext, EventInput, PortfolioEvent, PortfolioError, fold_events
from app.portfolio.repository import (
    append_event, append_event_in_transaction, account_snapshot, account_book_revision, original_book_revision,
    list_events, list_original_events, import_events,
)
from app.portfolio.economic_projection import TieOrder, ProjectionError
from app.portfolio.valuation import ReferencePrice, value_snapshot
from tests.test_dex_call_booking import resolved_case, preview, book
from tests.test_economic_projection_source import saved_order
from app.portfolio.economic_projection_source import capture_booked_order


@contextmanager
def corrected_case(directory, *, before_accept=None):
    with resolved_case(directory, 'SELL') as case:
        vault, _, owner, meta, _, _, _, resolved = case
        at = datetime.fromisoformat(resolved['receipt']['block_at'])
        # A compatible old manual entry can differ from its receipt by two
        # minutes. Its original date is retained after linking and acceptance.
        with vault.session() as db:
            sale = append_event(db, owner, str(uuid4()), EventInput('SELL', 'USDT', at - timedelta(minutes=1),
                asset='WBNB', quantity=D('0.01'), unit_price=D('1'), note='Manual'))
        proposal = preview(case, trade_event_id=sale.id)
        book(case, proposal, trade_event_id=sale.id)
        saved = saved_order(case)
        with vault.session() as db:
            purchase = append_event(db, owner, str(uuid4()), EventInput('BUY', 'USDT', at,
                asset='WBNB', quantity=D('1'), unit_price=D('4')))
            before = account_snapshot(db, owner)
            original = list_original_events(db, owner)
            revision = account_book_revision(db, owner)
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
        boundary = before_accept(case, at) if before_accept is not None else at
        if before_accept is not None:
            with vault.session() as db:
                before = account_snapshot(db, owner)
                original = list_original_events(db, owner)
                revision = account_book_revision(db, owner)
        tie = TieOrder(owner, at, ('event:' + purchase.id, 'chain:' + saved['id']))
        proposal = archive.preview_acceptance(vault, owner, [witness], wallet_addresses=[meta['owner']], tie_orders=[tie])
        accepted_at = max(datetime.now(timezone.utc), boundary) + timedelta(minutes=1)
        accepted = archive.confirm_acceptance(vault, owner, [witness], wallet_addresses=[meta['owner']],
            preview_digest=proposal['preview_digest'], now=accepted_at, tie_orders=[tie])
        yield case, at, sale, before, original, revision, accepted, accepted_at


def test_snapshot_revision_risk_valuation_and_api_keep_original_facts_with_corrected_economics(tmp_path):
    with corrected_case(tmp_path) as (case, at, sale, before, original, raw_revision, accepted, accepted_at):
        vault, _, owner, _, _, _, _, _ = case
        with vault.session() as db:
            current = account_snapshot(db, owner)
            events = list_events(db, owner)
            assert fold_events(owner, events) == current
            assert list_original_events(db, owner) == original
            assert original_book_revision(db, owner) == raw_revision
            assert account_book_revision(db, owner) != raw_revision
            current_revision = account_book_revision(db, owner)
        assert accepted['accounting_method_active'] and not accepted['applied_to_ledger']
        assert current.positions['WBNB', 'USDT'].cost_basis == D('5.97')
        assert current.positions['WBNB', 'USDT'].realized_pnl == D('-0.02')
        assert before.positions['WBNB', 'USDT'].cost_basis == D('5.98')
        assert before.positions['WBNB', 'USDT'].realized_pnl == D('-0.01')
        assert current.cash == before.cash and current.contributions == before.contributions
        linked = next(event for event in events if event.id == sale.id)
        assert linked.input.occurred_at == at - timedelta(minutes=1)
        assert linked.occurred_at == at and linked.financial_input.occurred_at == at
        payload = _event_payload(linked)
        assert payload['occurred_at'] == sale.input.occurred_at.isoformat()
        assert payload['accounting']['occurred_at'] == at.isoformat()
        assert payload['accounting']['acceptance_ids'] == [accepted['id']]
        risk = calculate_daily_book_risk(owner, events, 'USDT', now=at + timedelta(seconds=1), base_asset='WBNB')
        assert risk.realized_pnl_quote == D('-0.02')
        assert risk.position_cost_basis_quote == D('5.97') and risk.average_cost_quote_per_base == D('3')
        assert not risk.reconciled_with_venue and not risk.execution_ready
        prices = {('WBNB', 'USDT'): ReferencePrice(D('10'), 'owned-fixture', at.isoformat(), False)}
        prior_value = value_snapshot(before, prices, now=at)
        current_value = value_snapshot(current, prices, now=at)
        prior_usdt = next(item for item in prior_value['currencies'] if item['currency'] == 'USDT')
        usdt = next(item for item in current_value['currencies'] if item['currency'] == 'USDT')
        assert usdt['equity'] == prior_usdt['equity'] == D('113.91')
        assert usdt['total_pnl'] == prior_usdt['total_pnl'] == D('13.91')
        assert usdt['unrealized_pnl'] == D('13.93') and prior_usdt['unrealized_pnl'] == D('13.92')
        with vault.session() as db:
            historical = list_events(db, owner, recorded_through=accepted_at - timedelta(microseconds=1))
            assert all(event.accounting is None for event in historical)
            assert fold_events(owner, historical) == before
            assert account_book_revision(db, owner, recorded_through=accepted_at - timedelta(microseconds=1)) == raw_revision
            assert account_book_revision(db, owner) == current_revision


def test_csv_batch_and_contribution_use_active_method_without_turning_capital_into_profit(tmp_path):
    with corrected_case(tmp_path) as (case, at, _, _, original, _, _, _):
        vault, _, owner, _, _, _, _, _ = case
        entries = [(str(uuid4()), EventInput('CONTRIBUTION', 'USDT', at + timedelta(seconds=1), amount=D('5'))),
            (str(uuid4()), EventInput('SELL', 'USDT', at + timedelta(seconds=2), asset='WBNB', quantity=D('0.99'), unit_price=D('10')))]
        with vault.session() as db:
            assert import_events(db, owner, entries, dry_run=True)['new'] == 2
            assert list_original_events(db, owner) == original
        with vault.session() as db:
            assert import_events(db, owner, entries, dry_run=False)['new'] == 2
        with vault.session() as db:
            after = account_snapshot(db, owner)
            assert import_events(db, owner, entries, dry_run=False)['already_present'] == 2
        assert after.cash['USDT'] == D('108.91') and after.contributions['USDT'] == D('105')
        assert after.positions['WBNB', 'USDT'].quantity == D('1')
        assert after.positions['WBNB', 'USDT'].cost_basis == D('3')
        assert after.positions['WBNB', 'USDT'].realized_pnl == D('6.91')
        invalid = [(str(uuid4()), EventInput('CONTRIBUTION', 'USDT', at + timedelta(seconds=3), amount=D('1'))),
            (str(uuid4()), EventInput('CONTRIBUTION', 'USDT', at - timedelta(seconds=1), amount=D('1')))]
        with pytest.raises(ProjectionError):
            with vault.session() as db:
                import_events(db, owner, invalid, dry_run=False)
        with vault.session() as db:
            assert account_snapshot(db, owner) == after


def test_daily_risk_uses_effective_utc_day_and_context_cannot_change_financial_facts():
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    prior = start - timedelta(minutes=1)
    account, acceptance = str(uuid4()), str(uuid4())
    contribution = PortfolioEvent(str(uuid4()), account, str(uuid4()), prior,
        EventInput('CONTRIBUTION', 'USDT', prior - timedelta(hours=1), amount=D('100')))
    purchase = PortfolioEvent(str(uuid4()), account, str(uuid4()), prior,
        EventInput('BUY', 'USDT', prior - timedelta(minutes=10), asset='WBNB', quantity=D('1'), unit_price=D('10')))
    sale = PortfolioEvent(str(uuid4()), account, str(uuid4()), prior,
        EventInput('SELL', 'USDT', prior, asset='WBNB', quantity=D('1'), unit_price=D('12')))
    effective = replace(sale.input, occurred_at=start)
    context = AccountingContext(sale, effective, 2, (acceptance,), True, str(uuid4()), False)
    corrected = replace(sale, accounting=context)
    legacy = calculate_daily_book_risk(account, [contribution, purchase, sale], 'USDT', now=start + timedelta(seconds=1))
    active = calculate_daily_book_risk(account, [contribution, purchase, corrected], 'USDT', now=start + timedelta(seconds=1))
    assert legacy.trade_count == 0 and legacy.realized_pnl_quote == 0
    assert active.trade_count == 1 and active.turnover_quote == D('12') and active.realized_pnl_quote == D('2')
    with pytest.raises(PortfolioError):
        replace(sale, accounting=replace(context, effective_input=replace(effective, quantity=D('2'))))
    with pytest.raises(PortfolioError):
        replace(corrected, id=str(uuid4()))


def test_public_read_preserves_callers_write_transaction_and_rollback(tmp_path):
    with corrected_case(tmp_path) as (case, at, _, _, original, _, _, _):
        with case[0].session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            append_event_in_transaction(db, case[2], str(uuid4()),
                EventInput('CONTRIBUTION', 'USDT', at + timedelta(seconds=1), amount=D('5')))
            assert account_snapshot(db, case[2]).cash['USDT'] == D('99.01')
            assert db.connection().connection.driver_connection.in_transaction
            db.rollback()
        with case[0].session() as db:
            assert account_snapshot(db, case[2]).cash['USDT'] == D('94.01')
            assert list_original_events(db, case[2]) == original


def test_accepted_fold_is_independent_of_callers_decimal_rounding():
    from decimal import Context, localcontext, ROUND_FLOOR, ROUND_HALF_EVEN
    from app.portfolio.domain import CALC_PRECISION
    at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    account, acceptance = str(uuid4()), str(uuid4())
    values = [EventInput('CONTRIBUTION', 'USDT', at, amount=D('10')),
        EventInput('BUY', 'USDT', at, asset='WBNB', quantity=D('1'), unit_price=D('1')),
        EventInput('BUY', 'USDT', at, asset='WBNB', quantity=D('2'), unit_price=D('0.5')),
        EventInput('SELL', 'USDT', at, asset='WBNB', quantity=D('1'), unit_price=D('1'))]
    events = []
    for index, item in enumerate(values):
        original = PortfolioEvent(str(uuid4()), account, str(uuid4()), at, item)
        events.append(replace(original, accounting=AccountingContext(original, item, index,
            (acceptance,), True, None, False)))
    baseline = fold_events(account, events)
    with localcontext(Context(prec=6, rounding=ROUND_FLOOR)):
        lowered = fold_events(account, events)
    assert lowered == baseline
    context = Context(prec=CALC_PRECISION, rounding=ROUND_HALF_EVEN)
    expected_profit = context.subtract(D('1'), context.divide(D('2'), D('3')))
    assert baseline.positions['WBNB', 'USDT'].realized_pnl == expected_profit
