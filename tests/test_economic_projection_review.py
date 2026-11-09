"""Whole-book review, dependent transfer costs and complete DEX coverage."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal as D, ROUND_FLOOR, localcontext
from uuid import uuid4
from threading import Event

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy.orm import Session

from app.portfolio import dex_receipt_order as orders, dex_receipt_booking as booking
from app.portfolio import wallet_swap_booking
from app.portfolio.domain import EventInput
from app.portfolio.economic_projection import ProjectionError, ProjectionLimits, TieOrder
from app.portfolio.economic_projection_review import capture_projection_inputs, review_projection as replay
from app.portfolio.economic_projection_source import capture_booked_order
from app.portfolio.models import RealEvent
from app.portfolio.repository import (
    append_event, create_account, list_events, account_snapshot,
    transfer_asset_between_accounts, transfer_cash_between_accounts,
)
from tests.dex_order_fixture import same_block_case
from tests.test_dex_call_booking import resolved_case, preview, book
from tests.test_economic_projection_source import saved_order
from tests.test_wallet_swap import PASSWORD


def review_projection(db, root, witnesses, *, tie_orders=(), **kwargs):
    # Compact helper for assertions about one SQL snapshot. The dedicated test
    # below proves the application port can replay after releasing that session.
    return replay(capture_projection_inputs(db, root, witnesses, **kwargs), tie_orders=tie_orders)


def test_review_recalculates_transitive_transfer_cost_and_sale_without_rewriting_ledger(tmp_path):
    with resolved_case(tmp_path, 'SELL') as case:
        vault, _, owner, meta, _, _, _, resolved = case
        book(case, preview(case))
        saved = saved_order(case)
        at = datetime.fromisoformat(resolved['receipt']['block_at'])
        with vault.session() as db:
            manual = append_event(db, owner, str(uuid4()), EventInput('BUY', 'USDT', at,
                asset='WBNB', quantity=D('1'), unit_price=D('4')))
        with vault.session() as db:
            peer = create_account(db, 'Segunda', 'USDT').id
            third = create_account(db, 'Tercera', 'USDT').id
        with vault.session() as db:
            transfer_asset_between_accounts(db, owner, peer, str(uuid4()), asset='WBNB',
                quote_currency='USDT', quantity=D('1.99'), occurred_at=at + timedelta(seconds=1), fee=D('0.99'))
        with vault.session() as db:
            transfer_asset_between_accounts(db, peer, third, str(uuid4()), asset='WBNB',
                quote_currency='USDT', quantity=D('0.5'), occurred_at=at + timedelta(seconds=2))
        with vault.session() as db:
            append_event(db, third, str(uuid4()), EventInput('SELL', 'USDT', at + timedelta(seconds=3),
                asset='WBNB', quantity=D('0.25'), unit_price=D('10')))
        with vault.session() as db:
            originals = {account: list_events(db, account) for account in (owner, peer, third)}
            snapshots = {account: account_snapshot(db, account) for account in originals}
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            tie = TieOrder(owner, at, ('event:' + manual.id, 'chain:' + saved['id']))
            default = review_projection(db, owner, [witness])
            assert all(not item['changed'] for item in default.body['comparisons'])
            reviewed = review_projection(db, owner, [witness], tie_orders=[tie])
            assert reviewed.digest != default.digest
            assert set(reviewed.body['account_revisions']) == {owner, peer, third}
            assert reviewed.body['adjusted_transfer_legs'] == 4
            assert reviewed.body['events_count'] == sum(len(items) for items in originals.values())
            assert all(item['changed'] for item in reviewed.body['comparisons'])
            assert not reviewed.body['applied_to_ledger'] and not reviewed.body['trading_authority']
            projected = reviewed.projection.snapshots
            assert projected[owner].positions['WBNB', 'USDT'].realized_pnl == D('-0.02')
            assert snapshots[owner].positions['WBNB', 'USDT'].realized_pnl == D('-0.01')
            assert projected[peer].positions['WBNB', 'USDT'].cost_basis == D('2.985')
            assert snapshots[peer].positions['WBNB', 'USDT'].cost_basis == D('2.99')
            assert projected[third].positions['WBNB', 'USDT'].cost_basis == D('1.4925')
            assert projected[third].positions['WBNB', 'USDT'].realized_pnl == D('1.0075')
            for account in originals:
                assert projected[account].cash == snapshots[account].cash
                assert projected[account].contributions == snapshots[account].contributions
                assert list_events(db, account) == originals[account]
                assert account_snapshot(db, account) == snapshots[account]
            with localcontext() as ambient:
                ambient.prec = 6
                ambient.rounding = ROUND_FLOOR
                again = review_projection(db, owner, [witness], tie_orders=[tie])
            assert again.digest == reviewed.digest and again.body == reviewed.body
            downstream = review_projection(db, third, [witness], tie_orders=[tie])
            assert downstream.projection == reviewed.projection
            assert set(downstream.body['account_revisions']) == {owner, peer, third}
            # Caller mutation cannot alter the detached reviewed body.
            copy = deepcopy(reviewed.body)
            witness['account_id'] = third
            assert reviewed.body == copy


def test_cash_closure_changes_revision_on_new_peer_event_and_rejects_limits_or_missing_side(tmp_path):
    with resolved_case(tmp_path) as case:
        vault, _, owner, meta, _, _, _, resolved = case
        book(case, preview(case))
        saved = saved_order(case)
        at = datetime.fromisoformat(resolved['receipt']['block_at'])
        with vault.session() as db:
            peer = create_account(db, 'Destino', 'USDT').id
        with vault.session() as db:
            movement = transfer_cash_between_accounts(db, owner, peer, str(uuid4()), currency='USDT',
                amount=D('5'), fee=D('0.1'), occurred_at=at + timedelta(seconds=1))
        with vault.session() as db:
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            first = review_projection(db, owner, [witness])
            assert first.projection.snapshots[owner].cash['USDT'] == D('92.89')
            assert first.projection.snapshots[peer].cash['USDT'] == D('5')
            assert first.projection.snapshots[peer].contributions == {}
            for limits in (ProjectionLimits(max_accounts=1), ProjectionLimits(max_events=2)):
                with pytest.raises(ProjectionError):
                    review_projection(db, owner, [witness], limits=limits)
        with vault.session() as db:
            append_event(db, peer, str(uuid4()), EventInput('CONTRIBUTION', 'USDT',
                at + timedelta(seconds=2), amount=D('1')))
        with vault.session() as db:
            changed = review_projection(db, owner, [witness])
            assert first.digest != changed.digest
            assert first.body['account_revisions'][owner] == changed.body['account_revisions'][owner]
            assert first.body['account_revisions'][peer] != changed.body['account_revisions'][peer]
            db.delete(db.get(RealEvent, movement['target_event_id']))
            db.commit()
        with vault.session() as db:
            with pytest.raises(ProjectionError, match='lado'):
                review_projection(db, owner, [witness])


def test_review_requires_every_known_dex_trade_and_rejects_duplicate_or_foreign_witness(tmp_path, monkeypatch):
    with same_block_case(tmp_path, monkeypatch) as case:
        witnesses = []
        for original in case.sources:
            arguments = dict(context_account_id=case.account, wallet_address=case.meta['owner'],
                evidence=case.observations[original.key], now=case.at)
            with case.vault.session() as db:
                proposal = orders.preview_order(db, original.kind, original.id, **arguments)
            with case.vault.session() as db:
                saved = orders.confirm_order(db, original.kind, original.id,
                    preview_digest=proposal['preview_digest'], **arguments)
            module = wallet_swap_booking if original.kind == 'WALLET_SWAP' else booking
            positional = (original.id,) if original.kind == 'WALLET_SWAP' else (original.kind, original.id)
            with case.vault.session() as db:
                proposal = module.preview_booking(db, *positional,
                    context_account_id=case.account, wallet_address=case.meta['owner'])
            writer = module.book_swap if original.kind == 'WALLET_SWAP' else module.book_receipt
            with case.vault.session() as db:
                writer(db, *positional, context_account_id=case.account, wallet_address=case.meta['owner'],
                    expected_revision=proposal['revision'], preview_digest=proposal['preview_digest'], now=case.at)
            with case.vault.session() as db:
                witnesses.append(capture_booked_order(db, saved['id'], wallet_address=case.meta['owner']))
        with case.vault.session() as db:
            for incomplete in ([witnesses[0]], [witnesses[0], witnesses[0]], []):
                with pytest.raises(ProjectionError):
                    review_projection(db, case.ledger_account, incomplete)
            with pytest.raises(ProjectionError):
                review_projection(db, case.account, witnesses)
            complete = review_projection(db, case.ledger_account, witnesses)
            reversed_input = review_projection(db, case.ledger_account, list(reversed(witnesses)))
            assert complete.body == reversed_input.body and complete.digest == reversed_input.digest
            assert complete.body['claimed_transactions'] == 2


def test_pure_review_runs_after_session_and_vault_are_closed_and_detects_a_newer_capture(tmp_path, monkeypatch):
    with resolved_case(tmp_path) as case:
        vault, _, owner, meta, _, _, _, resolved = case
        book(case, preview(case))
        saved = saved_order(case)
        with vault.session() as db:
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            inputs = capture_projection_inputs(db, owner, [witness])
        vault.lock()
        from app.portfolio import economic_projection_review as module
        def forbidden(*args, **kwargs):
            raise AssertionError('pure review accessed SQL')
        with monkeypatch.context() as guard:
            guard.setattr(module, 'account_book_revision', forbidden)
            guard.setattr(module, 'claim_from_witness', forbidden)
            guard.setattr(module, 'original_account_closure', forbidden)
            reviewed = replay(inputs)
        assert reviewed.projection.snapshots[owner].cash['USDT'] == D('97.99')
        vault.unlock(PASSWORD)
        with vault.session() as db:
            append_event(db, owner, str(uuid4()), EventInput('CONTRIBUTION', 'USDT',
                datetime.fromisoformat(resolved['receipt']['block_at']) + timedelta(seconds=1), amount=D('1')))
        with vault.session() as db:
            fresh = capture_projection_inputs(db, owner, [witness])
        vault.lock()
        assert replay(inputs) == reviewed
        assert replay(fresh).digest != reviewed.digest
        assert replay(fresh).projection.snapshots[owner].cash['USDT'] == D('98.99')


def test_capture_pins_one_real_sqlite_snapshot_until_session_release(tmp_path, monkeypatch):
    with resolved_case(tmp_path) as case:
        vault, _, owner, meta, _, _, _, resolved = case
        book(case, preview(case))
        saved = saved_order(case)
        with vault.session() as db:
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
        from app.portfolio import economic_projection_review as module
        original = module.original_account_closure
        attempting, committed = Event(), Event()
        futures = []
        def writer():
            with Session(vault._engine) as independent:
                sql_event.listen(independent, 'before_commit', lambda db: attempting.set())
                append_event(independent, owner, str(uuid4()), EventInput('CONTRIBUTION', 'USDT',
                    datetime.fromisoformat(resolved['receipt']['block_at']) + timedelta(seconds=1), amount=D('1')))
                committed.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            def after_event_read(db, *args, **kwargs):
                result = original(db, *args, **kwargs)
                futures.append(pool.submit(writer))
                assert attempting.wait(5), 'independent writer never attempted commit'
                assert not committed.wait(0.05), 'writer committed through the pinned read snapshot'
                return result
            with monkeypatch.context() as guard:
                guard.setattr(module, 'original_account_closure', after_event_read)
                with vault.session() as db:
                    pinned = capture_projection_inputs(db, owner, [witness])
                    assert not committed.is_set()
            futures[0].result(timeout=10)
        assert committed.is_set()
        assert replay(pinned).projection.snapshots[owner].cash['USDT'] == D('97.99')
        with vault.session() as db:
            current = capture_projection_inputs(db, owner, [witness])
        assert replay(current).projection.snapshots[owner].cash['USDT'] == D('98.99')
        assert pinned.account_revisions != current.account_revisions


def test_capture_refuses_pending_orm_edits_instead_of_flushing_them(tmp_path):
    with resolved_case(tmp_path) as case:
        vault, _, owner, meta, _, _, _, _ = case
        book(case, preview(case))
        saved = saved_order(case)
        with vault.session() as db:
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            original = list_events(db, owner)[0]
            row = db.get(RealEvent, original.id)
            row.note = 'Unsaved edit'
            with pytest.raises(ProjectionError, match='pendientes'):
                capture_projection_inputs(db, owner, [witness])
            assert row in db.dirty
            db.rollback()
        with vault.session() as db:
            assert db.get(RealEvent, original.id).note == original.input.note
