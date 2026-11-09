"""Accepted accounting prefixes on disposable EVMs and encrypted owned stores."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.portfolio import economic_acceptance as archive
from app.portfolio.economic_projection import ProjectionError
from app.portfolio.economic_projection_source import capture_booked_order
from app.portfolio.domain import EventInput
from app.portfolio.models import RealEconomicAcceptance as Record, RealEconomicAcceptanceAccount as Member
from app.portfolio.repository import append_event, account_snapshot, list_events, list_original_events, create_account, transfer_cash_between_accounts
from app.portfolio.vault import RealVault, VaultError, SCHEMA_VERSION
from app.portfolio.wallet_funding import _encoded
from tests.test_dex_call_booking import resolved_case, preview, book
from tests.test_economic_projection_source import saved_order
from tests.test_wallet_swap import PASSWORD
from tests.test_dex_operator import write
from tests.dex_order_fixture import finalized_wallet_order
from tests.test_dex_receipt_order_journal import arguments, review, save


@contextmanager
def ready(directory, mode='BUY'):
    with resolved_case(directory, mode) as case:
        book(case, preview(case))
        saved = saved_order(case)
        with case[0].session() as db:
            witness = capture_booked_order(db, saved['id'], wallet_address=case[3]['owner'])
        yield case, witness


def proposed(case, witness):
    return archive.preview_acceptance(case[0], case[2], [witness], wallet_addresses=[case[3]['owner']])


def confirm(case, witness, proposal, *, at=None, **kwargs):
    return archive.confirm_acceptance(case[0], case[2], [witness], wallet_addresses=[case[3]['owner']],
        preview_digest=proposal['preview_digest'], now=at or datetime.now(timezone.utc) + timedelta(minutes=1), **kwargs)


def second_wallet(case):
    from app.portfolio import wallet_swap_booking
    from app.portfolio.wallet_links import save_wallet_link
    vault, context, _, meta, rpc, _, _, _ = case
    alternate = {**meta, 'owner': meta['operator']}
    write(rpc, meta, meta['usdt'], 'mint(address,uint256)', ['address', 'uint256'], [alternate['owner'], 10**18])
    write(rpc, alternate, meta['usdt'], 'approve(address,uint256)', ['address', 'uint256'], [meta['router'], 10**18])
    with vault.session() as db:
        owner = create_account(db, 'Otra wallet', 'USDT').id
        save_wallet_link(db, owner, alternate['owner'], '0x38', 'Segunda')
        db.commit()
    for currency, amount in [('USDT', '100'), ('BNB', '0.01')]:
        with vault.session() as db:
            append_event(db, owner, str(uuid4()), EventInput('CONTRIBUTION', currency,
                datetime.now(timezone.utc) - timedelta(days=1), amount=Decimal(amount)))
    alternate_case = (*case[:2], owner, alternate, *case[4:])
    original, observed = finalized_wallet_order(alternate_case)
    scope = arguments(alternate_case, original, observed)
    stored = save(alternate_case, original, scope, review(alternate_case, original, scope))
    with vault.session() as db:
        proposal = wallet_swap_booking.preview_booking(db, original.id, context_account_id=context,
            wallet_address=alternate['owner'])
    with vault.session() as db:
        wallet_swap_booking.book_swap(db, original.id, context_account_id=context, wallet_address=alternate['owner'],
            expected_revision=proposal['revision'], preview_digest=proposal['preview_digest'], now=scope['now'])
    with vault.session() as db:
        witness = capture_booked_order(db, stored['id'], wallet_address=alternate['owner'])
    return owner, alternate['owner'], witness


@pytest.mark.parametrize('mode', ['BUY', 'SELL'])
def test_exact_prefix_survives_later_appends_cipher_backup_and_offline_replay(tmp_path, mode):
    with ready(tmp_path, mode) as (case, witness):
        vault, _, owner, meta, _, _, _, resolved = case
        with vault.session() as db:
            original = list_original_events(db, owner)
            snapshot = account_snapshot(db, owner)
        proposal = proposed(case, witness)
        first = confirm(case, witness, proposal)
        again = confirm(case, witness, proposal)
        assert first['created'] and not again['created'] and first['id'] == again['id']
        assert not first['applied_to_ledger'] and not first['trading_authority']
        with vault.session() as db:
            archived = archive.capture_acceptance(db, first['id'])
            assert list_original_events(db, owner) == original and account_snapshot(db, owner) == snapshot
        reviewed = archive.replay_acceptance(archived)
        assert reviewed.projection.snapshots[owner] == snapshot
        for address in (meta['owner'], meta['operator']):
            assert address.lower() not in archived.payload_json.lower()
        # The new row may have the same/earlier registration clock than the
        # test acceptance. IDs, rather than a time predicate, fix the prefix.
        with vault.session() as db:
            later = append_event(db, owner, str(uuid4()), EventInput('CONTRIBUTION', 'USDT',
                datetime.fromisoformat(resolved['receipt']['block_at']) + timedelta(seconds=2), amount=Decimal('3')))
            assert later.recorded_at < datetime.fromisoformat(first['recorded_at'])
        with vault.session() as db:
            captured = archive.capture_acceptance(db, first['id'])
            assert later.id not in {event.id for event in captured.inputs.events}
        assert archive.replay_acceptance(captured) == reviewed
        backup = vault.backup(tmp_path / 'accepted.db')
        assert backup.read_bytes()[:16] != b'SQLite format 3\x00'
        restored = RealVault(backup)
        restored.unlock(PASSWORD)
        with restored.session() as db:
            reopened = archive.capture_acceptance(db, first['id'])
        restored.lock()
        assert archive.replay_acceptance(reopened) == reviewed
        assert archive.identifier(first['draft_digest']) == first['id']


def test_stale_book_and_wrong_wallet_do_not_save_a_record(tmp_path):
    with ready(tmp_path) as (case, witness):
        vault, _, owner, meta, _, _, _, resolved = case
        proposal = proposed(case, witness)
        with pytest.raises(ProjectionError):
            archive.preview_acceptance(vault, owner, [witness], wallet_addresses=[meta['operator']])
        with vault.session() as db:
            append_event(db, owner, str(uuid4()), EventInput('CONTRIBUTION', 'USDT',
                datetime.fromisoformat(resolved['receipt']['block_at']) + timedelta(seconds=1), amount=Decimal('1')))
        with pytest.raises(ProjectionError, match='cambió'):
            confirm(case, witness, proposal)
        with vault.session() as db:
            assert db.scalar(select(func.count()).select_from(Record)) == 0
            assert db.scalar(select(func.count()).select_from(Member)) == 0


def test_lock_during_pure_calculation_cancels_the_acceptance_generation(tmp_path, monkeypatch):
    with ready(tmp_path) as (case, witness):
        proposal = proposed(case, witness)
        original = archive._draft
        with ThreadPoolExecutor(max_workers=1) as pool:
            def lock_after_calculation(*args):
                result = original(*args)
                # A different thread must obtain the lease while pure replay
                # is running; same-thread RLock reentry would not prove this.
                pool.submit(case[0].lock).result(timeout=5)
                return result
            monkeypatch.setattr(archive, '_draft', lock_after_calculation)
            with pytest.raises(VaultError, match='session changed'):
                confirm(case, witness, proposal)
        case[0].unlock(PASSWORD)
        with case[0].session() as db:
            assert db.scalar(select(func.count()).select_from(Record)) == 0


def test_two_independent_sql_connections_commit_once_and_recover_a_lost_response(tmp_path, monkeypatch):
    with ready(tmp_path) as (case, witness):
        proposal = proposed(case, witness)
        barrier = Barrier(2)
        original = archive._draft
        def meet_outside_sql(*args):
            result = original(*args)
            barrier.wait(timeout=10)
            return result
        monkeypatch.setattr(archive, '_draft', meet_outside_sql)
        class IndependentConnections:
            # Controlled fixture bypasses the in-process lease to exercise the
            # SQL transaction/unique constraints with actual separate sessions.
            session_generation = 1
            @contextmanager
            def guard_session_generation(self, generation):
                assert generation == 1
                yield
            @contextmanager
            def session(self):
                with Session(case[0]._engine) as db:
                    yield db
        def writer():
            return archive.confirm_acceptance(IndependentConnections(), case[2], [witness],
                wallet_addresses=[case[3]['owner']], preview_digest=proposal['preview_digest'],
                now=datetime.now(timezone.utc) + timedelta(minutes=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: writer(), range(2)))
        assert sum(result['created'] for result in results) == 1
        assert len({result['id'] for result in results}) == 1
        monkeypatch.setattr(archive, '_draft', original)
        assert not confirm(case, witness, proposal)['created']
        with case[0].session() as db:
            assert db.scalar(select(func.count()).select_from(Record)) == 1
            assert db.scalar(select(func.count()).select_from(Member)) == 1


def test_post_insert_validation_failure_rolls_back_record_and_all_members(tmp_path, monkeypatch):
    with ready(tmp_path) as (case, witness):
        proposal = proposed(case, witness)
        original = archive._lineage
        def fail_after_insert(db, record_id):
            if db.get(Record, record_id) is not None:
                raise RuntimeError('owned post-insert failure')
            return original(db, record_id)
        monkeypatch.setattr(archive, '_lineage', fail_after_insert)
        with pytest.raises(RuntimeError, match='owned post-insert'):
            confirm(case, witness, proposal)
        with case[0].session() as db:
            assert db.scalar(select(func.count()).select_from(Record)) == 0
            assert db.scalar(select(func.count()).select_from(Member)) == 0
        monkeypatch.setattr(archive, '_lineage', original)
        assert confirm(case, witness, proposal)['created']


def test_public_rehash_does_not_validate_saved_results_or_changed_member_rows(tmp_path):
    with ready(tmp_path) as (case, witness):
        saved = confirm(case, witness, proposed(case, witness))
        with case[0].session() as db:
            row = db.get(Record, saved['id'])
            raw, digest = row.payload_json, row.payload_digest
            payload = json.loads(raw)
            payload['draft']['review']['comparisons'][0]['after']['cash']['USDT'] = '999999'
            row.payload_json, row.payload_digest = _encoded(payload)
            db.commit()
        with pytest.raises(ProjectionError):
            with case[0].session() as db:
                archive.capture_acceptance(db, saved['id'])
        with case[0].session() as db:
            row = db.get(Record, saved['id'])
            row.payload_json, row.payload_digest = raw, digest
            db.commit()
            archived = archive.capture_acceptance(db, saved['id'])
        fake = json.loads(archived.payload_json)
        fake['draft']['review']['comparisons'][0]['after']['cash']['USDT'] = '999999'
        with pytest.raises(ProjectionError, match='reproduce'):
            archive.replay_acceptance(archive.ArchivedAcceptance(json.dumps(fake), archived.inputs))
        with case[0].session() as db:
            db.delete(db.get(Member, (saved['id'], case[2])))
            db.commit()
        with pytest.raises(ProjectionError):
            with case[0].session() as db:
                archive.capture_applicable_acceptance(db, case[2])
        with pytest.raises(ProjectionError):
            proposed(case, witness)
        with pytest.raises(VaultError, match='acceptance'):
            case[0].backup(tmp_path / 'missing-member.db')


def test_v73_migration_adds_empty_archive_and_preserves_originals(tmp_path):
    from app.portfolio.models import RealDexExternalObservation
    with ready(tmp_path) as (case, witness):
        with case[0].session() as db:
            original = list_events(db, case[2])
            from tests.external_financial_fixture import drop_external_financial_tables
            drop_external_financial_tables(db)
            db.connection().exec_driver_sql('DROP TABLE real_economic_acceptance_accounts')
            db.connection().exec_driver_sql('DROP TABLE real_economic_acceptances')
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_order_proofs')
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_observations')
            db.connection().exec_driver_sql('PRAGMA user_version=73')
            db.commit()
        case[0].lock()
        case[0].unlock(PASSWORD)
        backup = case[0].last_migration_backup
        assert backup is not None and backup.read_bytes()[:16] != b'SQLite format 3\x00'
        assert RealVault._verify_portfolio_file(backup, PASSWORD, expected_version=73)
        with case[0].session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() == SCHEMA_VERSION
            assert list_events(db, case[2]) == original
            assert db.scalar(select(func.count()).select_from(Record)) == 0
            assert db.scalar(select(func.count()).select_from(RealDexExternalObservation)) == 0
        assert proposed(case, witness)['draft']['review']['events_count'] == len(original)


def test_downgraded_schema_cannot_hide_acceptances_or_change_owner_file_on_failed_migration(tmp_path):
    with ready(tmp_path) as (case, witness):
        confirm(case, witness, proposed(case, witness))
        with case[0].session() as db:
            db.connection().exec_driver_sql('PRAGMA user_version=73')
            db.commit()
        with pytest.raises(VaultError, match='acceptance'):
            case[0].backup(tmp_path / 'hidden.db')
        case[0].lock()
        before = hashlib.sha256(case[0].path.read_bytes()).hexdigest()
        with pytest.raises(VaultError, match='acceptance'):
            case[0].unlock(PASSWORD)
        assert hashlib.sha256(case[0].path.read_bytes()).hexdigest() == before


def test_merge_keeps_both_account_lineages_and_recovers_old_reply_after_new_bookings(tmp_path):
    with ready(tmp_path) as (case, witness):
        vault, _, owner, meta, _, _, _, _ = case
        old_proposal = proposed(case, witness)
        first = confirm(case, witness, old_proposal)
        peer, address, other_witness = second_wallet(case)
        separate = archive.preview_acceptance(vault, peer, [other_witness], wallet_addresses=[address])
        second_at = datetime.fromisoformat(first['recorded_at']) + timedelta(seconds=1)
        second = archive.confirm_acceptance(vault, peer, [other_witness], wallet_addresses=[address],
            preview_digest=separate['preview_digest'], now=second_at)
        with vault.session() as db:
            movement = transfer_cash_between_accounts(db, owner, peer, str(uuid4()), currency='USDT',
                amount=Decimal('1'), occurred_at=datetime.now(timezone.utc) + timedelta(seconds=5))
        witnesses = [witness, other_witness]
        wallets = [meta['owner'], address]
        merged = archive.preview_acceptance(vault, owner, witnesses, wallet_addresses=wallets)
        assert {key: value['id'] for key, value in merged['draft']['parents'].items()} == {
            owner: first['id'], peer: second['id']}
        accepted = archive.confirm_acceptance(vault, owner, witnesses, wallet_addresses=wallets,
            preview_digest=merged['preview_digest'], now=second_at + timedelta(seconds=1))
        with vault.session() as db:
            historical = archive.capture_acceptance(db, first['id'])
            combined = archive.capture_acceptance(db, accepted['id'])
            assert set(historical.inputs.accounts) == {owner}
            assert set(combined.inputs.accounts) == {owner, peer}
            assert movement['source_event_id'] not in {event.id for event in historical.inputs.events}
            assert movement['source_event_id'] in {event.id for event in combined.inputs.events}
        assert archive.replay_acceptance(combined).projection.snapshots[peer].cash['USDT'] == Decimal('100.99')
        # The accepted historical wallet binding needs no retained full address.
        single_wallet = archive.preview_acceptance(vault, owner, witnesses, wallet_addresses=[address])
        assert all(parent['id']==accepted['id']for parent in single_wallet['draft']['parents'].values())
        with pytest.raises(ProjectionError,match='wallet actual'):
            archive.preview_acceptance(vault, owner, witnesses, wallet_addresses=['0x'+'ff'*20])
        recovered = confirm(case, witness, old_proposal)
        assert not recovered['created'] and recovered['id'] == first['id']
        # Historical prefix remains reproducible after the linked accounts merge.
        assert archive.replay_acceptance(historical).projection.snapshots[owner].cash['USDT'] == Decimal('97.99')
        vault.backup(tmp_path / 'merged-lineages.db')


def test_acceptance_selector_preserves_prior_method_and_rejects_stale_parent_or_clock(tmp_path):
    with ready(tmp_path) as (case, witness):
        first = confirm(case, witness, proposed(case, witness))
        first_at = datetime.fromisoformat(first['recorded_at'])
        with case[0].session() as db:
            append_event(db, case[2], str(uuid4()), EventInput('CONTRIBUTION', 'USDT',
                first_at + timedelta(seconds=1), amount=Decimal('4')))
        next_proposal = proposed(case, witness)
        assert next_proposal['draft']['parents'][case[2]]['id'] == first['id']
        with pytest.raises(ProjectionError, match='reloj'):
            confirm(case, witness, next_proposal, at=first_at)
        second = confirm(case, witness, next_proposal, at=first_at + timedelta(seconds=2))
        with case[0].session() as db:
            assert archive.capture_applicable_acceptance(db, case[2],
                accepted_through=first_at - timedelta(microseconds=1)) is None
            historical = archive.capture_applicable_acceptance(db, case[2], accepted_through=first_at)
            current = archive.capture_applicable_acceptance(db, case[2])
            assert json.loads(historical.payload_json)['id'] == first['id']
            assert json.loads(current.payload_json)['id'] == second['id']
        assert archive.replay_acceptance(historical).projection.snapshots[case[2]].cash['USDT'] == Decimal('97.99')
        assert archive.replay_acceptance(current).projection.snapshots[case[2]].cash['USDT'] == Decimal('101.99')
        # A new head makes the draft obtained before it stale, even if its
        # original raw event rows have not changed.
        before_head = proposed(case, witness)
        confirm(case, witness, before_head, at=first_at + timedelta(seconds=3))
        fresh = proposed(case, witness)
        assert fresh['preview_digest'] != before_head['preview_digest']
        # Already committed drafts remain idempotently recoverable.
        assert not confirm(case, witness, before_head)['created']


def test_client_edited_results_are_recomputed_and_never_accepted_from_the_digest(tmp_path):
    with ready(tmp_path) as (case, witness):
        proposal = proposed(case, witness)
        fake = deepcopy(proposal['draft'])
        fake['review']['comparisons'][0]['after']['cash']['USDT'] = '999999'
        fake_digest = _encoded(fake)[1]
        with pytest.raises(ProjectionError, match='cambió'):
            confirm(case, witness, {**proposal, 'preview_digest': fake_digest})
        with case[0].session() as db:
            assert db.scalar(select(func.count()).select_from(Record)) == 0


def test_even_sealed_saved_snapshot_is_replayed_instead_of_becoming_a_balance(tmp_path, monkeypatch):
    with ready(tmp_path) as (case, witness):
        original = archive._draft
        def incorrect_writer(*args):
            draft, _ = original(*args)
            draft['review']['comparisons'][0]['after']['cash']['USDT'] = '999999'
            return draft, _encoded(draft)[1]
        # Fault injection simulates a future calculation/writer regression;
        # this is not a client path and does not involve an owner's store.
        with monkeypatch.context() as injected:
            injected.setattr(archive, '_draft', incorrect_writer)
            saved = confirm(case, witness, proposed(case, witness))
        with case[0].session() as db:
            archived = archive.capture_acceptance(db, saved['id'])
        with pytest.raises(ProjectionError, match='reproduce'):
            archive.replay_acceptance(archived)
        with pytest.raises(VaultError, match='acceptance'):
            case[0].backup(tmp_path / 'invalid-sealed-result.db')
