"""Durable cancellation grants, explicit same-nonce replacement and one fee."""
from uuid import uuid4
from datetime import timedelta
from decimal import Decimal
import pytest
from app.live_execution.dex_gas_cancellation import read_cancellation_state,plan_cancellation
from app.live_execution.dex_native_resolution import collect_resolution
from app.portfolio import dex_gas_cancellation as journal,dex_gas_return as returns,dex_receipt_booking as accounting
from app.portfolio.repository import append_event,account_snapshot
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.domain import EventInput
from tests.dex_gas_cancellation_fixture import pending_return,cancellation_review,cancellation_plan
from tests.test_dex_gas_funding import now
from tests.test_dex_deployments import PASSWORD


def test_explicit_new_cancellation_after_unknown_post_keeps_nonce_and_books_only_winner(tmp_path):
    with pending_return(tmp_path) as (values,original,original_hash,sign):
        vault,account,manager,draft,meta,rpc,_,_=values
        def prepare(price):
            snapshot=read_cancellation_state(rpc,meta['owner'],draft['operator_address'],nonce=int(original['nonce']),now=now())
            with vault.session() as db:return journal.prepare_cancellation(db,identifier=str(uuid4()),return_id=original['id'],account_id=account,
                wallet_address=meta['owner'],draft_id=draft['id'],drafts=manager,snapshot=snapshot,gas_price=price,bump_percent=10,max_gas_wei=10**14,now=now())
        def grant(record):
            state=read_cancellation_state(rpc,meta['owner'],draft['operator_address'],nonce=int(original['nonce']),now=now())
            plan=plan_cancellation(state,original_nonce=int(original['nonce']),original_gas_price=record['base_gas_price'],gas_price=record['gas_price'],bump_percent=10,max_gas_wei=10**14,now=now())
            with vault.session() as db:journal.reserve_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],draft_id=draft['id'],drafts=manager,plan=plan,plan_digest=record['plan_digest'],now=now())
            signed=sign(plan.transaction());tx_hash='0x'+signed.hash.hex()
            with vault.session() as db:journal.record_signature(db,record['id'],account_id=account,plan=plan,raw=bytes(signed.raw_transaction),tx_hash=tx_hash,now=now())
            with vault.session() as db:journal.mark_broadcast(db,record['id'],account_id=account,plan_digest=record['plan_digest'],tx_hash=tx_hash,now=now())
            return signed,tx_hash
        with vault.session() as db:
            parent=returns.return_record(db,original['id'])[0];saved=(parent.state_json,parent.state_digest)
        first=prepare(1200000000);_,first_hash=grant(first)
        with pytest.raises(ValueError):
            with vault.session() as db:journal.mark_broadcast(db,first['id'],account_id=account,plan_digest=first['plan_digest'],tx_hash=first_hash,now=now())
        # The first cancellation POST never arrived. A new explicit, more
        # expensive zero-self mandate may replace it, never replay its bytes.
        with pytest.raises(ValueError):prepare(1250000000)
        second=prepare(1500000000);assert second['base_gas_price']==1200000000 and second['previous_posts']==1
        signed,tx_hash=grant(second);rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
        rpc('miner_start',[1]);rpc('evm_mine',[])
        evidence=collect_resolution(rpc,tx_hash)
        with vault.session() as db:
            settled=journal.observe_cancellation(db,second['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
            assert settled['status']=='SETTLED' and settled['winner']=='CANCELLATION'
        with vault.session() as db:
            old=journal.settle_known(db,first['id'],account_id=account,now=now())
            assert old['status']=='SETTLED' and old['winner_id']==second['id']
            parent=returns.return_record(db,original['id'])[0];assert (parent.state_json,parent.state_digest)==saved
            returns.require_available(db,draft['operator_address'],str(int(original['nonce'])+1))
        assert rpc('eth_getTransactionReceipt',[original_hash]) is None and rpc('eth_getTransactionReceipt',[first_hash]) is None
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',now()-timedelta(days=1),amount=Decimal('1')))
        resolution_id=settled['resolution_id']
        with vault.session() as db:preview=accounting.preview_booking(db,'NATIVE_RESOLUTION',resolution_id,context_account_id=account,wallet_address=meta['owner'])
        with vault.session() as db:
            booked=accounting.book_receipt(db,'NATIVE_RESOLUTION',resolution_id,context_account_id=account,wallet_address=meta['owner'],expected_revision=preview['revision'],preview_digest=preview['preview_digest'],now=now())
            assert booked['events_created']==1 and account_snapshot(db,account).cash['BNB']==Decimal('0.9999685')
        vault.backup(tmp_path/'cancelled.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.view_cancellation(db,second['id'],now=now())['winner_id']==second['id']


@pytest.mark.parametrize('stage',['PREPARED','RESERVED','SIGNED','BROADCAST_ATTEMPTED'])
def test_original_can_win_at_every_stage_without_changing_its_execution_effect(tmp_path,stage):
    with pending_return(tmp_path) as (values,original,tx_hash,sign):
        vault,account,manager,draft,meta,rpc,_,_=values
        review=cancellation_review(values,original);plan=cancellation_plan(values,review)
        if stage!='PREPARED':
            with vault.session() as db:journal.reserve_cancellation(db,review['id'],account_id=account,wallet_address=meta['owner'],draft_id=draft['id'],drafts=manager,plan=plan,plan_digest=review['plan_digest'],now=now())
        if stage in {'SIGNED','BROADCAST_ATTEMPTED'}:
            signed=sign(plan.transaction());cancel_hash='0x'+signed.hash.hex()
            with vault.session() as db:journal.record_signature(db,review['id'],account_id=account,plan=plan,raw=bytes(signed.raw_transaction),tx_hash=cancel_hash,now=now())
        if stage=='BROADCAST_ATTEMPTED':
            with vault.session() as db:journal.mark_broadcast(db,review['id'],account_id=account,plan_digest=review['plan_digest'],tx_hash=cancel_hash,now=now())
        rpc('miner_start',[1]);rpc('evm_mine',[]);evidence=collect_resolution(rpc,tx_hash)
        with vault.session() as db:
            settled=journal.observe_cancellation(db,review['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
            assert settled['winner']=='ORIGINAL' and settled['resolution_id'] is None and int(settled['receipt']['transferred_wei'])>0
            assert settled['receipt']['gas_fee_wei']=='21000000000000'
            assert returns.view_return(db,original['id'],now=now())['status']=='FINALIZED'
            returns.require_available(db,draft['operator_address'],str(int(original['nonce'])+1))
        with pytest.raises(ValueError):
            with vault.session() as db:journal.mark_broadcast(db,review['id'],account_id=account,plan_digest=review['plan_digest'],tx_hash='0x'+'ab'*32,now=now())
        vault.backup(tmp_path/'original-wins.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.view_cancellation(db,review['id'],now=now())['winner']=='ORIGINAL'


def test_competing_reviews_reserve_only_one_and_abandoning_unsent_does_not_release_original_nonce(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with pending_return(tmp_path) as (values,original,_,_):
        vault,account,manager,draft,meta,_,_,_=values
        reviews=[cancellation_review(values,original) for _ in range(2)]
        plans=[cancellation_plan(values,review) for review in reviews]
        def reserve(index):
            try:
                with vault.session() as db:return journal.reserve_cancellation(db,reviews[index]['id'],account_id=account,wallet_address=meta['owner'],draft_id=draft['id'],drafts=manager,plan=plans[index],plan_digest=reviews[index]['plan_digest'],now=now())
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,range(2)))
        assert sum(result is not None for result in results)==1
        reserved=next(result for result in results if result is not None)
        with vault.session() as db:journal.abandon_unsent(db,reserved['id'],account_id=account,now=now())
        with pytest.raises(ValueError,match='pendiente'):
            with vault.session() as db:returns.require_available(db,draft['operator_address'],str(int(original['nonce'])+1))
        other=next(index for index in range(2) if reviews[index]['id']!=reserved['id'])
        assert reserve(other)['reserved'];vault.backup(tmp_path/'one-reservation.db')


@pytest.mark.parametrize('part',['plan','state'])
def test_rehashed_cancellation_forgery_is_rejected_by_reads_and_encrypted_backup(tmp_path,part):
    from app.portfolio.models import RealDexGasCancellationIntent
    from app.portfolio.wallet_funding import _decoded,_encoded
    from app.portfolio.vault import VaultError
    with pending_return(tmp_path) as (values,original,_,_):
        vault,_,_,_,meta,_,_,_=values;review=cancellation_review(values,original)
        with vault.session() as db:
            row=db.get(RealDexGasCancellationIntent,review['id'])
            assert meta['owner'].lower() not in (row.payload_json+row.state_json).lower()
            if part=='plan':
                payload=_decoded(row.payload_json,row.payload_digest);payload['gas_price']='1300000000';row.payload_json,row.payload_digest=_encoded(payload)
            else:
                state=_decoded(row.state_json,row.state_digest);state['history'][0]['at']=(row.created_at-timedelta(seconds=1)).isoformat();row.state_json,row.state_digest=_encoded(state)
            db.commit()
        with pytest.raises(ValueError):
            with vault.session() as db:journal.view_cancellation(db,review['id'],now=now())
        with pytest.raises(VaultError):vault.backup(tmp_path/'forged-cancellation.db')


def test_schema_68_migration_preserves_pending_original_byte_for_byte(tmp_path):
    from app.portfolio.vault import SCHEMA_VERSION
    with pending_return(tmp_path) as (values,original,_,_):
        vault,_,_,_,_,_,_,_=values
        with vault.session() as db:
            parent=returns.return_record(db,original['id'])[0];saved=(parent.payload_json,parent.payload_digest,parent.state_json,parent.state_digest)
            db.connection().exec_driver_sql('DROP TABLE real_dex_gas_cancellation_intents')
            db.connection().exec_driver_sql('PRAGMA user_version=68');db.commit()
        vault.lock();vault.unlock(PASSWORD);assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            parent=returns.return_record(db,original['id'])[0]
            assert (parent.payload_json,parent.payload_digest,parent.state_json,parent.state_digest)==saved
        vault.backup(tmp_path/'migrated-pending.db')
