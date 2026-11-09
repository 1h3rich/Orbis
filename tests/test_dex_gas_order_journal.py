"""Durable gas certificates on owned SQLCipher/Anvil; never owner funds."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
import hashlib,json

import pytest
from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio import dex_gas_order as gas,dex_receipt_order as trades,models as m
from app.portfolio.dex_gas_order_source import gas_source
from app.portfolio.vault import RealVault,VaultError,SCHEMA_VERSION
from app.portfolio.wallet_funding import _decoded,_encoded
from tests.gas_order_fixture import gas_case,mixed_order_case


def counts(vault):
    with vault.session()as db:
        return tuple(db.scalar(select(func.count()).select_from(model))for model in
            (m.RealEvent,m.RealWalletSwapBooking,m.RealDexReceiptBooking,m.RealEconomicAcceptance))


def preview(case,**changes):
    with case.vault.session()as db:return gas.preview_gas_order(db,case.kind,case.id,**{**case.scope,**changes})


def save(case,review,**changes):
    with case.vault.session()as db:return gas.confirm_gas_order(db,case.kind,case.id,
        preview_digest=review['preview_digest'],**{**case.scope,**changes})


@pytest.mark.parametrize('kind',list(m.GAS_ORDER_SOURCES))
def test_each_original_source_has_exact_foreign_key_encrypted_backup_and_idempotent_reopen(tmp_path,kind):
    with gas_case(tmp_path,kind)as case:
        before=counts(case.vault);review=preview(case)
        assert not review['already_saved']and counts(case.vault)==before
        first=save(case,review);again=save(case,review)
        assert first['created']and not again['created']and first['id']==again['id']
        assert first['saved']and not first['applied_to_ledger']and not first['trading_authority']
        assert preview(case)['already_saved']and counts(case.vault)==before
        with case.vault.session()as db:
            row,body=gas.gas_order_record(db,first['id']);raw=row.payload_json
            assert {field:getattr(row,field)for field,_ in m.GAS_ORDER_SOURCES.values()}=={
                field:case.id if family==kind else None for family,(field,_)in m.GAS_ORDER_SOURCES.items()}
            assert gas_source(db,kind,case.id,wallet_address=case.meta['owner'])==case.source
            trades.verify_all_order_records(db,version=SCHEMA_VERSION)
            for private in (case.meta['owner'],case.meta['operator'],case.scope['evidence']['tx_hash'],case.scope['evidence']['transaction']['r']):
                assert private.lower()not in raw.lower()
            data={column.name:getattr(row,column.name)for column in row.__table__.columns}
        # All-null/missing source and non-existent parent cannot be inserted.
        for source_fields in ({},{m.GAS_ORDER_SOURCES[kind][0]:str(uuid4())}):
            values={**data,'id':str(uuid4()),'tx_digest':'f'*64,'nonce':str(int(data['nonce'])+1000),
                'block_number':str(int(data['block_number'])+1000),'transaction_index':'0',
                **{field:None for field,_ in m.GAS_ORDER_SOURCES.values()},**source_fields}
            with case.vault.session()as db:
                db.add(m.RealDexGasOrderProof(**values))
                with pytest.raises(IntegrityError):db.commit()
                db.rollback()
        backup=case.vault.backup(tmp_path/'gas-order-copy.db')
        assert backup.read_bytes()[:16]!=b'SQLite format 3\x00'
        restored=RealVault(backup);restored.unlock(case.password)
        try:
            with restored.session()as db:
                assert gas.gas_order_record(db,first['id'])[0].payload_json==raw
                trades.verify_all_order_records(db,version=SCHEMA_VERSION)
        finally:restored.lock()
        case.vault.lock();case.vault.unlock(case.password)
        assert save(case,review)['created']is False and counts(case.vault)==before


@pytest.mark.parametrize('kind',['DEPLOYMENT','CALL_RESOLUTION'])
def test_typed_receipt_certificate_is_saved_without_changing_original_formats(tmp_path,kind):
    with gas_case(tmp_path,kind,typed=True)as case:
        saved=save(case,preview(case));assert saved['proof']['signature_kind']in ('CREATE','CALL')
        with case.vault.session()as db:assert gas.gas_order_record(db,saved['id'])[1]['proof']['purpose']=='NATIVE_GAS_ONLY'
        case.vault.backup(tmp_path/'typed-copy.db')


def test_wrong_scope_stale_review_and_post_insert_failure_leave_no_rows(tmp_path,monkeypatch):
    with gas_case(tmp_path)as case:
        before=counts(case.vault)
        for change in ({'context_account_id':str(uuid4())},{'wallet_address':case.meta['operator']}):
            with pytest.raises(DexExecutorError):preview(case,**change)
        review=preview(case)
        with pytest.raises(DexExecutorError):
            with case.vault.session()as db:gas.confirm_gas_order(db,case.kind,case.id,preview_digest='0'*64,**case.scope)
        original=gas.gas_order_record
        def fail_after_flush(db,identifier):
            value=original(db,identifier)
            if value is not None:raise DexExecutorError('Owned post-insert fault')
            return value
        with monkeypatch.context()as patch:
            patch.setattr(gas,'gas_order_record',fail_after_flush)
            with pytest.raises(DexExecutorError,match='Owned'):save(case,review)
        with case.vault.session()as db:assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==0
        assert counts(case.vault)==before and save(case,review)['created']


def test_two_independent_sql_connections_commit_one_gas_certificate(tmp_path):
    with gas_case(tmp_path)as case:
        review=preview(case);barrier=Barrier(2)
        def commit():
            with Session(case.vault._engine,expire_on_commit=False)as db:
                barrier.wait(timeout=10)
                return gas.confirm_gas_order(db,case.kind,case.id,preview_digest=review['preview_digest'],**case.scope)
        with ThreadPoolExecutor(max_workers=2)as pool:results=list(pool.map(lambda _:commit(),range(2)))
        assert sorted(item['created']for item in results)==[False,True]
        with case.vault.session()as db:assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==1


def test_downgraded_schema_cannot_hide_gas_or_change_the_file_on_failed_migration(tmp_path):
    with gas_case(tmp_path)as case:
        saved=save(case,preview(case))
        with case.vault.session()as db:
            row=db.get(m.RealDexGasOrderProof,saved['id']);body=_decoded(row.payload_json,row.payload_digest)
            body['proof']['gas_fee_wei']='1';row.payload_json,row.payload_digest=_encoded(body);db.commit()
        with pytest.raises(VaultError,match='order'):case.vault.backup(tmp_path/'tampered-gas.db')
        # Restore the authenticated record before testing a version downgrade.
        with case.vault.session()as db:
            row=db.get(m.RealDexGasOrderProof,saved['id']);body['proof']=saved['proof'];row.payload_json,row.payload_digest=_encoded(body)
            db.connection().exec_driver_sql('PRAGMA user_version=74');db.commit()
        with pytest.raises(VaultError,match='order'):case.vault.backup(tmp_path/'hidden-gas.db')
        case.vault.lock();before=hashlib.sha256(case.vault.path.read_bytes()).hexdigest()
        with pytest.raises(VaultError,match='order'):case.vault.unlock(case.password)
        assert hashlib.sha256(case.vault.path.read_bytes()).hexdigest()==before


def test_v74_migration_preserves_accepted_book_trade_proof_and_original_table(tmp_path):
    from tests.test_economic_acceptance import ready,proposed,confirm
    from app.portfolio.repository import list_events
    from app.portfolio import economic_acceptance as archive
    from tests.test_wallet_swap import PASSWORD as SWAP_PASSWORD
    with ready(tmp_path)as (case,witness):
        vault,_,owner,*_=case;saved=confirm(case,witness,proposed(case,witness))
        with vault.session()as db:
            original=list_events(db,owner);acceptance=archive.capture_acceptance(db,saved['id'])
            ddl=db.connection().exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='real_dex_receipt_order_proofs'").scalar_one()
            db.connection().exec_driver_sql('DROP TABLE real_dex_gas_order_proofs')
            from tests.external_financial_fixture import drop_external_financial_tables
            drop_external_financial_tables(db)
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_order_proofs')
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_observations')
            db.connection().exec_driver_sql('PRAGMA user_version=74');db.commit()
        vault.lock();vault.unlock(SWAP_PASSWORD)
        backup=vault.last_migration_backup
        assert backup is not None and backup.read_bytes()[:16]!=b'SQLite format 3\x00'
        RealVault._verify_portfolio_file(backup,SWAP_PASSWORD,expected_version=74)
        with vault.session()as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==0
            assert db.scalar(select(func.count()).select_from(m.RealDexExternalObservation))==0
            assert ddl==db.connection().exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='real_dex_receipt_order_proofs'").scalar_one()
            assert list_events(db,owner)==original and archive.capture_acceptance(db,saved['id'])==acceptance


@pytest.mark.parametrize('gas_first',[True,False])
def test_actual_same_block_gas_and_trade_use_one_history_guard_in_both_write_orders(tmp_path,monkeypatch,gas_first):
    from app.portfolio.dex_order_history import require_compatible_history
    with mixed_order_case(tmp_path,monkeypatch)as case:
        before=counts(case.vault)
        with case.vault.session()as db:gas_review=gas.preview_gas_order(db,case.gas.kind,case.gas.id,**case.gas_scope)
        with case.vault.session()as db:trade_review=trades.preview_order(db,case.trade.kind,case.trade.id,**case.trade_scope)
        calls=[]
        def checked_history(db,proof):
            calls.append(proof['schema']);return require_compatible_history(db,proof)
        monkeypatch.setattr(gas,'require_compatible_history',checked_history)
        monkeypatch.setattr(trades,'require_compatible_history',checked_history)
        def store_gas():
            with case.vault.session()as db:return gas.confirm_gas_order(db,case.gas.kind,case.gas.id,preview_digest=gas_review['preview_digest'],**case.gas_scope)
        def store_trade():
            with case.vault.session()as db:return trades.confirm_order(db,case.trade.kind,case.trade.id,preview_digest=trade_review['preview_digest'],**case.trade_scope)
        existing=(store_gas if gas_first else store_trade)()
        incoming=trade_review['proof']if gas_first else gas_review['proof'];other=existing['proof']
        # Admission authenticates the existing row in the OTHER table. These
        # conflicting candidate facts exercise the common layer independently
        # of the earlier signed-observation verifier, which has its own tests.
        wrongs=[{'common_tx_ref':other['common_tx_ref']},
            {'block_number':other['block_number'],'transaction_index':other['transaction_index']},
            {'signer_ref':other['signer_ref'],'nonce':other['nonce']},
            {'block_transactions_ref':'f'*64},{'block_transaction_count':str(int(other['block_transaction_count'])+1)}]
        if int(incoming['transaction_index'])<int(other['transaction_index']):
            wrongs.append({'signer_ref':other['signer_ref'],'nonce':str(int(other['nonce'])+1)})
        for change in wrongs:
            with case.vault.session()as db:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                with pytest.raises(DexExecutorError):require_compatible_history(db,{**incoming,**change})
                db.rollback()
        (store_trade if gas_first else store_gas)()
        assert len(calls)==2 and len(set(calls))==2
        assert counts(case.vault)==before
        with case.vault.session()as db:
            assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==1
            assert db.scalar(select(func.count()).select_from(m.RealDexReceiptOrderProof))==1
            trades.verify_all_order_records(db,version=SCHEMA_VERSION)
        case.vault.backup(tmp_path/'shared-order.db')


def test_archive_checks_cross_table_duplicates_even_when_each_local_certificate_is_sealed(tmp_path,monkeypatch):
    from app.live_execution.dex_gas_order import _seal as certificate_seal
    from app.portfolio.dex_gas_order_source import gas_reference
    with mixed_order_case(tmp_path,monkeypatch)as case:
        with case.vault.session()as db:proposal=gas.preview_gas_order(db,case.gas.kind,case.gas.id,**case.gas_scope)
        with case.vault.session()as db:saved=gas.confirm_gas_order(db,case.gas.kind,case.gas.id,preview_digest=proposal['preview_digest'],**case.gas_scope)
        with case.vault.session()as db:proposal=trades.preview_order(db,case.trade.kind,case.trade.id,**case.trade_scope)
        with case.vault.session()as db:other=trades.confirm_order(db,case.trade.kind,case.trade.id,preview_digest=proposal['preview_digest'],**case.trade_scope)
        with case.vault.session()as db:
            row,body=gas.gas_order_record(db,saved['id'])
            # Model a conflicting imported/previously attested certificate.
            # Local seals alone cannot replace the global archive invariant.
            body['proof']['common_tx_ref']=other['proof']['common_tx_ref']
            body['proof']['certificate_ref']=certificate_seal(body['proof'],gas_reference(db,case.gas))
            body['tx_digest']=body['proof']['common_tx_ref']
            body['seal']=gas._seal(db,{key:value for key,value in body.items()if key!='seal'})
            row.tx_digest=body['tx_digest']
            row.payload_json,row.payload_digest=_encoded(body);db.commit()
        with case.vault.session()as db:
            assert gas.gas_order_record(db,saved['id'])and trades.order_record(db,other['id'])
            with pytest.raises(DexExecutorError,match='repite'):trades.verify_all_order_records(db,version=SCHEMA_VERSION)
        with pytest.raises(VaultError,match='order'):case.vault.backup(tmp_path/'conflicting-certificates.db')


@pytest.mark.parametrize('backwards',[False,True])
def test_same_wallet_permission_and_later_swap_share_signer_identity_even_when_imported_backwards(tmp_path,backwards):
    from tests.test_dex_operator import write
    from tests.dex_order_fixture import finalized_wallet_order
    from app.live_execution.dex_receipt_order import collect_order
    with gas_case(tmp_path,'WALLET_SWAP')as case:
        owner_meta={**case.meta,'operator':case.meta['owner']}
        write(case.rpc,owner_meta,case.meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
        for token,recipient in [('usdt',case.meta['owner']),('wbnb',case.meta['router']),('wbnb',case.meta['pool'])]:
            write(case.rpc,owner_meta,case.meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**18])
        values=(case.vault,case.account,case.account,case.meta,case.rpc,None,None,None)
        trade,observed=finalized_wallet_order(values)
        assert trade.signer_digest==case.source.signer_digest and trade.call.nonce>case.source.nonce
        trade_scope={'context_account_id':case.account,'wallet_address':case.meta['owner'],'evidence':observed,
            'now':max(case.scope['now'],trade.updated_at+timedelta(seconds=1))}
        def save_trade():
            with case.vault.session()as db:review=trades.preview_order(db,trade.kind,trade.id,**trade_scope)
            with case.vault.session()as db:return trades.confirm_order(db,trade.kind,trade.id,preview_digest=review['preview_digest'],**trade_scope)
        if backwards:later=save_trade()
        earlier=save(case,preview(case))
        if not backwards:later=save_trade()
        assert earlier['proof']['signer_ref']==later['proof']['signer_ref']
        with case.vault.session()as db:trades.verify_all_order_records(db,version=SCHEMA_VERSION)
        case.vault.backup(tmp_path/'same-wallet-order.db')
