"""A cancellation signs only zero-to-self at the original nonce. Own EVM."""
from datetime import datetime,timezone
from datetime import timedelta
from dataclasses import replace
from eth_account import Account
from eth_utils import to_checksum_address
import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_cancellation import read_cancellation_state,plan_cancellation,verify_cancellation_signature
from app.live_execution.dex_gas_return import read_return_state,plan_return,verify_return_signature
from tests.dex_evm_fixture import evm


def test_cancellation_preserves_principal_pins_every_signed_field_and_cannot_use_the_return_signer(tmp_path):
    with evm(tmp_path/'evm') as (meta,rpc):
        now=datetime.now(timezone.utc);nonce=int(rpc('eth_getTransactionCount',[meta['operator'],'latest']),16)
        state=read_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,now=now)
        plan=plan_cancellation(state,original_nonce=nonce,original_gas_price=10**9,gas_price=1200000000,bump_percent=10,max_gas_wei=25200*10**9,now=now)
        tx=plan.transaction();assert tx['to']==tx['from']==meta['operator'].lower() and tx['value']=='0x0' and tx['data']=='0x'
        signable={key:to_checksum_address(value) if key=='to' else value if key=='data' else int(value,16)
            for key,value in tx.items() if key not in ('from','type')}
        signed=Account.sign_transaction(signable,'0x'+'22'*32);tx_hash='0x'+signed.hash.hex()
        verify_cancellation_signature(plan,bytes(signed.raw_transaction),tx_hash)
        original_return=plan_return(read_return_state(rpc,meta['owner'],meta['operator'],now=now),gas_price=10**9,max_gas_wei=21000*10**9,now=now)
        with pytest.raises(DexExecutorError):verify_return_signature(original_return,bytes(signed.raw_transaction),tx_hash)
        for field,value in [('to',to_checksum_address(meta['owner'])),('value',1),('data','0x01'),('nonce',nonce+1),('gas',22000),('gasPrice',1200000001),('chainId',1)]:
            wrong=Account.sign_transaction({**signable,field:value},'0x'+'22'*32)
            with pytest.raises(DexExecutorError):verify_cancellation_signature(plan,bytes(wrong.raw_transaction),'0x'+wrong.hash.hex())
        before=int(rpc('eth_getBalance',[meta['operator'],'latest']),16);owner_before=int(rpc('eth_getBalance',[meta['owner'],'latest']),16)
        receipt=rpc('eth_getTransactionReceipt',[rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])])
        assert receipt['status']=='0x1' and int(receipt['gasUsed'],16)==21000
        assert int(rpc('eth_getBalance',[meta['operator'],'latest']),16)==before-plan.gas_wei
        assert int(rpc('eth_getBalance',[meta['owner'],'latest']),16)==owner_before


def test_cancellation_rejects_consumed_nonce_unknown_queue_stale_state_and_unreviewed_cost(tmp_path):
    with evm(tmp_path/'evm') as (meta,rpc):
        now=datetime.now(timezone.utc);nonce=int(rpc('eth_getTransactionCount',[meta['operator'],'latest']),16)
        state=read_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,now=now)
        arguments=dict(original_nonce=nonce,original_gas_price=10**9,gas_price=1200000000,bump_percent=10,max_gas_wei=25200*10**9,now=now)
        for changed in (replace(state,nonce=True),replace(state,latest_nonce=nonce+1),replace(state,live_nonce=nonce+1),
                        replace(state,pending_nonce=nonce+2),replace(state,balance=1),replace(state,latest_balance=0),
                        replace(state,owner_code_empty=False),replace(state,operator_code_empty=False),
                        replace(state,block_at=now-timedelta(seconds=31)),replace(state,observed_at=now+timedelta(seconds=1))):
            with pytest.raises(DexExecutorError):plan_cancellation(changed,**arguments)
        queued=replace(state,pending_nonce=nonce+1)
        assert plan_cancellation(queued,**arguments).transaction()['nonce']==hex(nonce)
        for changed in ({'original_nonce':nonce+1},{'gas_price':1099999999},{'gas_price':True},{'original_gas_price':False},
                        {'bump_percent':0},{'bump_percent':1.5},{'max_gas_wei':25200*10**9-1}):
            with pytest.raises(DexExecutorError):plan_cancellation(state,**{**arguments,**changed})
        tiny=plan_cancellation(state,**{**arguments,'original_gas_price':101,'gas_price':112})
        assert tiny.minimum_price==112
        with pytest.raises(DexExecutorError):tiny.validate(now+timedelta(seconds=16))


def test_same_nonce_cancellation_replaces_a_pending_native_transfer_on_owned_evm(tmp_path):
    with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
        nonce=int(rpc('eth_getTransactionCount',[meta['operator'],'latest']),16)
        rpc('miner_stop',[])
        original=Account.sign_transaction({'chainId':56,'nonce':nonce,'gas':21000,'gasPrice':10**9,
            'to':to_checksum_address(meta['owner']),'value':100,'data':'0x'},'0x'+'22'*32)
        original_hash=rpc('eth_sendRawTransaction',['0x'+original.raw_transaction.hex()])
        assert rpc('eth_getTransactionReceipt',[original_hash]) is None
        now=datetime.now(timezone.utc)
        state=read_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,now=now)
        plan=plan_cancellation(state,original_nonce=nonce,original_gas_price=10**9,gas_price=1200000000,bump_percent=10,max_gas_wei=25200*10**9,now=now)
        tx=plan.transaction();fields={key:to_checksum_address(value) if key=='to' else value if key=='data' else int(value,16)
            for key,value in tx.items() if key not in ('from','type')}
        cancel=Account.sign_transaction(fields,'0x'+'22'*32);verify_cancellation_signature(plan,bytes(cancel.raw_transaction),'0x'+cancel.hash.hex())
        cancel_hash=rpc('eth_sendRawTransaction',['0x'+cancel.raw_transaction.hex()])
        rpc('miner_start',[1]);rpc('evm_mine',[])
        assert rpc('eth_getTransactionReceipt',[cancel_hash])['status']=='0x1'
        assert rpc('eth_getTransactionReceipt',[original_hash]) is None
        assert int(rpc('eth_getTransactionCount',[meta['operator'],'latest']),16)==nonce+1
        with pytest.raises(DexExecutorError):read_cancellation_state(rpc,meta['owner'],meta['operator'],nonce=nonce,now=datetime.now(timezone.utc))
