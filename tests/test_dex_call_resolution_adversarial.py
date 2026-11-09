"""Reverted opaque calls have only gas; unknown success never becomes a fill."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from eth_abi import encode
from eth_utils import keccak

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import collect_call_resolution,validate_call_proof
from app.live_execution.evm_transaction import recover_wallet_call,recover_reverted_call
from tests.dex_evm_fixture import evm
from tests.test_wallet_swap import setup
from tests.test_dex_call_resolution import prepare,verify,now


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('call',['known','opaque','empty-fallback','creation'])
def test_failed_known_or_opaque_call_consumes_nonce_but_never_creates_trade_or_approval(tmp_path,typed,call):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,before=prepare(vault,account,meta,rpc)
            # Known swap fails for absent token balance/allowance. Opaque calls
            # really revert in the fixture, including native value and CREATE.
            tx={**plan['transaction'],'gasPrice':hex(2*10**9)}
            if call!='known':tx.update(to=meta['router'],data='0xdeadbeef',value='0x1')
            if call=='empty-fallback':tx['data']='0x'
            if call=='creation':tx.update(to=None,data='0x60006000fd',value='0x0')
            if typed:
                tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(2*10**9),maxPriorityFeePerGas=hex(10**9))
            sent=rpc('eth_sendTransaction',[tx]);evidence=collect_call_resolution(rpc,source,sent)
            assert evidence['receipt']['status']=='0x0' and evidence['receipt']['logs']==[]
            proof=verify(vault,source,evidence)
            assert proof['outcome']=='REVERTED' and proof['effect'] is None and proof['matches_call'] is (call=='known')
            assert proof['call_kind']==('KNOWN' if call=='known' else 'CREATE' if call=='creation' else 'OTHER_CALL')
            assert int(proof['source_nonce'])>source.nonce and int(proof['receipt']['gas_fee_wei'])>0
            with pytest.raises(ValueError):recover_reverted_call(evidence['transaction'],receipt_status=True)
            with pytest.raises(ValueError):recover_reverted_call(evidence['transaction'],receipt_status=1)
            if call=='empty-fallback':
                with pytest.raises(ValueError):recover_wallet_call(evidence['transaction'],expected_to=meta['router'].lower())
    finally:vault.lock()


def test_signed_successful_other_call_does_not_resolve_the_original_swap(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,before=prepare(vault,account,meta,rpc)
            data='0x'+(keccak(text='set(uint256,bool)')[:4]+encode(['uint256','bool'],[12345,False])).hex()
            sent=rpc('eth_sendTransaction',[{**plan['transaction'],'to':meta['router'],'data':data}])
            evidence=collect_call_resolution(rpc,source,sent);assert evidence['receipt']['status']=='0x1'
            with pytest.raises(DexExecutorError):verify(vault,source,evidence)
            from app.portfolio.wallet_swap import swap_record
            with vault.session() as db:
                row,_,_=swap_record(db,plan['id']);assert (row.payload_json,row.state_json,row.updated_at)==before
    finally:vault.lock()


def test_actual_approval_rejects_signed_field_linkage_gas_and_log_tampering_and_replays_sanitized_proof(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,_=prepare(vault,account,meta,rpc,action='APPROVE')
            sent=rpc('eth_sendTransaction',[{**plan['transaction'],'gasPrice':hex(2*10**9)}])
            reads=[]
            def read(method,params):reads.append(method);return rpc(method,params)
            evidence=collect_call_resolution(read,source,sent);proof=verify(vault,source,evidence)
            assert not any(method in {'eth_sendTransaction','eth_sendRawTransaction'} for method in reads)
            for part,key,value in [('transaction','nonce',hex(source.nonce+1)),('transaction','from',meta['operator']),
                    ('transaction','gasPrice',hex(3*10**9)),('transaction','input','0x12345678'),
                    ('transaction','hash','0x'+'ab'*32),('receipt','effectiveGasPrice',hex(3*10**9)),
                    ('receipt','transactionIndex','0xffff'),('receipt','blockHash','0x'+'ab'*32),
                    ('receipt','to',meta['owner']),('receipt','status','0x2'),('block','transactions',[]),
                    ('finalized','number','0x0')]:
                wrong=deepcopy(evidence);wrong[part][key]=value
                with pytest.raises(DexExecutorError):verify(vault,source,wrong)
            for key,value in [('source_nonce',hex(source.nonce)),('source_code','0x01'),('target_code','0x')]:
                wrong=deepcopy(evidence);wrong[key]=value
                with pytest.raises(DexExecutorError):verify(vault,source,wrong)
            wrong=deepcopy(evidence);wrong['receipt']['logs'][0]['data']='0x'+(1).to_bytes(32,'big').hex()
            with pytest.raises(DexExecutorError):verify(vault,source,wrong)
            for part,key,value in [(None,'matches_call',False),(None,'fee_changed',False),(None,'source_nonce',str(source.nonce)),
                    (None,'call_kind','OTHER_CALL'),(None,'call_digest','a'*64),(None,'expected_call_digest','b'*64),
                    ('effect','approved_units','1'),('receipt','gas_fee_wei','1')]:
                wrong=deepcopy(proof);(wrong if part is None else wrong[part])[key]=value
                with pytest.raises(ValueError):validate_call_proof(wrong,source,now=now())
            with pytest.raises(ValueError):validate_call_proof(proof,replace(source,created_at=now()+timedelta(minutes=1)),now=now())
            # An archive can replay amounts without retaining the owner's raw
            # address. The enclosing journal must seal these attested references.
            validate_call_proof(proof,replace(source,owner='0x'+'01'*20),now=now())
            # A consumed nonce observed before the receipt contradicts recovery.
            with pytest.raises(ValueError):validate_call_proof(proof,replace(source,observation={'block_number':'1','block_hash':'0x'+'ab'*32}),now=now())
            repeated=0
            def forked(method,params):
                nonlocal repeated
                value=rpc(method,params)
                if method=='eth_getBlockByNumber' and params[0]!='finalized':
                    repeated+=1
                    if repeated==2:return {**value,'hash':'0x'+'cd'*32}
                return value
            with pytest.raises(DexExecutorError):collect_call_resolution(forked,source,sent)
    finally:vault.lock()


def test_missing_or_unfinalized_receipt_never_produces_an_effect(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,_=prepare(vault,account,meta,rpc,action='APPROVE')
            assert collect_call_resolution(rpc,source,'0x'+'ab'*32) is None
            sent=rpc('eth_sendTransaction',[plan['transaction']])
            def lagged(method,params):
                result=rpc(method,params)
                if method=='eth_getBlockByNumber' and params[0]=='finalized':return {**result,'number':'0x0'}
                return result
            assert collect_call_resolution(lagged,source,sent) is None
    finally:vault.lock()
