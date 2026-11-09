"""Accepted prefixes with later trades, transfers and exact knowledge cutoffs."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from uuid import uuid4

import pytest

from app.portfolio.accounting_view import capture_accounting_inputs, replay_accounting_view
from app.portfolio import economic_acceptance as archive, wallet_swap_booking, dex_receipt_booking
from app.portfolio.economic_projection import ProjectionError, TieOrder
from app.portfolio.economic_projection_source import capture_booked_order
from app.portfolio.domain import EventInput
from app.portfolio.models import RealEvent
from app.portfolio.repository import append_event, list_events, list_original_events, create_account, transfer_asset_between_accounts, _digest
from tests.test_economic_acceptance import ready, proposed, confirm
from tests.dex_order_fixture import finalized_wallet_order, same_block_case
from tests.test_dex_receipt_order_journal import arguments, review, save
from tests.test_wallet_swap import PASSWORD


def capture(vault, account, **kwargs):
    with vault.session() as db:
        return capture_accounting_inputs(db, account, **kwargs)


def test_manual_sales_and_fee_transfer_propagate_accepted_cost_without_changing_originals(tmp_path):
    with ready(tmp_path, 'SELL') as (case, witness):
        vault, _, owner, meta, _, _, _, resolved = case
        at = datetime.fromisoformat(resolved['receipt']['block_at'])
        with vault.session() as db:
            manual = append_event(db, owner, str(uuid4()), EventInput('BUY', 'USDT', at,
                asset='WBNB', quantity=D('1'), unit_price=D('4')))
        tie = TieOrder(owner, at, ('event:' + manual.id, 'chain:' + witness['order_record_id']))
        proposal = archive.preview_acceptance(vault, owner, [witness], wallet_addresses=[meta['owner']], tie_orders=[tie])
        archive.confirm_acceptance(vault, owner, [witness], wallet_addresses=[meta['owner']], tie_orders=[tie],
            preview_digest=proposal['preview_digest'], now=datetime.now(timezone.utc) + timedelta(minutes=1))
        baseline = capture(vault, owner)
        initial = replay_accounting_view(baseline)
        position = initial.snapshots[owner].positions['WBNB', 'USDT']
        assert (position.quantity, position.cost_basis, position.realized_pnl) == (D('1.99'), D('5.97'), D('-0.02'))
        with vault.session() as db:
            append_event(db, owner, str(uuid4()), EventInput('SELL', 'USDT', at + timedelta(seconds=1),
                asset='WBNB', quantity=D('0.99'), unit_price=D('10')))
        with vault.session() as db:
            peer = create_account(db, 'Cuenta receptora', 'USDT').id
        with vault.session() as db:
            transfer_asset_between_accounts(db, owner, peer, str(uuid4()), asset='WBNB', quote_currency='USDT',
                quantity=D('1'), fee=D('0.1'), occurred_at=at + timedelta(seconds=2))
        with vault.session() as db:
            append_event(db, peer, str(uuid4()), EventInput('SELL', 'USDT', at + timedelta(seconds=3),
                asset='WBNB', quantity=D('0.3'), unit_price=D('10')))
            originals = {account: list_events(db, account) for account in (owner, peer)}
        current = capture(vault, owner)
        downstream = capture(vault, peer)
        projected = replay_accounting_view(current)
        assert current.revision != baseline.revision
        assert current.revision != downstream.revision
        assert replay_accounting_view(downstream).snapshots == projected.snapshots
        source = projected.snapshots[owner]
        destination = projected.snapshots[peer]
        assert source.cash['USDT'] == D('103.91')
        assert source.positions['WBNB', 'USDT'].quantity == 0
        assert source.positions['WBNB', 'USDT'].realized_pnl == D('6.91')
        assert destination.cash['USDT'] == D('3') and destination.contributions == {}
        assert destination.positions['WBNB', 'USDT'].quantity == D('0.6')
        assert destination.positions['WBNB', 'USDT'].cost_basis == D('2')
        assert destination.positions['WBNB', 'USDT'].realized_pnl == D('2')
        old_inputs = {event.original.id: event.accounting_input for event in initial.events}
        for event in projected.events:
            if event.original.id in old_inputs:
                assert event.accounting_input == old_inputs[event.original.id]
        with vault.session() as db:
            assert {account: list_events(db, account) for account in originals} == originals
        vault.lock()
        assert replay_accounting_view(current) == projected
        vault.unlock(PASSWORD)


@pytest.mark.parametrize('proof_first', [False, True])
def test_new_mined_wallet_trade_extends_known_owner_only_with_its_order_proof(tmp_path, proof_first):
    with ready(tmp_path) as (case, witness):
        vault, context, owner, meta, _, _, _, _ = case
        accepted = confirm(case, witness, proposed(case, witness))
        previous = capture(vault, owner)
        original, observed = finalized_wallet_order(case)
        scope = arguments(case, original, observed)
        with vault.session() as db:
            before_booking = list_original_events(db, owner)
        if proof_first:
            save(case, original, scope, review(case, original, scope))
        else:
            with pytest.raises(ProjectionError, match='orden del nuevo recibo'):
                with vault.session() as db:
                    wallet_swap_booking.preview_booking(db, original.id,
                        context_account_id=context, wallet_address=meta['owner'])
            with vault.session() as db:
                assert list_original_events(db, owner) == before_booking
            save(case, original, scope, review(case, original, scope))
        with vault.session() as db:
            proposal = wallet_swap_booking.preview_booking(db, original.id,
                context_account_id=context, wallet_address=meta['owner'])
        with vault.session() as db:
            wallet_swap_booking.book_swap(db, original.id, context_account_id=context,
                wallet_address=meta['owner'], expected_revision=proposal['revision'],
                preview_digest=proposal['preview_digest'], now=scope['now'])
            original_rows = list_original_events(db, owner)
        current = capture(vault, owner)
        assert len(current.extension_claims) == 1
        assert current.extension_claims[0].source_id == original.id
        assert current.revision != previous.revision
        projected = replay_accounting_view(current)
        assert projected.snapshots[owner].cash['USDT'] == D('97.98')
        position = projected.snapshots[owner].positions['WBNB', 'USDT']
        assert (position.quantity, position.cost_basis) == (D('1.02'), D('2.02'))
        with vault.session() as db:
            assert list_original_events(db, owner) == original_rows
            historical = archive.capture_acceptance(db, accepted['id'])
        assert archive.replay_acceptance(historical).projection.snapshots[owner].cash['USDT'] == D('97.99')


def test_recorded_cutoff_excludes_later_transfer_and_uses_the_method_known_then(tmp_path):
    with ready(tmp_path) as (case, witness):
        saved = confirm(case, witness, proposed(case, witness))
        at = datetime.fromisoformat(saved['recorded_at'])
        before = capture(case[0], case[2], recorded_through=at - timedelta(microseconds=1))
        assert before is None
        current = capture(case[0], case[2], recorded_through=at)
        with case[0].session() as db:
            added = append_event(db, case[2], str(uuid4()), EventInput('CONTRIBUTION', 'USDT',
                at + timedelta(seconds=1), amount=D('5')))
            db.get(RealEvent, added.id).recorded_at = at + timedelta(seconds=1)
            db.commit()
        unchanged = capture(case[0], case[2], recorded_through=at)
        after = capture(case[0], case[2])
        assert unchanged.revision == current.revision
        assert replay_accounting_view(unchanged) == replay_accounting_view(current)
        assert after.revision != current.revision
        assert replay_accounting_view(after).snapshots[case[2]].cash['USDT'] == D('102.99')


def test_retroactive_manual_row_requires_review_and_never_reverts_to_legacy_fold(tmp_path):
    with ready(tmp_path) as (case, witness):
        confirm(case, witness, proposed(case, witness))
        item = EventInput('CONTRIBUTION', 'USDT',
            datetime.fromisoformat(case[7]['receipt']['block_at']) - timedelta(seconds=1), amount=D('1'))
        with case[0].session() as db:
            original = list_original_events(db, case[2])
        with pytest.raises(ProjectionError, match='prefijo aceptado'):
            with case[0].session() as db:
                append_event(db, case[2], str(uuid4()), item)
        with case[0].session() as db:
            assert list_original_events(db, case[2]) == original
            # Controlled fault injection also tests a row from an older writer
            # that bypassed the new admission guard. Never an owner's store.
            db.add(RealEvent(id=str(uuid4()), account_id=case[2], request_id=str(uuid4()),
                payload_digest=_digest(item), kind=item.kind, asset=None, quote_currency='USDT',
                quantity=None, unit_price=None, gross_amount=item.amount, fee=D('0'), fee_asset=None,
                occurred_at=item.occurred_at, recorded_at=datetime.now(timezone.utc), note=None))
            db.commit()
        with pytest.raises(ProjectionError, match='prefijo aceptado'):
            replay_accounting_view(capture(case[0], case[2]))


def test_late_arriving_earlier_transaction_in_same_block_requires_new_acceptance(tmp_path, monkeypatch):
    with same_block_case(tmp_path, monkeypatch) as case:
        witnesses = []
        sources = sorted(case.sources, key=lambda item: int(case.observations[item.key]['transaction']['transactionIndex'], 16), reverse=True)
        for index, original in enumerate(sources):
            scope = dict(context_account_id=case.account, wallet_address=case.meta['owner'],
                evidence=case.observations[original.key], now=case.at)
            # Reuse the real fixture's save helpers without changing its owner.
            tuple_case = (case.vault, case.account, case.ledger_account, case.meta)
            stored = save(tuple_case, original, scope, review(tuple_case, original, scope))
            module = wallet_swap_booking if original.kind == 'WALLET_SWAP' else dex_receipt_booking
            positional = (original.id,) if original.kind == 'WALLET_SWAP' else (original.kind, original.id)
            writer = module.book_swap if original.kind == 'WALLET_SWAP' else module.book_receipt
            if index:
                with pytest.raises(ProjectionError, match='anterior al prefijo'):
                    with case.vault.session() as db:
                        module.preview_booking(db, *positional,
                            context_account_id=case.account, wallet_address=case.meta['owner'])
            from app.portfolio import repository
            with monkeypatch.context() as injected:
                if index:
                    # Fault injection represents data admitted by an older
                    # writer. The real preflight above refused this booking;
                    # a joint book-and-review command is still pending.
                    injected.setattr(repository, '_accounting_inputs', lambda *args, **kwargs: None)
                with case.vault.session() as db:
                    proposal = module.preview_booking(db, *positional,
                        context_account_id=case.account, wallet_address=case.meta['owner'])
                with case.vault.session() as db:
                    writer(db, *positional, context_account_id=case.account, wallet_address=case.meta['owner'],
                        expected_revision=proposal['revision'], preview_digest=proposal['preview_digest'], now=case.at)
            with case.vault.session() as db:
                witnesses.append(capture_booked_order(db, stored['id'], wallet_address=case.meta['owner']))
            if index == 0:
                proposal = archive.preview_acceptance(case.vault, case.ledger_account, witnesses,
                    wallet_addresses=[case.meta['owner']])
                archive.confirm_acceptance(case.vault, case.ledger_account, witnesses,
                    wallet_addresses=[case.meta['owner']], preview_digest=proposal['preview_digest'],
                    now=max(case.at, datetime.now(timezone.utc)) + timedelta(minutes=1))
        with pytest.raises(ProjectionError):
            replay_accounting_view(capture(case.vault, case.ledger_account))
        correction = archive.preview_acceptance(case.vault, case.ledger_account, witnesses,
            wallet_addresses=[case.meta['owner']])
        archive.confirm_acceptance(case.vault, case.ledger_account, witnesses,
            wallet_addresses=[case.meta['owner']], preview_digest=correction['preview_digest'],
            now=max(case.at, datetime.now(timezone.utc)) + timedelta(minutes=2))
        assert replay_accounting_view(capture(case.vault, case.ledger_account)).claimed_transactions == 2
