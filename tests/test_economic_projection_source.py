"""Booked-order bridge against real receipts on disposable EVMs and SQLCipher."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, func, event as sql_event

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio import dex_receipt_order as orders, dex_receipt_booking as booking
from app.portfolio import wallet_swap_booking as wallet_booking
from app.portfolio.economic_projection import ProjectionError, project_events
from app.portfolio.economic_projection_source import SCHEMA, capture_booked_order, claim_from_witness
from app.portfolio.models import RealAccount, RealEvent, RealWalletLink, RealWalletSwapBooking, RealWalletIdentitySalt
from app.portfolio.repository import account_snapshot, list_events
from app.portfolio.vault import RealVault
from app.portfolio.wallet_funding import _encoded, _decoded
from tests.dex_order_fixture import same_block_case, finalized_wallet_order
from tests.test_dex_call_booking import resolved_case, preview, book
from tests.test_dex_receipt_order import source, evidence
from tests.test_dex_receipt_order_journal import arguments, review, save
from tests.test_wallet_swap import PASSWORD, WALLET, setup


def saved_order(case):
    original = source(case)
    scope = arguments(case, original, evidence(case, original))
    return save(case, original, scope, review(case, original, scope))


@pytest.mark.parametrize('mode', ['BUY', 'SELL'])
def test_booked_resolved_receipt_reopens_as_pure_claim_with_exact_cash_units_and_gas(tmp_path, mode):
    with resolved_case(tmp_path, mode) as case:
        vault, account, owner, meta, _, _, _, resolved = case
        book(case, preview(case))
        saved = saved_order(case)
        with vault.session() as db:
            original = list_events(db, owner)
            before = account_snapshot(db, owner)
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            claim = claim_from_witness(db, witness)
            projection = project_events(original, [claim])
            assert list_events(db, owner) == original
        assert projection.snapshots[owner] == before
        assert claim.account_id == owner and claim.context_account_id == account
        assert claim.source_id == resolved['id'] and claim.source_kind == 'CALL_RESOLUTION'
        assert claim.transaction_index == int(saved['transaction_index'])
        assert len(claim.gas_event_ids) == 1
        assert before.cash['USDT'] == Decimal('97.99' if mode == 'BUY' else '98.01')
        assert before.positions[('WBNB', 'USDT')].quantity == Decimal('1.01' if mode == 'BUY' else '0.99')
        assert projection.claimed_transactions == 1 and projection.adjusted_transfer_legs == 0
        for private in (meta['owner'], meta['operator']):
            assert private.lower() not in _encoded(witness)[0].lower()
        backup = vault.backup(tmp_path / 'bridge.db')
        # A second encrypted file has the original salt and all dependencies;
        # the caller retains the witness until a projection is accepted.
        restored = RealVault(backup)
        restored.unlock(PASSWORD)
        try:
            with restored.session() as db:
                reopened = claim_from_witness(db, witness)
                assert reopened == claim
                assert project_events(list_events(db, owner), [reopened]) == projection
        finally:
            restored.lock()


def test_missing_booking_wrong_wallet_and_revoked_link_cannot_capture_authority(tmp_path):
    with resolved_case(tmp_path) as case:
        vault, _, owner, meta, _, _, _, _ = case
        saved = saved_order(case)
        with vault.session() as db:
            with pytest.raises(ProjectionError, match='Contabiliza'):
                capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
        book(case, preview(case))
        with vault.session() as db:
            with pytest.raises((ProjectionError, DexExecutorError)):
                capture_booked_order(db, saved['id'], wallet_address=meta['operator'])
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            claim = claim_from_witness(db, witness)
            link = db.scalar(select(RealWalletLink).where(RealWalletLink.account_id == owner))
            db.delete(link)
            db.commit()
        with vault.session() as db:
            with pytest.raises(ProjectionError, match='wallet vinculada'):
                capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            # Historical evidence survives unlinking; it grants no new capture
            # or trade permission and checks the actual originals again.
            assert claim_from_witness(db, witness) == claim


def test_witness_rehash_and_changed_registration_metadata_are_rejected_after_warm_read(tmp_path):
    with resolved_case(tmp_path) as case:
        vault, account, owner, meta, _, _, _, _ = case
        book(case, preview(case))
        saved = saved_order(case)
        with vault.session() as db:
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            claim = claim_from_witness(db, witness)
        for key, value in [('account_id', account), ('owner_digest', 'f' * 64),
                           ('booking_digest', '0' * 64), ('order_record_digest', '0' * 64)]:
            forged = {**witness, key: value}
            with vault.session() as db:
                with pytest.raises(ProjectionError):
                    claim_from_witness(db, forged)
        forged = deepcopy(witness)
        forged['events'][0]['recorded_at'] = '2030-01-01T00:00:00+00:00'
        with vault.session() as db:
            with pytest.raises(ProjectionError):
                claim_from_witness(db, forged)
        with vault.session() as db:
            row = db.get(RealEvent, claim.trade_event_id)
            original_time = row.recorded_at
            row.recorded_at += timedelta(seconds=1)
            db.commit()
        with vault.session() as db:
            with pytest.raises(ProjectionError):
                claim_from_witness(db, witness)
            row = db.get(RealEvent, claim.trade_event_id)
            row.recorded_at = original_time
            db.commit()
        with vault.session() as db:
            assert claim_from_witness(db, witness) == claim
            assert account_snapshot(db, owner).cash['USDT'] == Decimal('97.99')


def test_legacy_wallet_target_requires_live_binding_and_cannot_be_moved_by_public_rehash(tmp_path):
    with resolved_case(tmp_path) as case:
        vault, context, owner, meta, _, _, _, _ = case
        book(case, preview(case))
        original, observed = finalized_wallet_order(case)
        scope = arguments(case, original, observed)
        saved = save(case, original, scope, review(case, original, scope))
        with vault.session() as db:
            proposed = wallet_booking.preview_booking(db, original.id,
                context_account_id=context, wallet_address=meta['owner'])
        with vault.session() as db:
            wallet_booking.book_swap(db, original.id, context_account_id=context,
                wallet_address=meta['owner'], expected_revision=proposed['revision'],
                preview_digest=proposed['preview_digest'], now=scope['now'])
        with vault.session() as db:
            witness = capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            claim = claim_from_witness(db, witness)
            assert claim.account_id == owner
            assert project_events(list_events(db, owner), [claim]).claimed_transactions == 1
            row = db.get(RealWalletSwapBooking, original.id)
            payload = _decoded(row.payload_json, row.payload_digest)
            row.account_id = context
            row.payload_json, row.payload_digest = _encoded({**payload, 'account_id': context})
            for reference in payload['events']:
                db.get(RealEvent, reference['id']).account_id = context
            db.commit()
        with vault.session() as db:
            # Legacy public digests alone do not authenticate target ownership.
            assert wallet_booking.booking_record(db, original.id)[0].account_id == context
            with pytest.raises(ProjectionError, match='wallet vinculada'):
                capture_booked_order(db, saved['id'], wallet_address=meta['owner'])
            with pytest.raises(ProjectionError):
                claim_from_witness(db, witness)


def test_actual_two_emitter_block_claims_use_global_indices_and_never_arrival_order(tmp_path, monkeypatch):
    with same_block_case(tmp_path, monkeypatch) as case:
        claims = []
        for original in reversed(case.sources):
            scope = dict(context_account_id=case.account, wallet_address=case.meta['owner'],
                evidence=case.observations[original.key], now=case.at)
            with case.vault.session() as db:
                proposed = orders.preview_order(db, original.kind, original.id, **scope)
            with case.vault.session() as db:
                saved = orders.confirm_order(db, original.kind, original.id,
                    preview_digest=proposed['preview_digest'], **scope)
            module = wallet_booking if original.kind == 'WALLET_SWAP' else booking
            positional = (original.id,) if original.kind == 'WALLET_SWAP' else (original.kind, original.id)
            with case.vault.session() as db:
                proposal = module.preview_booking(db, *positional,
                    context_account_id=case.account, wallet_address=case.meta['owner'])
            writer = module.book_swap if original.kind == 'WALLET_SWAP' else module.book_receipt
            with case.vault.session() as db:
                writer(db, *positional, context_account_id=case.account, wallet_address=case.meta['owner'],
                    expected_revision=proposal['revision'], preview_digest=proposal['preview_digest'], now=case.at)
            with case.vault.session() as db:
                witness = capture_booked_order(db, saved['id'], wallet_address=case.meta['owner'])
                claims.append(claim_from_witness(db, witness))
        assert {claim.source_kind for claim in claims} == {'WALLET_SWAP', 'OPERATOR'}
        assert {claim.transaction_index for claim in claims} == {0, 1}
        with case.vault.session() as db:
            events = list_events(db, case.ledger_account)
            projected = project_events(events, claims)
            actual = [event.order_record_id for event in projected.events if event.accounting_input.kind == 'BUY']
            assert actual == [claim.order_record_id for claim in sorted(claims, key=lambda claim: claim.transaction_index)]
            assert projected.snapshots[case.ledger_account] == account_snapshot(db, case.ledger_account)
            assert list_events(db, case.ledger_account) == events


@pytest.mark.parametrize('operation', ['reopen', 'capture', 'review'])
@pytest.mark.parametrize('pending', [False, True])
def test_invalid_witness_on_new_vault_never_creates_an_identity_or_other_row(tmp_path, operation, pending):
    from app.portfolio.economic_projection_review import capture_projection_inputs
    vault, account, _ = setup(tmp_path)
    statements = []
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().split(None, 1)[0].upper())
    sql_event.listen(vault._engine, 'before_cursor_execute', observe)
    try:
        with vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealWalletIdentitySalt)) == 0
            if pending:
                db.get(RealAccount, account).name = 'Unsaved alias'
            with pytest.raises(ProjectionError):
                if operation == 'reopen':
                    claim_from_witness(db, {'schema': SCHEMA, 'seal': '0' * 64})
                elif operation == 'capture':
                    capture_booked_order(db, account, wallet_address=WALLET)
                else:
                    capture_projection_inputs(db, account, [{'schema': SCHEMA, 'seal': '0' * 64}])
            with db.no_autoflush:
                assert db.scalar(select(func.count()).select_from(RealWalletIdentitySalt)) == 0
            assert not db.new and not db.deleted and bool(db.dirty) == pending
            assert not {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER'} & set(statements)
    finally:
        vault.lock()
