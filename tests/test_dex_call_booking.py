"""Resolved signed calls become exact local events on owned EVM/vaults only."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.live_execution.dex_call_resolution import collect_call_resolution
from app.portfolio import dex_call_resolution as resolutions,dex_receipt_booking as booking
from app.portfolio import wallet_swap as swaps,wallet_swap_booking as original_booking
from app.portfolio.domain import EventInput,PortfolioError
from app.portfolio.models import RealDexReceiptBooking
from app.portfolio.repository import create_account,append_event,account_snapshot,list_events
from app.portfolio.wallet_funding import _encoded,_decoded
from app.portfolio.wallet_links import save_wallet_link
from tests.dex_evm_fixture import evm
from tests.test_dex_call_resolution import prepare,now
from tests.test_dex_operator import write
from tests.test_wallet_swap import setup,PASSWORD


@contextmanager
def resolved_case(directory,mode='BUY',*,typed=False):
    vault,account,_=setup(directory)
    try:
        with evm(directory/'evm',backend='anvil') as (meta,rpc):
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
            for token in ('usdt','wbnb'):
                for recipient in (meta['owner'],meta['pool']):
                    write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**18])
                write(rpc,{**meta,'operator':meta['owner']},meta[token],'approve(address,uint256)',
                    ['address','uint256'],[meta['router'],10**18])
            action=mode if mode in {'APPROVE','REVOKE'} else 'SWAP'
            plan,source,before=prepare(vault,account,meta,rpc,action=action,token='WBNB' if mode=='SELL' else 'USDT')
            rpc('miner_stop',[]);old_hash=rpc('eth_sendTransaction',[plan['transaction']])
            tx={**plan['transaction'],'gasPrice':hex(2*10**9)}
            if typed:
                tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(2*10**9),maxPriorityFeePerGas=hex(10**9))
            if mode=='CANCELLED':tx.update(to=meta['owner'],data='0x',value='0x0',gas='0x5208')
            if mode=='REVERTED':tx.update(data='0xdeadbeef',gas=hex(60000))
            sent=rpc('eth_sendTransaction',[tx]);rpc('evm_mine',[]);rpc('miner_start',[1])
            assert rpc('eth_getTransactionReceipt',[old_hash]) is None
            evidence=collect_call_resolution(rpc,source,sent)
            scope=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,accept_changes=True,now=now())
            with vault.session() as db:proposed=resolutions.preview_resolution(db,'WALLET_SWAP',source.id,**scope)
            with vault.session() as db:
                result=resolutions.confirm_resolution(db,'WALLET_SWAP',source.id,preview_digest=proposed['preview_digest'],
                    source_state_digest=proposed['source_state_digest'],**scope)
            with vault.session() as db:
                owner=create_account(db,'Wallet propia','USDT').id
                save_wallet_link(db,owner,meta['owner'],'0x38','Principal');db.commit()
            for item in (EventInput('CONTRIBUTION','USDT',now()-timedelta(days=1),amount=Decimal('100')),
                    EventInput('CONTRIBUTION','BNB',now()-timedelta(days=1),amount=Decimal('0.01')),
                    EventInput('BUY','USDT',now()-timedelta(hours=1),asset='WBNB',quantity=Decimal('1'),unit_price=Decimal('2'))):
                with vault.session() as db:append_event(db,owner,str(uuid4()),item)
            yield vault,account,owner,meta,rpc,source,before,result
    finally:vault.lock()


def preview(case,**selection):
    vault,account,_,meta,_,_,_,resolved=case
    with vault.session() as db:return booking.preview_booking(db,'CALL_RESOLUTION',resolved['id'],
        context_account_id=account,wallet_address=meta['owner'],**selection)


def book(case,proposal,**selection):
    vault,account,_,meta,_,_,_,resolved=case
    with vault.session() as db:return booking.book_receipt(db,'CALL_RESOLUTION',resolved['id'],context_account_id=account,
        wallet_address=meta['owner'],expected_revision=proposal['revision'],preview_digest=proposal['preview_digest'],now=now(),**selection)


@pytest.mark.parametrize('mode',['BUY','SELL','APPROVE','REVOKE','CANCELLED','REVERTED'])
def test_actual_resolved_calls_book_trade_only_when_executed_and_gas_once_across_reopen(tmp_path,mode):
    with resolved_case(tmp_path,mode) as case:
        vault,account,owner,meta,_,source,before,resolved=case
        proposal=preview(case);is_trade=mode in {'BUY','SELL'}
        assert proposal['contract']=='DEX_RECEIPT_BOOKING_V4' and proposal['target_account_id']==owner
        assert proposal['economic_outcome']==resolved['outcome']
        assert (proposal['trade'] is not None)==is_trade
        assert proposal['internal_principal_bnb']=='0' and proposal['principal_creates_event'] is False
        if is_trade:
            assert proposal['trade']['kind']==mode
            assert proposal['trade']['gross_amount']=='0.01' and proposal['trade']['quantity']=='0.01'
        initial=book(case,proposal);assert initial['booked'] and initial['events_created']==(2 if is_trade else 1)
        assert book(case,proposal)['events_created']==0
        with vault.session() as db:
            values=booking.list_receipts(db,account_id=account,wallet_address=meta['owner'])
            assert len(values)==1 and values[0]['source_kind']=='CALL_RESOLUTION' and values[0]['booked']
            snap=account_snapshot(db,owner)
            assert snap.cash['BNB']==Decimal('0.01')-Decimal(resolved['receipt']['gas_fee_wei'])/10**18
            assert snap.positions[('WBNB','USDT')].quantity==Decimal('1')+(Decimal('0.01') if mode=='BUY' else -Decimal('0.01') if mode=='SELL' else Decimal('0'))
            assert snap.contributions=={'USDT':Decimal('100'),'BNB':Decimal('0.01')} and snap.withdrawals=={}
            assert list_events(db,account)==[] and len(list_events(db,owner))==3+(2 if is_trade else 1)
            parent,_,_=swaps.swap_record(db,source.id)
            assert (parent.payload_json,parent.state_json,parent.updated_at)==before
            with pytest.raises(PortfolioError):original_booking.preview_booking(db,source.id,context_account_id=account,wallet_address=meta['owner'])
            rows=[(r.id,r.payload_json,r.payload_digest) for r in db.scalars(select(RealDexReceiptBooking))]
            for value in (meta['owner'],meta['operator']):assert value.lower() not in str(rows).lower()
        vault.backup(tmp_path/'booked.db');vault.lock();vault.unlock(PASSWORD)
        assert book(case,proposal)['events_created']==0
        with vault.session() as db:
            assert rows==[(r.id,r.payload_json,r.payload_digest) for r in db.scalars(select(RealDexReceiptBooking))]


def test_cancel_links_an_existing_gas_expense_without_a_trade_or_duplicate(tmp_path):
    with resolved_case(tmp_path,'CANCELLED') as case:
        vault,_,owner,_,_,_,_,resolved=case
        at=now().fromisoformat(resolved['receipt']['block_at'])
        amount=Decimal(resolved['receipt']['gas_fee_wei'])/10**18
        with vault.session() as db:existing=append_event(db,owner,str(uuid4()),EventInput('FEE','BNB',at,amount=amount,note='Red BNB'))
        assert not preview(case)['available']
        selected=preview(case,gas_event_ids=[existing.id]);assert selected['available'] and selected['trade'] is None
        assert book(case,selected,gas_event_ids=[existing.id])['events_created']==0
        with vault.session() as db:assert len(list_events(db,owner))==4


def test_stale_revision_rollback_and_rehashed_booking_tamper_are_rejected(tmp_path,monkeypatch):
    with resolved_case(tmp_path) as case:
        vault,_,owner,_,_,_,_,_=case
        proposed=preview(case)
        with vault.session() as db:append_event(db,owner,str(uuid4()),EventInput('CONTRIBUTION','USDT',now()-timedelta(hours=2),amount=Decimal('1')))
        with pytest.raises(PortfolioError,match='cambió'):book(case,proposed)
        proposed=preview(case);append=booking.append_event_in_transaction;calls=[]
        def fail_second(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('injected second event failure')
            return append(*args,**kwargs)
        monkeypatch.setattr(booking,'append_event_in_transaction',fail_second)
        with pytest.raises(RuntimeError,match='injected'):book(case,proposed)
        with vault.session() as db:
            assert len(list_events(db,owner))==4 and not list(db.scalars(select(RealDexReceiptBooking)))
        monkeypatch.setattr(booking,'append_event_in_transaction',append)
        result=book(case,proposed)
        with vault.session() as db:
            row=db.get(RealDexReceiptBooking,result['booking_id']);payload=_decoded(row.payload_json,row.payload_digest)
            payload['contract']='DEX_RECEIPT_BOOKING_V1';row.payload_json,row.payload_digest=_encoded(payload);db.commit()
        with pytest.raises(PortfolioError):
            with vault.session() as db:booking.booking_record(db,result['booking_id'])


def test_later_original_wallet_swap_must_follow_the_earlier_resolved_trade(tmp_path):
    with resolved_case(tmp_path) as case:
        vault,account,owner,meta,rpc,_,_,_=case
        plan,source,_=prepare(vault,account,meta,rpc)
        sent=rpc('eth_sendTransaction',[plan['transaction']]);evidence=collect_call_resolution(rpc,source,sent)
        with vault.session() as db:swaps.report_swap(db,source.id,account_id=account,tx_hash=sent,now=now())
        legacy={key:evidence[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
        with vault.session() as db:swaps.check_swap(db,source.id,account_id=account,evidence=legacy,now=now())
        def later():
            with vault.session() as db:return original_booking.preview_booking(db,source.id,context_account_id=account,wallet_address=meta['owner'])
        with pytest.raises(PortfolioError,match='anterior'):later()
        book(case,preview(case));proposal=later()
        with vault.session() as db:
            result=original_booking.book_swap(db,source.id,context_account_id=account,wallet_address=meta['owner'],
                expected_revision=proposal['revision'],preview_digest=proposal['preview_digest'],now=now())
            assert result['booked']
        with vault.session() as db:assert account_snapshot(db,owner).positions[('WBNB','USDT')].quantity==Decimal('1.02')


def test_chain_order_uses_verified_index_or_same_signer_nonce_and_rejects_ambiguity(tmp_path):
    # Counterfactual pure comparisons on one authenticated source. No modified
    # source is ever accepted or written as economic evidence.
    from app.portfolio.dex_call_booking import _compare
    with resolved_case(tmp_path) as case:
        vault,_,_,_,_,_,_,resolved=case
        with vault.session() as db:source=booking.source_record(db,'CALL_RESOLUTION',resolved['id'])
        later=replace(source,id=str(uuid4()),signer_digest='f'*64,transaction_index=source.transaction_index+1)
        assert _compare(source,later)<0 and _compare(later,source)>0
        with pytest.raises(PortfolioError,match='orden'):_compare(source,replace(later,transaction_index=None))
        same=replace(later,signer_digest=source.signer_digest,nonce=source.nonce+1,transaction_index=None)
        assert _compare(source,same)<0
        with pytest.raises(PortfolioError,match='índice'):_compare(source,replace(later,transaction_index=source.transaction_index))
        wrong=replace(later,receipt={**later.receipt,'block_hash':'0x'+'f'*64})
        with pytest.raises(PortfolioError,match='hash'):_compare(source,wrong)


@pytest.mark.parametrize('mode',['BUY','CANCELLED'])
def test_private_http_booking_requires_owner_proof_and_recovers_a_lost_reply_without_rpc(tmp_path,mode):
    from concurrent.futures import ThreadPoolExecutor
    from eth_account import Account
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.portfolio.api import create_real_router
    from tests.test_wallet_funding_api import prove
    with resolved_case(tmp_path,mode) as case:
        vault,account,owner,meta,_,_,_,resolved=case
        vault.lock()
        def forbidden_rpc():raise AssertionError('Accounting requested a network/signer')
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=forbidden_rpc))
        with TestClient(app) as client:
            path=f'/real/accounts/{account}/dex-receipts';url=path+'/CALL_RESOLUTION/'+resolved['id']
            body={'address':meta['owner']}
            assert client.post(path+'/query',json=body).status_code==401
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            assert client.post(url+'/booking-preview',headers=headers,json=body).status_code==409
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            assert client.post(url+'/booking-preview',headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
            assert client.post(url+'/booking-preview',headers=headers,json={**body,'raw_transaction':'0x'}).status_code==422
            response=client.post(url+'/booking-preview',headers=headers,json=body)
            assert response.status_code==200 and response.headers['cache-control']=='no-store',response.text
            proposal=response.json();assert (proposal['trade'] is not None)==(mode=='BUY')
            command={**body,'expected_revision':proposal['revision'],'preview_digest':proposal['preview_digest'],
                'confirmation':'CONTABILIZAR_RECIBO_DEX'}
            assert client.post(url+'/book',headers=headers,json={**command,'preview_digest':'f'*64}).status_code==409
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:client.post(url+'/book',headers=headers,json=command),range(2)))
            assert all(result.status_code==200 for result in results),[result.text for result in results]
            assert sorted(result.json()['events_created'] for result in results)==[0,2 if mode=='BUY' else 1]
            rows=client.post(path+'/query',headers=headers,json=body).json()['items']
            assert len(rows)==1 and rows[0]['booked']
            assert client.post('/real/vault/backup',headers=headers).status_code==200
            assert client.post('/real/vault/lock',headers=headers).status_code==200
            assert client.post(url+'/book',headers=headers,json=command).status_code==401
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            assert client.post(url+'/book',headers=headers,json=command).status_code==409
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            assert client.post(url+'/book',headers=headers,json=command).json()['events_created']==0
            events=client.get(f'/real/accounts/{owner}/events',headers=headers).json()
            assert len(events)==3+(2 if mode=='BUY' else 1)
            assert client.post('/real/vault/lock',headers=headers).status_code==200
