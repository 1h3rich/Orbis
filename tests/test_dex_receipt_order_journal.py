"""Sealed order records on real SQLCipher sources and disposable EVMs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import hashlib

import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio import dex_receipt_order as orders,dex_call_resolution as resolutions,wallet_swap as swaps
from app.portfolio.dex_receipt_order_source import archived_order_source
from app.portfolio.models import RealDexReceiptOrderProof,RealDexCallResolution
from app.portfolio.repository import account_snapshot,list_events
from app.portfolio.vault import RealVault,VaultError,SCHEMA_VERSION
from app.portfolio.wallet_funding import _encoded,_decoded
from tests.test_dex_call_booking import resolved_case
from tests.test_dex_receipt_order import source,evidence
from tests.test_dex_call_resolution import now
from tests.test_wallet_swap import PASSWORD
from tests.dex_order_fixture import same_block_case,finalized_wallet_order


def arguments(case,original,observed):
    return {'context_account_id':original.account_id,'wallet_address':case[3]['owner'],
        'evidence':observed,'now':max(now(),original.updated_at+timedelta(seconds=1))}


def review(case,original,scope):
    with case[0].session() as db:return orders.preview_order(db,original.kind,original.id,**scope)


def save(case,original,scope,proposed):
    with case[0].session() as db:return orders.confirm_order(db,original.kind,original.id,
        preview_digest=proposed['preview_digest'],**scope)


@pytest.mark.parametrize('typed',[False,True])
def test_saved_resolved_proof_survives_cipher_backup_without_addresses_or_ledger_effect(tmp_path,typed):
    with resolved_case(tmp_path,typed=typed) as case:
        vault,_,owner,meta,_,parent,_,resolved=case;original=source(case);observed=evidence(case,original)
        scope=arguments(case,original,observed)
        with vault.session() as db:
            events_before=list_events(db,owner);snapshot_before=account_snapshot(db,owner)
            before=(swaps.swap_record(db,parent.id)[0].state_json,resolutions.resolution_record(db,resolved['id'])[0].payload_json)
        proposed=review(case,original,scope);assert not proposed['already_saved'] and not proposed['applied_to_ledger']
        first=save(case,original,scope,proposed);assert first['created'] and first['saved'] and not first['trading_authority']
        again=save(case,original,scope,proposed);assert not again['created'] and {k:v for k,v in again.items() if k!='created'}=={k:v for k,v in first.items() if k!='created'}
        assert review(case,original,scope)['already_saved']
        with vault.session() as db:
            record=orders.order_record(db,first['id']);raw=record[0].payload_json
            archived=archived_order_source(db,original.kind,original.id,proof=record[1]['proof'])
            assert archived.call.owner!=meta['owner'] and archived.owner_digest==original.owner_digest
            assert list_events(db,owner)==events_before and account_snapshot(db,owner)==snapshot_before
            assert before==(swaps.swap_record(db,parent.id)[0].state_json,resolutions.resolution_record(db,resolved['id'])[0].payload_json)
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==1
        for value in (meta['owner'],meta['operator'],observed['tx_hash'],observed['transaction']['input']):assert value.lower() not in raw.lower()
        backup=vault.backup(tmp_path/'saved-order.db');assert backup.read_bytes()[:16]!=b'SQLite format 3\x00'
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            assert orders.order_record(db,first['id'])[0].payload_json==raw
            orders.verify_all_order_records(db,version=SCHEMA_VERSION)


def test_two_real_emitters_are_saved_in_reverse_arrival_order_without_rewriting_old_receipts(tmp_path,monkeypatch):
    with same_block_case(tmp_path,monkeypatch) as case:
        before={item.key:item.receipt_json for item in case.sources};saved=[]
        for original in sorted(case.sources,key=lambda item:int(case.observations[item.key]['transaction']['transactionIndex'],16),reverse=True):
            scope={'context_account_id':case.account,'wallet_address':case.meta['owner'],'evidence':case.observations[original.key],'now':case.at}
            with case.vault.session() as db:proposed=orders.preview_order(db,original.kind,original.id,**scope)
            with case.vault.session() as db:saved.append(orders.confirm_order(db,original.kind,original.id,preview_digest=proposed['preview_digest'],**scope))
        assert len({item['transaction_index'] for item in saved})==2
        with case.vault.session() as db:
            orders.verify_all_order_records(db,version=SCHEMA_VERSION)
            for saved_item in saved:
                row,body=orders.order_record(db,saved_item['id']);archived=archived_order_source(db,row.source_kind,body['source_id'],proof=body['proof'])
                assert archived.transaction_index is None and archived.receipt_json==before[archived.key]
        case.vault.backup(tmp_path/'both-senders.db')


def test_same_signer_original_wallet_and_resolved_receipts_can_be_imported_backwards(tmp_path):
    with resolved_case(tmp_path) as case:
        first=source(case);first_observed=evidence(case,first)
        later,later_observed=finalized_wallet_order(case)
        assert later.signer_digest==first.signer_digest and later.call.nonce>first.call.nonce
        saved=[]
        for original,observed in ((later,later_observed),(first,first_observed)):
            scope=arguments(case,original,observed);saved.append(save(case,original,scope,review(case,original,scope)))
        with case[0].session() as db:
            orders.verify_all_order_records(db,version=SCHEMA_VERSION)
            old=orders.order_record(db,saved[0]['id'])
            restored=archived_order_source(db,later.kind,later.id,proof=old[1]['proof'])
            assert restored.transaction_index is None and restored.receipt_json==later.receipt_json
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==2
        case[0].backup(tmp_path/'backfilled-signer.db')


def test_wrong_context_or_wallet_and_stale_preview_create_no_record(tmp_path):
    with resolved_case(tmp_path) as case:
        vault,_,other,meta,_,_,_,_=case;original=source(case);scope=arguments(case,original,evidence(case,original))
        for changed in ({'context_account_id':other},{'wallet_address':meta['operator']}):
            with pytest.raises(DexExecutorError):review(case,original,{**scope,**changed})
        proposed=review(case,original,scope)
        with pytest.raises(DexExecutorError,match='cambió'):save(case,original,scope,{**proposed,'preview_digest':'f'*64})
        with vault.session() as db:assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==0


def test_independent_sql_connections_confirm_once_after_a_lost_reply(tmp_path):
    with resolved_case(tmp_path) as case:
        original=source(case);scope=arguments(case,original,evidence(case,original));proposed=review(case,original,scope)
        barrier=Barrier(2)
        def writer():
            with Session(case[0]._engine) as db:
                barrier.wait(10)
                return orders.confirm_order(db,original.kind,original.id,preview_digest=proposed['preview_digest'],**scope)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:writer(),range(2)))
        assert sum(result['created'] for result in results)==1 and len({result['id'] for result in results})==1
        with case[0].session() as db:
            recovered=orders.public_record(orders.order_record(db,results[0]['id']))
            assert recovered['saved'] and db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==1


def test_failed_post_insert_verification_rolls_back_the_whole_record(tmp_path,monkeypatch):
    with resolved_case(tmp_path) as case:
        original=source(case);scope=arguments(case,original,evidence(case,original));proposed=review(case,original,scope)
        reader=orders.order_record
        def fail_after_flush(db,identifier):
            if db.get(RealDexReceiptOrderProof,identifier) is not None:raise RuntimeError('owned verification failure')
            return reader(db,identifier)
        monkeypatch.setattr(orders,'order_record',fail_after_flush)
        with pytest.raises(RuntimeError,match='owned verification'):save(case,original,scope,proposed)
        with case[0].session() as db:assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==0
        monkeypatch.setattr(orders,'order_record',reader)
        assert save(case,original,scope,proposed)['created']


def test_rehashed_record_changes_or_changed_original_are_rejected_after_warm_read(tmp_path):
    with resolved_case(tmp_path) as case:
        vault=case[0];original=source(case);scope=arguments(case,original,evidence(case,original));proposed=review(case,original,scope)
        saved=save(case,original,scope,proposed)
        with vault.session() as db:
            row,payload=orders.order_record(db,saved['id']);raw,digest=row.payload_json,row.payload_digest
        for key,value in [('context_account_id',case[2]),('wallet_digest','f'*64),('tx_digest','f'*64),('source_updated_at',now().isoformat())]:
            with vault.session() as db:
                row=db.get(RealDexReceiptOrderProof,saved['id']);row.payload_json,row.payload_digest=_encoded({**payload,key:value});db.commit()
            with pytest.raises(DexExecutorError):
                with vault.session() as db:orders.order_record(db,saved['id'])
            with vault.session() as db:
                row=db.get(RealDexReceiptOrderProof,saved['id']);row.payload_json,row.payload_digest=raw,digest;db.commit()
        for key,value in [('block_number','999'),('transaction_index','999'),('tx_digest','f'*64),('account_id',case[2])]:
            with vault.session() as db:
                row=db.get(RealDexReceiptOrderProof,saved['id']);previous=getattr(row,key);setattr(row,key,value);db.commit()
            with pytest.raises(DexExecutorError):
                with vault.session() as db:orders.order_record(db,saved['id'])
            with vault.session() as db:
                row=db.get(RealDexReceiptOrderProof,saved['id']);setattr(row,key,previous);db.commit()
        with vault.session() as db:
            parent=db.get(RealDexCallResolution,original.id);parent.payload_json,parent.payload_digest=_encoded({**_decoded(parent.payload_json,parent.payload_digest),'nonce':'999'});db.commit()
        with pytest.raises(DexExecutorError):
            with vault.session() as db:orders.order_record(db,saved['id'])
        with pytest.raises(VaultError,match='call resolution|order'):vault.backup(tmp_path/'forged-source.db')


def test_source_foreign_key_and_downgraded_schema_do_not_hide_saved_proofs(tmp_path):
    with resolved_case(tmp_path) as case:
        vault=case[0];original=source(case);scope=arguments(case,original,evidence(case,original));saved=save(case,original,scope,review(case,original,scope))
        with vault.session() as db:
            db.delete(db.get(RealDexCallResolution,original.id))
            with pytest.raises(IntegrityError):db.commit()
            db.rollback();assert orders.order_record(db,saved['id'])
            db.connection().exec_driver_sql('PRAGMA user_version=72');db.commit()
        with pytest.raises(VaultError,match='order'):vault.backup(tmp_path/'hidden-proof.db')
        before=hashlib.sha256(vault.path.read_bytes()).hexdigest();vault.lock()
        with pytest.raises(VaultError,match='order'):vault.unlock(PASSWORD)
        assert hashlib.sha256(vault.path.read_bytes()).hexdigest()==before


def test_actual_v72_migration_preserves_sources_and_ledger_and_adds_only_an_empty_order_archive(tmp_path):
    with resolved_case(tmp_path) as case:
        vault,_,owner,_,_,parent,_,resolved=case
        with vault.session() as db:
            before=(list_events(db,owner),swaps.swap_record(db,parent.id)[0].state_json,resolutions.resolution_record(db,resolved['id'])[0].payload_json)
            db.connection().exec_driver_sql('DROP TABLE real_dex_receipt_order_proofs')
            db.connection().exec_driver_sql('PRAGMA user_version=72');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup is not None and vault.last_migration_backup.read_bytes()[:16]!=b'SQLite format 3\x00'
        assert RealVault._verify_portfolio_file(vault.last_migration_backup,PASSWORD,expected_version=72)
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==0
            assert before==(list_events(db,owner),swaps.swap_record(db,parent.id)[0].state_json,resolutions.resolution_record(db,resolved['id'])[0].payload_json)


def test_decimal_string_comparisons_keep_large_nonces_and_heights_exact(tmp_path):
    from sqlalchemy import String,cast,literal
    from tests.test_wallet_swap import setup
    vault,_,_=setup(tmp_path)
    try:
        with vault.session() as db:
            for left,right in [('9','10'),('99','100'),('9223372036854775807','9223372036854775808'),
                               ('18446744073709551614','18446744073709551615'),('9'*77,'1'+'0'*77)]:
                assert db.scalar(select(orders._less(cast(literal(left),String),right))) is True
                assert db.scalar(select(orders._greater(cast(literal(right),String),left))) is True
    finally:vault.lock()
