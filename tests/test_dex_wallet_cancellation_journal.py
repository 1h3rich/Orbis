"""Durable one-shot wallet mandates, sealed recovery and actual own-EVM fees."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
from app.live_execution.dex_native_resolution import collect_resolution
from app.portfolio import dex_wallet_cancellation as journal,dex_gas_funding as funding,dex_receipt_booking as booking
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from app.portfolio.repository import append_event,account_snapshot
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.domain import EventInput
from tests.dex_wallet_cancellation_fixture import pending_funding,review,plan,state
from tests.test_dex_gas_funding import prepare,claim,now
from tests.test_dex_deployments import PASSWORD


def grant(values,source,tx_hash,record):
    vault,account,_,_,meta,_,_,_=values
    current=plan(values,source,tx_hash,record)
    with vault.session() as db:
        result=journal.claim_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],plan=current,
            plan_digest=record['plan_digest'],now=now())
    return result,current


def test_wallet_cancel_claim_is_unique_and_winning_fee_survives_reopen_without_mutating_source(tmp_path):
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,draft,meta,rpc,_,_=values;record=review(values,source,tx_hash)
        with vault.session() as db:
            row=funding.funding_record(db,source['id'])[0];saved=(row.payload_json,row.payload_digest,row.state_json,row.state_digest)
        current=plan(values,source,tx_hash,record)
        def contender(_):
            with vault.session() as db:return journal.claim_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],
                plan=current,plan_digest=record['plan_digest'],now=now())
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(contender,range(2)))
        assert sum(result['claim_granted'] for result in results)==1
        with pytest.raises(ValueError):
            with vault.session() as db:assert_wallet_nonce_available(db,meta['owner'],str(int(source['nonce'])+1))
        cancel_hash=rpc('eth_sendTransaction',[current.transaction()]);rpc('miner_start',[1]);rpc('evm_mine',[])
        evidence=collect_resolution(rpc,cancel_hash)
        # Lost hash-report reply: verified receipt can link the sole claimed
        # current review atomically, without claiming or asking wallet again.
        with vault.session() as db:
            done=journal.observe_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
            assert done['winner']=='CANCELLATION' and done['winner_id']==record['id']
            row=funding.funding_record(db,source['id'])[0]
            assert (row.payload_json,row.payload_digest,row.state_json,row.state_digest)==saved
            child=journal.cancellation_record(db,record['id'])[0]
            for secret in (meta['owner'],draft['operator_address'],tx_hash,cancel_hash):
                assert secret.lower() not in (child.payload_json+child.state_json).lower()
            assert_wallet_nonce_available(db,meta['owner'],str(int(source['nonce'])+1))
            with pytest.raises(ValueError):assert_wallet_nonce_available(db,meta['owner'],source['nonce'])
        assert rpc('eth_getTransactionReceipt',[tx_hash]) is None
        assert rpc('eth_getBalance',[draft['operator_address'],'latest'])=='0x0'
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',now()-timedelta(days=1),amount=Decimal('1')))
        with vault.session() as db:preview=booking.preview_booking(db,'NATIVE_RESOLUTION',done['resolution_id'],context_account_id=account,wallet_address=meta['owner'])
        with vault.session() as db:
            booked=booking.book_receipt(db,'NATIVE_RESOLUTION',done['resolution_id'],context_account_id=account,wallet_address=meta['owner'],
                expected_revision=preview['revision'],preview_digest=preview['preview_digest'],now=now())
            assert booked['events_created']==1 and account_snapshot(db,account).cash['BNB']==Decimal('0.9999748')
        fresh=prepare(values);assert int(fresh['nonce'])==int(source['nonce'])+1 and claim(values,fresh)['claim_granted']
        vault.backup(tmp_path/'wallet-cancel.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.view_cancellation(db,record['id'],now=now())['winner_id']==record['id']


@pytest.mark.parametrize('claimed',[False,True])
def test_original_wins_and_cancel_never_replaces_its_real_effect(tmp_path,claimed):
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,draft,meta,rpc,_,_=values;record=review(values,source,tx_hash)
        if claimed:grant(values,source,tx_hash,record)
        rpc('miner_start',[1]);rpc('evm_mine',[]);evidence=collect_resolution(rpc,tx_hash)
        with vault.session() as db:
            done=journal.observe_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
            assert done['winner']=='ORIGINAL' and done['receipt']['transferred_wei']==source['amount_wei']
        vault.backup(tmp_path/'original.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.view_cancellation(db,record['id'],now=now())['winner']=='ORIGINAL'


def test_unknown_original_and_unknown_prior_wallet_request_need_new_explicit_higher_review(tmp_path):
    with pending_funding(tmp_path,arrived=False) as (values,source,tx_hash):
        vault,account,_,_,meta,rpc,_,_=values
        with pytest.raises(ValueError):review(values,source,tx_hash)
        first=review(values,source,tx_hash,accept_unknown=True);grant(values,source,tx_hash,first)
        with pytest.raises(ValueError):review(values,source,tx_hash,price=1250000000,accept_unknown=True)
        second=review(values,source,tx_hash,price=1500000000,accept_unknown=True)
        assert second['previous_fees_unknown'] and second['previous_prices']==[1200000000]
        result,current=grant(values,source,tx_hash,second);assert result['claim_granted']
        cancel_hash=rpc('eth_sendTransaction',[current.transaction()]);rpc('miner_start',[1]);rpc('evm_mine',[])
        with vault.session() as db:journal.report_hash(db,second['id'],account_id=account,tx_hash=cancel_hash,now=now())
        evidence=collect_resolution(rpc,cancel_hash)
        with vault.session() as db:assert journal.observe_cancellation(db,second['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())['winner_id']==second['id']
        with vault.session() as db:assert journal.settle_known(db,first['id'],account_id=account,now=now())['winner_id']==second['id']
        vault.backup(tmp_path/'unknown.db')


def test_cancellation_review_withdrawal_rejection_and_stale_claim_never_release_original(tmp_path):
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,_,meta,_,_,_=values
        expired=review(values,source,tx_hash);current=plan(values,source,tx_hash,expired)
        with pytest.raises(ValueError):
            with vault.session() as db:journal.claim_cancellation(db,expired['id'],account_id=account,wallet_address=meta['owner'],plan=current,plan_digest=expired['plan_digest'],now=now()+timedelta(seconds=121))
        with vault.session() as db:journal.cancel_review(db,expired['id'],account_id=account,now=now())
        rejected=review(values,source,tx_hash);grant(values,source,tx_hash,rejected)
        with vault.session() as db:assert journal.report_rejected(db,rejected['id'],account_id=account,now=now())['status']=='WALLET_REJECTED'
        with pytest.raises(ValueError):
            with vault.session() as db:assert_wallet_nonce_available(db,meta['owner'],str(int(source['nonce'])+1))
        vault.backup(tmp_path/'rejected.db')


@pytest.mark.parametrize('part',['plan','state'])
def test_rehashed_wallet_cancel_forgery_cannot_enter_backup(tmp_path,part):
    from app.portfolio.models import RealDexWalletCancellationIntent
    from app.portfolio.wallet_funding import _decoded,_encoded
    from app.portfolio.vault import VaultError
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,_,_,_,_,_,_,_=values;record=review(values,source,tx_hash)
        with vault.session() as db:
            row=db.get(RealDexWalletCancellationIntent,record['id'])
            if part=='plan':
                body=_decoded(row.payload_json,row.payload_digest);body['gas_price']='1500000000';row.payload_json,row.payload_digest=_encoded(body)
            else:
                body=_decoded(row.state_json,row.state_digest);body['history'][0]['at']=(row.created_at-timedelta(seconds=1)).isoformat();row.state_json,row.state_digest=_encoded(body)
            db.commit()
        with pytest.raises(ValueError):
            with vault.session() as db:journal.view_cancellation(db,record['id'],now=now())
        with pytest.raises(VaultError):vault.backup(tmp_path/'forged.db')


def test_shared_nonce_guard_blocks_unknown_cancel_even_when_original_hash_was_reported(tmp_path):
    from app.portfolio.wallet_nonce_guard import assert_funding_cancellation_available
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,draft,meta,_,_,_=values;next_nonce=str(int(source['nonce'])+1)
        with vault.session() as db:funding.report_funding(db,source['id'],account_id=account,tx_hash=tx_hash,now=now())
        with vault.session() as db:assert_wallet_nonce_available(db,meta['owner'],next_nonce)
        record=review(values,source,tx_hash);grant(values,source,tx_hash,record)
        with pytest.raises(ValueError,match='cancelación'):
            with vault.session() as db:assert_wallet_nonce_available(db,meta['owner'],next_nonce)
        for wallet,nonce in ((meta['owner'],next_nonce),(draft['operator_address'],source['nonce'])):
            with pytest.raises(ValueError):
                with vault.session() as db:assert_funding_cancellation_available(db,wallet,nonce,source['id'])
        vault.backup(tmp_path/'nonce-guard.db')


@pytest.mark.parametrize('typed',[False,True])
def test_wallet_fee_or_type_override_requires_explicit_native_review_and_remains_external(tmp_path,typed):
    from app.portfolio import dex_native_resolution as native
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,_,meta,rpc,_,_=values;record=review(values,source,tx_hash)
        _,current=grant(values,source,tx_hash,record);tx=current.transaction()
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(1500000000),maxPriorityFeePerGas=hex(1500000000))
        else:tx['gasPrice']=hex(1500000000)
        changed_hash=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        with vault.session() as db:journal.report_hash(db,record['id'],account_id=account,tx_hash=changed_hash,now=now())
        evidence=collect_resolution(rpc,changed_hash)
        with pytest.raises(ValueError,match='modificó'):
            with vault.session() as db:journal.observe_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
        with vault.session() as db:assert native.status(db,'GAS_FUNDING',source['id']) is None
        scope=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,accept_changes=True,now=now())
        with vault.session() as db:preview=native.preview_resolution(db,'GAS_FUNDING',source['id'],**scope)
        with vault.session() as db:native.confirm_resolution(db,'GAS_FUNDING',source['id'],preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],**scope)
        with vault.session() as db:
            settled=journal.settle_known(db,record['id'],account_id=account,now=now())
            assert settled['winner']=='EXTERNAL' and settled['winner_id'] is None and settled['receipt']['gas_fee_wei']=='31500000000000'
        vault.backup(tmp_path/'changed-fee.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.view_cancellation(db,record['id'],now=now())['winner']=='EXTERNAL'


@pytest.mark.parametrize('winner',['ORIGINAL','CANCELLATION'])
def test_receipt_and_child_settlement_roll_back_together_then_recover_once(tmp_path,monkeypatch,winner):
    from app.portfolio import dex_native_resolution as native
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,_,meta,rpc,_,_=values;record=review(values,source,tx_hash)
        _,current=grant(values,source,tx_hash,record)
        with vault.session() as db:
            parent=funding.funding_record(db,source['id'])[0];saved=(parent.state_json,parent.state_digest)
        if winner=='CANCELLATION':tx_hash=rpc('eth_sendTransaction',[current.transaction()])
        rpc('miner_start',[1]);rpc('evm_mine',[]);evidence=collect_resolution(rpc,tx_hash)
        advance=journal._advance
        def fail(db,row,terms,state,status,at,**kwargs):
            if status=='SETTLED':raise ValueError('Controlled settlement storage fault')
            return advance(db,row,terms,state,status,at,**kwargs)
        monkeypatch.setattr(journal,'_advance',fail)
        with pytest.raises(ValueError,match='storage fault'):
            with vault.session() as db:journal.observe_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
        with vault.session() as db:
            assert native.status(db,'GAS_FUNDING',source['id']) is None
            parent=funding.funding_record(db,source['id'])[0];assert (parent.state_json,parent.state_digest)==saved
            assert journal.view_cancellation(db,record['id'],now=now())['status']=='WALLET_REQUESTED'
        monkeypatch.setattr(journal,'_advance',advance)
        with vault.session() as db:assert journal.observe_cancellation(db,record['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())['winner']==winner
        vault.backup(tmp_path/'atomic.db')


def test_schema_69_migration_keeps_pending_funding_bytes_and_preimage(tmp_path):
    from app.portfolio.vault import SCHEMA_VERSION
    with pending_funding(tmp_path) as (values,source,_):
        vault,_,_,_,_,_,_,_=values
        with vault.session() as db:
            row=funding.funding_record(db,source['id'])[0];saved=(row.payload_json,row.payload_digest,row.state_json,row.state_digest)
            db.connection().exec_driver_sql('DROP TABLE real_dex_wallet_cancellation_intents')
            db.connection().exec_driver_sql('PRAGMA user_version=69');db.commit()
        vault.lock();vault.unlock(PASSWORD);assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            row=funding.funding_record(db,source['id'])[0];assert (row.payload_json,row.payload_digest,row.state_json,row.state_digest)==saved
        vault.backup(tmp_path/'migrated.db')


def test_review_cannot_move_back_before_original_funding_anchor_or_switch_its_block(tmp_path):
    from dataclasses import replace
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,_,meta,_,_,_=values;observed=state(values,source,tx_hash);anchor=observed.snapshot
        variants=[replace(anchor,block_number=anchor.block_number-1,latest_number=anchor.block_number-1),
            replace(anchor,block_hash='0x'+'ab'*32,latest_hash='0x'+'ab'*32)]
        for changed in variants:
            with pytest.raises(ValueError):
                with vault.session() as db:journal.prepare_cancellation(db,identifier=str(uuid4()),funding_id=source['id'],account_id=account,
                    wallet_address=meta['owner'],snapshot=replace(observed,snapshot=changed),gas_price=1200000000,bump_percent=10,
                    max_gas_wei=10**14,accept_unknown_fee=False,now=now())


def test_prepare_replay_is_only_a_read_and_rejects_changed_or_noninteger_terms(tmp_path):
    with pending_funding(tmp_path) as (values,source,tx_hash):
        vault,account,_,_,meta,_,_,_=values;record=review(values,source,tx_hash)
        args=dict(identifier=record['id'],funding_id=source['id'],account_id=account,wallet_address=meta['owner'],
            snapshot=None,gas_price=record['gas_price'],bump_percent=record['bump_percent'],max_gas_wei=int(record['max_gas_wei']),
            accept_unknown_fee=False,now=now()+timedelta(seconds=121))
        with vault.session() as db:
            replay=journal.prepare_cancellation(db,**args)
            assert replay['replayed'] and replay['expired'] and replay['status']=='PREPARED' and 'transaction' not in replay
        for changed in ({'gas_price':record['gas_price']+1},{'bump_percent':10.0},{'gas_price':True},{'accept_unknown_fee':0}):
            with pytest.raises(ValueError):
                with vault.session() as db:journal.prepare_cancellation(db,**{**args,**changed})
