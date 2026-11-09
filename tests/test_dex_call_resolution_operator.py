"""Operator replacements preserve original intents and exact economic effects."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone

import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_trade import read_trade_plan,read_stop_plan
from app.live_execution.dex_operator_transport import collect_operator_receipt
from app.live_execution.dex_call_resolution import operator_source,collect_call_resolution
from app.portfolio import dex_operator_journal as journal
from tests.test_dex_call_resolution import verify
from tests.test_dex_operator_journal import case,prepared_execution


def now():return datetime.now(timezone.utc)


def lower_price(meta,rpc,nonce,*,buy=True,amount=10**16):
    return read_trade_plan(rpc,meta['owner'],nonce,500,buy=buy,amount=amount,slippage_bps=25,
        gas=500000,gas_price=5*10**8,max_gas_wei=10**15,now=now())


def pending(values,trade):
    vault,account,meta,rpc,deployment,grant,_=values
    session,prepared=prepared_execution(vault,account,grant,trade)
    rpc('miner_stop',[])
    original=[]
    def post(raw):
        tx_hash=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);original.append(tx_hash);return tx_hash
    prepared.broadcast_once(post,now=now());session.revoke()
    with vault.session() as db:
        row,terms,state=journal.intent_record(db,prepared.identifier);source=operator_source(row,terms,state,journal.grant_record(db,grant['id'])[1],meta['owner'],500)
        before=(row.payload_json,row.state_json,row.updated_at)
    return source,before,original[0]


def assert_unchanged(vault,source,before):
    with vault.session() as db:
        row,_,_=journal.intent_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before


@pytest.mark.parametrize('typed',[False,True])
def test_cancel_operator_pending_buy_has_no_position_or_output_and_preserves_original(tmp_path,typed):
    with case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,_=values
        trade=lower_price(meta,rpc,int(deployment['nonce']));source,before,original=pending(values,trade)
        tx={'from':meta['operator'],'to':meta['operator'],'nonce':hex(source.nonce),'chainId':'0x38','data':'0x','value':'0x0','gas':'0x5208','gasPrice':hex(10**9)}
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(10**9),maxPriorityFeePerGas=hex(5*10**8))
        sent=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        proof=verify(vault,source,collect_call_resolution(rpc,source,sent))
        assert proof['outcome']=='CANCELLED' and proof['effect'] is None and proof['matches_original'] is False
        assert rpc('eth_getTransactionReceipt',[original]) is None
        assert_unchanged(vault,source,before)


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('action',['BUY','SELL','STOP'])
def test_replacement_executes_the_original_known_call_with_actual_fees_and_partial_cost(tmp_path,typed,action):
    with case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,first=values
        nonce=int(deployment['nonce'])
        if action!='BUY':
            session,prepared=prepared_execution(vault,account,grant,first)
            actual=[]
            def post(raw):
                sent=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);actual.append(sent);return sent
            prepared.broadcast_once(post,now=now());session.revoke()
            evidence=collect_operator_receipt(rpc,meta['owner'],nonce,actual[0])
            with vault.session() as db:assert journal.check_intent(db,prepared.identifier,account_id=account,evidence=evidence,now=now())['status']=='FINALIZED'
        trade=(read_stop_plan(rpc,meta['owner'],nonce,500,gas=100000,gas_price=5*10**8,max_gas_wei=10**14,now=now()) if action=='STOP'
            else lower_price(meta,rpc,nonce,buy=action=='BUY',amount=4*10**15 if action=='SELL' else 10**16))
        source,before,original=pending(values,trade)
        tx={**trade.transaction(),'gasPrice':hex(10**9)}
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(10**9),maxPriorityFeePerGas=hex(5*10**8))
        sent=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        proof=verify(vault,source,collect_call_resolution(rpc,source,sent))
        assert proof['outcome']=='EXECUTED' and proof['matches_call'] and proof['fee_changed'] and proof['fee_overrun']
        effect=proof['effect']
        if action=='BUY':assert effect['owned_wbnb']==effect['cost_usdt']=='10000000000000000'
        elif action=='SELL':
            assert effect['owned_wbnb']==effect['cost_usdt']=='6000000000000000'
            assert effect['assigned_cost_usdt']=='4000000000000000' and effect['realized_pnl_usdt']=='0'
        else:assert effect['amount_out_units'] is None and effect['owned_wbnb']=='10000000000000000'
        assert rpc('eth_getTransactionReceipt',[original]) is None
        assert_unchanged(vault,source,before)
