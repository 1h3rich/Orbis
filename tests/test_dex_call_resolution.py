"""Mined call replacements on owned EVMs; never user wallets or mainnet."""
from copy import deepcopy
from datetime import datetime,timezone
from uuid import uuid4

import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import wallet_source,operator_source,collect_call_resolution,verify_call_resolution,validate_call_proof
from app.portfolio import wallet_swap as swaps,dex_operator_journal as operators
from tests.dex_evm_fixture import evm
from tests.test_wallet_swap import setup
from tests.test_dex_operator import write


def now():return datetime.now(timezone.utc)


def prepare(vault,account,meta,rpc,*,action='SWAP',token='USDT',amount='0.01'):
    nonce=str(int(rpc('eth_getTransactionCount',[meta['owner'],'latest']),16))
    with vault.session() as db:
        plan=swaps.prepare_swap(db,identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],action=action,
            token_in=token,amount=amount,quote_units='10000000000000000',fee=500,slippage_bps=25,
            nonce=nonce,gas='200000',gas_price='1000000000',max_gas_bnb='0.0003',now=now())
    with vault.session() as db:assert swaps.claim_swap(db,plan['id'],account_id=account,wallet_address=meta['owner'],plan_digest=plan['plan_digest'],now=now())['claim_granted']
    with vault.session() as db:
        row,terms,state=swaps.swap_record(db,plan['id']);source=wallet_source(row,terms,state,meta['owner'])
        before=(row.payload_json,row.state_json,row.updated_at)
    return plan,source,before


def verify(vault,source,evidence):
    with vault.session() as db:
        ref=lambda kind,value: (swaps if source.kind=='WALLET_SWAP' else operators)._identity(db,kind,value)
        proof=verify_call_resolution(source,evidence,reference=ref,now=now())
        validate_call_proof(proof,source,now=now())
        return proof


@pytest.mark.parametrize('typed',[False,True])
def test_actual_pending_manual_call_is_replaced_by_self_cancellation_without_a_fill(tmp_path,typed):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,before=prepare(vault,account,meta,rpc)
            rpc('miner_stop',[]);original=rpc('eth_sendTransaction',[plan['transaction']])
            with vault.session() as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=original,now=now())
            with vault.session() as db:
                row,terms,state=swaps.swap_record(db,plan['id']);source=wallet_source(row,terms,state,meta['owner']);before=(row.payload_json,row.state_json,row.updated_at)
            tx={'from':meta['owner'],'to':meta['owner'],'data':'0x','value':'0x0','chainId':'0x38','nonce':hex(source.nonce),'gas':'0x5208','gasPrice':hex(2*10**9)}
            if typed:
                tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(2*10**9),maxPriorityFeePerGas=hex(10**9))
            sent=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
            assert rpc('eth_getTransactionReceipt',[original]) is None
            proof=verify(vault,source,collect_call_resolution(rpc,source,sent))
            assert proof['outcome']=='CANCELLED' and proof['effect'] is None and not proof['matches_call']
            assert proof['receipt']['gas_used']=='21000' and int(proof['receipt']['gas_fee_wei'])>0
            assert proof['receipt']['transaction_type']==(2 if typed else 0)
            with vault.session() as db:
                row,_,_=swaps.swap_record(db,plan['id']);assert (row.payload_json,row.state_json,row.updated_at)==before
            assert all(value.lower() not in str(proof).lower() for value in (meta['owner'],sent,original))
    finally:vault.lock()


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('action,token',[('APPROVE','USDT'),('SWAP','USDT'),('APPROVE','WBNB'),('SWAP','WBNB'),('REVOKE','USDT')])
def test_manual_real_buy_sell_approvals_and_revocation_recover_changed_fees(tmp_path,typed,action,token):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
            for fixture_token,recipient in [('usdt',meta['owner']),('wbnb',meta['owner']),('usdt',meta['pool']),('wbnb',meta['pool'])]:
                write(rpc,meta,meta[fixture_token],'mint(address,uint256)',['address','uint256'],[recipient,10**18])
            for token_name in ('USDT','WBNB'):
                write(rpc,{**meta,'operator':meta['owner']},swaps.TOKENS[token_name],'approve(address,uint256)',['address','uint256'],[meta['router'],10**18])
            plan,source,before=prepare(vault,account,meta,rpc,action=action,token=token)
            tx={**plan['transaction'],'gasPrice':hex(2*10**9)}
            if typed:
                tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(2*10**9),maxPriorityFeePerGas=hex(10**9))
            sent=rpc('eth_sendTransaction',[tx]);evidence=collect_call_resolution(rpc,source,sent)
            with vault.session() as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=now())
            with vault.session() as db:
                row,terms,state=swaps.swap_record(db,plan['id']);source=wallet_source(row,terms,state,meta['owner'])
                before=(row.payload_json,row.state_json,row.updated_at)
            legacy_evidence={key:evidence[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
            with pytest.raises(ValueError):
                with vault.session() as db:swaps.check_swap(db,plan['id'],account_id=account,evidence=legacy_evidence,now=now())
            proof=verify(vault,source,evidence)
            assert proof['outcome']=='EXECUTED' and proof['matches_call'] and proof['fee_changed']
            assert proof['matches_original'] is True
            assert proof['receipt']['gas_fee_wei']==str(int(evidence['receipt']['gasUsed'],16)*int(evidence['receipt']['effectiveGasPrice'],16))
            assert proof['effect']['amount_out_units']==('10000000000000000' if action=='SWAP' else None)
            assert proof['effect']['approved_units']==(None if action=='SWAP' else '0' if action=='REVOKE' else '10000000000000000')
            with vault.session() as db:
                row,_,_=swaps.swap_record(db,plan['id']);assert (row.payload_json,row.state_json,row.updated_at)==before
    finally:vault.lock()
