"""Wallet cancellation reviews use signed fees, never inferred draft fees."""
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from copy import deepcopy
import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_wallet_cancellation import read_wallet_cancellation_state,plan_wallet_cancellation
from tests.dex_evm_fixture import evm


def now():return datetime.now(timezone.utc)


def original(rpc,meta,*,typed=False):
    nonce=int(rpc('eth_getTransactionCount',[meta['owner'],'latest']),16);rpc('miner_stop',[])
    tx={'from':meta['owner'],'to':meta['operator'],'nonce':hex(nonce),'gas':'0x5208','chainId':'0x38',
        'value':hex(5*10**14),'data':'0x'}
    if typed:tx.update(type='0x2',maxFeePerGas=hex(2*10**9),maxPriorityFeePerGas=hex(5*10**8))
    else:tx.update(type='0x0',gasPrice=hex(1500000000))
    return nonce,rpc('eth_sendTransaction',[tx])


@pytest.mark.parametrize('typed',[False,True])
def test_wallet_cancellation_uses_actual_signed_fee_cap_and_replaces_only_original_nonce(tmp_path,typed):
    with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
        nonce,tx_hash=original(rpc,meta,typed=typed)
        snapshot=read_wallet_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,amount_wei=5*10**14,original_hash=tx_hash,now=now())
        cap=2*10**9 if typed else 1500000000
        assert snapshot.original_fee.fee_cap==cap and snapshot.original_fee.transaction_type==(2 if typed else 0)
        assert snapshot.original_fee.priority_fee==(5*10**8 if typed else None)
        args=dict(gas_price=cap*12//10,bump_percent=10,max_gas_wei=10**14,accept_unknown_fee=False,now=now())
        plan=plan_wallet_cancellation(snapshot,**args)
        assert plan.minimum_price==cap*11//10 and not plan.original_fee_unknown
        with pytest.raises(DexExecutorError):plan_wallet_cancellation(snapshot,**{**args,'gas_price':cap*11//10-1})
        tx=plan.transaction();assert tx['from']==tx['to']==meta['owner'].lower() and tx['value']=='0x0' and tx['data']=='0x'
        assert tx['nonce']==hex(nonce) and tx['gas']=='0x5208' and tx['chainId']=='0x38'
        before=[int(rpc('eth_getBalance',[address,'latest']),16) for address in (meta['owner'],meta['operator'])]
        cancel_hash=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        receipt=rpc('eth_getTransactionReceipt',[cancel_hash]);assert receipt['status']=='0x1' and receipt['gasUsed']=='0x5208'
        assert rpc('eth_getTransactionReceipt',[tx_hash]) is None
        assert int(rpc('eth_getBalance',[meta['owner'],'latest']),16)==before[0]-21000*args['gas_price']
        assert int(rpc('eth_getBalance',[meta['operator'],'latest']),16)==before[1]
        with pytest.raises(DexExecutorError):read_wallet_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,amount_wei=5*10**14,original_hash=tx_hash,now=now())


@pytest.mark.parametrize('hash_value',[None,'0x'+'ab'*32])
def test_missing_original_fee_is_explicit_uncertainty_and_never_inferred_from_draft(tmp_path,hash_value):
    with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
        nonce=int(rpc('eth_getTransactionCount',[meta['owner'],'latest']),16)
        state=read_wallet_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,amount_wei=5*10**14,original_hash=hash_value,now=now())
        assert state.original_fee is None
        args=dict(gas_price=1200000000,bump_percent=10,max_gas_wei=10**14,now=now())
        for accepted in (False,1,'true'):
            with pytest.raises(DexExecutorError):plan_wallet_cancellation(state,accept_unknown_fee=accepted,**args)
        plan=plan_wallet_cancellation(state,accept_unknown_fee=True,**args)
        assert plan.original_fee_unknown and plan.minimum_price is None
        assert plan.transaction()['nonce']==hex(nonce) and plan.transaction()['to']==meta['owner'].lower()
        with pytest.raises(DexExecutorError):plan_wallet_cancellation(state,accept_unknown_fee=True,**{**args,'max_gas_wei':1})
        with pytest.raises(DexExecutorError):plan.validate(now()+timedelta(seconds=16))


def test_pending_funding_signed_scope_and_shape_cannot_be_forged_or_reclassified_as_unknown(tmp_path):
    with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
        nonce,tx_hash=original(rpc,meta,typed=True);actual=rpc('eth_getTransactionByHash',[tx_hash])
        for field,value in (('from',meta['operator']),('to',meta['owner']),('nonce',hex(nonce+1)),('value','0x1'),
                ('hash','0x'+'ab'*32),('maxFeePerGas',hex(10**9)),('input','0x01'),('blockNumber','0x1')):
            bad=deepcopy(actual);bad[field]=value
            def altered(method,params):return bad if method=='eth_getTransactionByHash' else rpc(method,params)
            with pytest.raises(DexExecutorError):read_wallet_cancellation_state(altered,meta['owner'],meta['operator'],nonce=nonce,amount_wei=5*10**14,original_hash=tx_hash,now=now())


def test_original_winning_during_read_prevents_review_and_reads_never_emit(tmp_path):
    with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
        nonce,tx_hash=original(rpc,meta);reads=[];lookups=0
        def raced(method,params):
            nonlocal lookups
            reads.append(method)
            if method=='eth_getTransactionByHash':
                lookups+=1
                if lookups==2:rpc('miner_start',[1]);rpc('evm_mine',[])
            return rpc(method,params)
        with pytest.raises(DexExecutorError):read_wallet_cancellation_state(raced,meta['owner'],meta['operator'],nonce=nonce,amount_wei=5*10**14,original_hash=tx_hash,now=now())
        assert 'eth_sendTransaction' not in reads and 'eth_sendRawTransaction' not in reads
        assert rpc('eth_getTransactionReceipt',[tx_hash])['status']=='0x1'


def test_review_binds_original_fee_evidence_and_prior_caps_to_exact_wallet_amount_and_nonce(tmp_path):
    with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
        nonce,tx_hash=original(rpc,meta)
        state=read_wallet_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,amount_wei=5*10**14,original_hash=tx_hash,now=now())
        args=dict(gas_price=1800000000,bump_percent=10,max_gas_wei=10**14,accept_unknown_fee=False,now=now())
        for fee in (replace(state.original_fee,nonce=nonce+1),replace(state.original_fee,amount_wei=1),
                replace(state.original_fee,wallet=meta['operator'].lower()),replace(state.original_fee,recipient=meta['owner'].lower()),
                replace(state.original_fee,tx_hash='0x'+'ab'*32),replace(state.original_fee,transaction_type=True)):
            with pytest.raises(DexExecutorError):plan_wallet_cancellation(replace(state,original_fee=fee),**args)
        with pytest.raises(DexExecutorError):plan_wallet_cancellation(state,previous_prices=(2*10**9,),**args)
        with pytest.raises(DexExecutorError):plan_wallet_cancellation(state,previous_fees_unknown=True,**args)
        with pytest.raises(DexExecutorError):plan_wallet_cancellation(state,previous_prices=(True,),**args)
        plan=plan_wallet_cancellation(state,previous_prices=(2*10**9,),**{**args,'gas_price':2200000000})
        assert plan.minimum_price==2200000000
        assert plan_wallet_cancellation(state,previous_fees_unknown=True,**{**args,'accept_unknown_fee':True}).uncertain_fee_basis
        with pytest.raises(DexExecutorError):plan_wallet_cancellation(replace(state,snapshot=replace(state.snapshot,balance=1,latest_balance=1)),**args)
