"""Durable consumed-nonce evidence on owned SQLCipher files and signed Anvil."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_observation import verify_external_call
from app.portfolio import dex_external_observation as journal, dex_call_resolution as known, wallet_swap as swaps
from app.portfolio.models import RealDexExternalObservation, RealDexCallResolution, RealWalletSwapIntent
from app.portfolio.vault import RealVault, VaultError, SCHEMA_VERSION
from app.portfolio.wallet_funding import _encoded, _decoded
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.test_dex_external_observation import external_case, reference
from tests.test_economic_receipt_source import counts
from tests.test_wallet_swap import PASSWORD


def proposal(case, *, proof=None):
    proof = proof or verify_external_call(case.source, case.evidence, reference=case.reference, now=case.now)
    with case.vault.session() as db:
        row, _, _, _ = known.source_record(db, case.source.kind, case.source.id)
        return journal.preview(row, case.source.kind, proof)


def save(case, reviewed, *, vault=None, **changes):
    args = dict(account_id=case.account, wallet_address=case.meta['owner'],
        signer_address=case.evidence['transaction']['from'], tx_hash=case.hash,
        proof=reviewed['observation'], preview_digest=reviewed['preview_digest'],
        source_state_digest=reviewed['source_state_digest'], now=case.now)
    args.update(changes)
    with (vault or case.vault).session() as db:
        return journal.save_observation(db, case.source.kind, case.source.id, **args)


@pytest.mark.parametrize('mode', ['TRANSFER', 'SELF', 'ZERO', 'APPROVAL', 'MINT', 'UNKNOWN_TOKEN', 'NATIVE', 'CREATE', 'NO_LOGS'])
def test_save_reopen_cold_copy_preserves_facts_originals_and_pending_accounting(tmp_path, mode):
    with external_case(tmp_path, mode) as case:
        reviewed = proposal(case); before = counts(case.vault)
        result = save(case, reviewed)
        assert result['status'] == 'EFFECTS_PENDING' and result['nonce_consumed'] and result['saved']
        assert all(result[key] is False for key in ('effects_reviewed', 'accounting_complete', 'resolves_intent', 'moves_funds', 'trading_authority'))
        assert result['observation'] == reviewed['observation']
        assert save(case, reviewed) == result
        assert counts(case.vault) == before
        with case.vault.session() as db:
            row, _, _ = swaps.swap_record(db, case.source.id)
            assert (row.payload_json, row.state_json, row.updated_at) == case.before
            assert db.scalar(select(func.count()).select_from(RealDexCallResolution)) == 0
            persisted = journal.observation_record(db, result['id'])[0].payload_json
            for value in (case.meta['owner'], case.meta['operator'], case.hash):
                assert value.lower() not in persisted.lower()
            for nonce in (case.source.nonce, case.source.nonce+1):
                with pytest.raises(ValueError, match='efectos externos'):
                    assert_wallet_nonce_available(db, case.meta['owner'], str(nonce))
        with pytest.raises(ValueError, match='externa'):
            with case.vault.session() as db:
                swaps.report_swap(db, case.source.id, account_id=case.account, tx_hash=case.hash, now=case.now)
        cold_path = case.vault.backup(tmp_path/'cold.db')
        assert cold_path.read_bytes()[:16] != b'SQLite format 3\x00'
        cold = RealVault(cold_path); cold.unlock(PASSWORD)
        try:
            with cold.session() as db:
                assert journal.status(db, case.source.kind, case.source.id) == result
        finally:
            cold.lock()
        case.vault.lock(); case.vault.unlock(PASSWORD)
        with case.vault.session() as db:
            assert journal.status(db, case.source.kind, case.source.id) == result


def test_signature_bound_review_rejects_changed_source_wallet_sender_hash_or_preview(tmp_path):
    with external_case(tmp_path) as case:
        reviewed = proposal(case)
        for changes in ({'account_id': str(uuid4())}, {'wallet_address': case.meta['operator']},
                        {'signer_address': case.meta['operator']}, {'tx_hash': '0x'+'88'*32},
                        {'source_state_digest': '0'*64}, {'preview_digest': '0'*64}):
            with pytest.raises(ValueError):
                save(case, reviewed, **changes)
            with case.vault.session() as db:
                assert not list(db.scalars(select(RealDexExternalObservation)))
        assert save(case, reviewed)['saved']


def test_commit_failure_rolls_back_and_two_connections_save_once(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    with external_case(tmp_path) as case:
        reviewed = proposal(case)
        with monkeypatch.context() as patch:
            patch.setattr(Session, 'commit', lambda db: (_ for _ in ()).throw(OSError('owned commit failure')))
            with pytest.raises(OSError):
                save(case, reviewed)
        with case.vault.session() as db:
            assert not list(db.scalars(select(RealDexExternalObservation)))
            assert swaps.swap_record(db, case.source.id)[0].state_json == case.before[1]
        other = RealVault(case.vault.path); other.unlock(PASSWORD)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(save, case, reviewed); b = pool.submit(save, case, reviewed, vault=other)
                assert a.result(timeout=10) == b.result(timeout=10)
        finally:
            other.lock()
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexExternalObservation)) == 1


def test_archive_rejects_tampering_even_with_outer_digest_and_seal_recomputed(tmp_path):
    with external_case(tmp_path) as case:
        done = save(case, proposal(case))
        with case.vault.session() as db:
            row = db.get(RealDexExternalObservation, done['id']); raw, digest = row.payload_json, row.payload_digest
        for field, value in [('gas', '1'), ('authority', True), ('source', str(uuid4())), ('status', 'REVIEWED')]:
            with case.vault.session() as db:
                body = _decoded(raw, digest)
                if field == 'gas': body['proof']['receipt']['gas_fee_wei'] = value
                elif field == 'authority': body['proof']['trading_authority'] = value
                elif field == 'source': body['source_id'] = value
                else: body['status'] = value
                body['seal'] = journal._seal(db, {k: v for k, v in body.items() if k != 'seal'})
                row = db.get(RealDexExternalObservation, done['id']); row.payload_json, row.payload_digest = _encoded(body); db.commit()
            with pytest.raises(ValueError):
                with case.vault.session() as db: journal.observation_record(db, done['id'])
            with pytest.raises(VaultError): case.vault.backup(tmp_path/f'bad-{field}.db')
            with case.vault.session() as db:
                row = db.get(RealDexExternalObservation, done['id']); row.payload_json, row.payload_digest = raw, digest; db.commit()
        with case.vault.session() as db:
            db.connection().exec_driver_sql('PRAGMA user_version = 75'); db.commit()
        with pytest.raises(VaultError): case.vault.backup(tmp_path/'hidden-v75.db')
        with case.vault.session() as db:
            db.connection().exec_driver_sql(f'PRAGMA user_version = {SCHEMA_VERSION}'); db.commit()
        case.vault.backup(tmp_path/'good.db')


def test_legacy_migration_preserves_cipher_preimage_and_source_then_can_save(tmp_path):
    with external_case(tmp_path) as case:
        reviewed = proposal(case)
        with case.vault.session() as db:
            from tests.external_financial_fixture import drop_external_financial_tables
            drop_external_financial_tables(db)
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_order_proofs')
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_observations')
            db.connection().exec_driver_sql('PRAGMA user_version = 75'); db.commit()
        case.vault.lock(); case.vault.unlock(PASSWORD)
        preimage = case.vault.last_migration_backup
        assert preimage and preimage.read_bytes()[:16] != b'SQLite format 3\x00'
        old = RealVault(preimage); old.unlock(PASSWORD, allow_legacy_for_verification=True)
        try:
            with old.session() as db:
                assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() == 75
                assert swaps.swap_record(db, case.source.id)[0].state_json == case.before[1]
                assert journal.status(db, case.source.kind, case.source.id) is None
        finally: old.lock()
        assert save(case, reviewed)['saved']


def test_observation_excludes_known_native_and_deployment_resolution_nonce_or_receipt(tmp_path):
    from app.portfolio.dex_deployments import _identity
    from app.portfolio.dex_native_resolution import require_unused as native_unused
    with external_case(tmp_path) as case:
        save(case, proposal(case))
        with case.vault.session() as db:
            actor = _identity(db, 'wallet', case.meta['owner']); tx_ref = _identity(db, 'tx', case.hash)
            for check in (known.require_unused, native_unused):
                for signer, nonce, tx in ((actor, str(case.source.nonce), '0'*64), ('0'*64, '999', tx_ref)):
                    with pytest.raises(ValueError, match='nonce|recibo'): check(db, signer, nonce, tx)
            with pytest.raises(ValueError, match='externa'):
                known.preview_resolution(db, case.source.kind, case.source.id, account_id=case.account,
                    wallet_address=case.meta['owner'], evidence=case.evidence, now=case.now)


def test_observation_cannot_take_a_nonce_claimed_by_another_original(tmp_path):
    with external_case(tmp_path) as case:
        reviewed = proposal(case)
        with case.vault.session() as db:
            row, terms, state = swaps.swap_record(db, case.source.id)
            terms = deepcopy(terms); terms['id'] = str(uuid4()); raw, digest = _encoded(terms)
            db.add(RealWalletSwapIntent(id=terms['id'], account_id=row.account_id, wallet_digest=row.wallet_digest,
                status=row.status, payload_json=raw, payload_digest=digest, state_json=row.state_json,
                state_digest=row.state_digest, created_at=row.created_at, updated_at=row.updated_at))
            db.commit(); swaps.swap_record(db, terms['id'])
        with pytest.raises(ValueError, match='original'): save(case, reviewed)
        with case.vault.session() as db:
            assert not list(db.scalars(select(RealDexExternalObservation)))


def test_advancing_finalized_head_still_saves_exact_review_and_retries(tmp_path):
    with external_case(tmp_path) as case:
        reviewed = proposal(case)
        for _ in range(3): case.rpc('evm_mine', [])
        evidence = collect_call_resolution(case.rpc, case.source, case.hash)
        proof = verify_external_call(case.source, evidence, reference=case.reference, now=case.now+timedelta(seconds=5))
        assert proof == reviewed['observation']
        result = save(case, {**reviewed, 'observation': proof}, now=case.now+timedelta(seconds=5))
        assert save(case, reviewed, now=case.now+timedelta(seconds=10)) == result


def test_operator_observation_requires_stop_freezes_source_and_charges_actual_gas_once(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.portfolio import dex_operator_journal as operators
    from tests.test_dex_call_resolution_operator import case as operator_case, lower_price, pending
    from tests.test_dex_deployments import PASSWORD as operator_password
    with operator_case(tmp_path, evm_backend='anvil') as values:
        vault, account, meta, rpc, deployment, grant, _ = values
        trade = lower_price(meta, rpc, int(deployment['nonce'])); source, before, original = pending(values, trade)
        sent = rpc('eth_sendTransaction', [{'from': meta['operator'], 'to': meta['owner'],
            'nonce': hex(source.nonce), 'data': '0x', 'value': hex(10**15), 'gas': hex(21000), 'gasPrice': hex(10**12)}])
        rpc('miner_start', [1]); rpc('evm_mine', [])
        evidence = collect_call_resolution(rpc, source, sent)
        now = max(datetime.now(timezone.utc), datetime.fromtimestamp(int(evidence['block']['timestamp'], 16), timezone.utc))+timedelta(seconds=2)
        ref = reference(vault, 'OPERATOR')
        case = SimpleNamespace(vault=vault, account=account, meta=meta, source=source, evidence=evidence,
            now=now, hash=sent, reference=ref)
        reviewed = proposal(case)
        with pytest.raises(ValueError, match='Detén'): save(case, reviewed)
        with vault.session() as db: operators.stop_grant(db, grant['id'], account_id=account, now=now)
        done = save(case, reviewed)
        assert done['observation']['gas_payer_role'] == 'SIGNER' and done['observation']['native_target_role'] == 'OWNER'
        assert done['observation']['native_value_wei'] == str(10**15)
        assert done['observation']['receipt']['gas_fee_wei'] == str(21000*10**12)
        assert rpc('eth_getTransactionReceipt', [original]) is None
        with vault.session() as db:
            view = operators.grant_view(db, grant['id'], now=now)
            assert view['unresolved_outcomes'] and view['external_outcomes_pending'] == 1
            assert view['status'] == 'STOPPED' and view['gas_reserved_wei'] == '0'
            assert view['gas_spent_wei'] == str(21000*10**12)
            assert view['remaining_gas_wei'] == '0' and view['gas_overrun_wei'] == str(21000*10**12-int(grant['gas_budget_wei']))
            assert not view.get('resolved_outcomes', False)
            row, _, state = operators.intent_record(db, source.id)
            assert (row.payload_json, row.state_json, row.updated_at) == before
            with pytest.raises(ValueError, match='externa'): operators._advance(db, row, state, 'NONCE_OBSERVED', now)
        with vault.session() as db:
            with pytest.raises(ValueError, match='efectos externos'):
                assert_wallet_nonce_available(db, meta['operator'], str(source.nonce+1))
        assert save(case, reviewed) == done
        vault.backup(tmp_path/'operator-cold.db'); vault.lock(); vault.unlock(operator_password)
        with vault.session() as db:
            assert journal.status(db, 'OPERATOR', source.id) == done
            assert operators.grant_view(db, grant['id'], now=now)['gas_spent_wei'] == str(21000*10**12)


@pytest.mark.parametrize('legacy', [70, 75])
def test_legacy_guards_do_not_import_the_external_journal_before_migration(tmp_path, monkeypatch, legacy):
    import builtins
    from datetime import datetime, timezone
    from app.live_execution.dex_operator import DexOperatorSession
    from app.portfolio import dex_operator_journal as operators
    from tests.test_dex_operator_journal import case as operator_case, reserve
    from tests.test_dex_operator import KEY
    with operator_case(tmp_path, evm_backend='anvil') as (vault, account, meta, _, _, grant, trade):
        with vault.session() as db:
            from tests.external_financial_fixture import drop_external_financial_tables
            drop_external_financial_tables(db)
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_order_proofs')
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_observations')
            db.connection().exec_driver_sql(f'PRAGMA user_version = {legacy}'); db.commit()
        real_import = builtins.__import__
        def without_new_journal(name, globals=None, locals=None, fromlist=(), level=0):
            if (name == 'app.portfolio.dex_external_observation'
                    or name == 'app.portfolio' and 'dex_external_observation' in (fromlist or ())):
                raise AssertionError('Legacy operations must not import the v76 journal')
            return real_import(name, globals, locals, fromlist, level)
        signer = DexOperatorSession(vault)
        try:
            now = datetime.now(timezone.utc)
            signer.connect(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120,
                gas_budget_wei=2*10**15, backup_confirmed=True, now=now)
            with monkeypatch.context() as patch:
                patch.setattr(builtins, '__import__', without_new_journal)
                intent = reserve(vault, account, grant, trade)
                now = datetime.now(timezone.utc)
                with vault.session() as db:
                    operators.record_signature(db, intent['id'], account_id=account,
                        signed=signer.sign_swap(trade, now=now), plan=trade, now=now)
                with vault.session() as db:
                    assert operators.grant_view(db, grant['id'], now=now)['gas_reserved_wei'] == str(trade.gas*trade.gas_price)
                    known.require_unused(db, 'a'*64, '999', 'b'*64)
                    known.require_signer_nonce_available(db, meta['owner'], '999')
                    with pytest.raises(ValueError, match='pendiente'):
                        assert_wallet_nonce_available(db, meta['operator'], str(trade.operator_nonce+1))
        finally: signer.revoke()
