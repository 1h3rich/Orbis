"""Owner-only residual BNB arithmetic and native bytes on identified local EVM."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone

import pytest
from eth_account import Account
from eth_utils import to_checksum_address

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_return import read_return_state,plan_return,verify_return_signature
from tests.dex_evm_fixture import evm


def test_return_plan_sweeps_exact_native_balance_minus_its_fee_and_signature_pins_every_field(tmp_path):
    with evm(tmp_path/'evm') as (meta,rpc):
        now=datetime.now(timezone.utc)
        state=read_return_state(rpc,meta['owner'],meta['operator'],now=now)
        plan=plan_return(state,gas_price=10**9,max_gas_wei=21000*10**9,now=now)
        assert plan.amount_wei==state.operator_balance-21000*10**9
        tx=plan.transaction();assert tx['to']==meta['owner'].lower() and tx['from']==meta['operator'].lower()
        assert tx['value']==hex(plan.amount_wei) and tx['data']=='0x' and tx['gas']=='0x5208'
        signable={k:(to_checksum_address(v) if k=='to' else int(v,16) if k not in ('data',) else v)
            for k,v in tx.items() if k not in ('from','type')}
        signed=Account.sign_transaction(signable,'0x'+'22'*32)
        original='0x'+signed.hash.hex();verify_return_signature(plan,bytes(signed.raw_transaction),original)
        for key,value in [('to',to_checksum_address(meta['operator'])),('value',plan.amount_wei-1),
                          ('chainId',1),('nonce',state.operator_nonce+1),('gas',22000),('data','0x01')]:
            wrong=Account.sign_transaction({**signable,key:value},'0x'+'22'*32)
            with pytest.raises(DexExecutorError):verify_return_signature(plan,bytes(wrong.raw_transaction),'0x'+wrong.hash.hex())
        with pytest.raises(DexExecutorError):verify_return_signature(plan,bytes(signed.raw_transaction),'0x'+'ab'*32)
        wrong=Account.sign_transaction(signable,'0x'+'11'*32)
        with pytest.raises(DexExecutorError):verify_return_signature(plan,bytes(wrong.raw_transaction),'0x'+wrong.hash.hex())
        # Own fixture signature only; there is no production signing/sending path yet.
        owner_before=int(rpc('eth_getBalance',[meta['owner'],'latest']),16)
        sent=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
        receipt=rpc('eth_getTransactionReceipt',[sent]);assert receipt['status']=='0x1' and int(receipt['gasUsed'],16)==21000
        assert int(rpc('eth_getBalance',[meta['operator'],'latest']),16)==0
        assert int(rpc('eth_getBalance',[meta['owner'],'latest']),16)-owner_before==plan.amount_wei


def test_return_plan_rejects_pending_nonce_empty_or_insufficient_balance_and_stale_observation(tmp_path):
    with evm(tmp_path/'evm') as (meta,rpc):
        now=datetime.now(timezone.utc);state=read_return_state(rpc,meta['owner'],meta['operator'],now=now)
        for wrong in (replace(state,pending_nonce=state.operator_nonce+1),replace(state,operator_balance=21000*10**9),
                      replace(state,operator_balance=0),replace(state,operator_nonce=True),replace(state,owner=state.operator),
                      replace(state,owner_code_empty=False),replace(state,operator_code_empty=False),
                      replace(state,block_at=now-timedelta(seconds=31)),replace(state,observed_at=now+timedelta(seconds=1))):
            with pytest.raises(DexExecutorError):plan_return(wrong,gas_price=10**9,max_gas_wei=21000*10**9,now=now)
        for price,maximum in ((True,10**14),(0,10**14),(10**12+1,10**17),(10**9,21000*10**9-1)):
            with pytest.raises(DexExecutorError):plan_return(state,gas_price=price,max_gas_wei=maximum,now=now)
        plan=plan_return(state,gas_price=10**9,max_gas_wei=21000*10**9,now=now)
        with pytest.raises(DexExecutorError):replace(plan,amount_wei=plan.amount_wei+1).validate(now)
        with pytest.raises(DexExecutorError):plan.validate(now+timedelta(seconds=16))


@pytest.mark.parametrize('fault',['chain','code','hash','number','nonce'])
def test_return_reader_rejects_changed_network_program_or_block_and_queued_nonce(tmp_path,fault):
    with evm(tmp_path/'evm') as (meta,rpc):
        reads=[]
        def read(method,params):
            result=rpc(method,params);reads.append(method)
            if fault=='chain' and method=='eth_chainId':return '0x1'
            if fault=='code' and method=='eth_getCode':return '0x6000'
            if method=='eth_getBlockByNumber' and reads.count(method)==2:
                if fault=='hash':return {**result,'hash':'0x'+'ab'*32}
                if fault=='number':return {**result,'number':hex(int(result['number'],16)+1)}
            if fault=='nonce' and method=='eth_getTransactionCount' and params[1]=='pending':return hex(int(result,16)+1)
            return result
        with pytest.raises(DexExecutorError):read_return_state(read,meta['owner'],meta['operator'],now=datetime.now(timezone.utc))
