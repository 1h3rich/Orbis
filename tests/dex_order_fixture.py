"""Two actual economic emitters in one owned block, with old index-free receipts."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
import json

from app.live_execution.dex_receipt_order import collect_order
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.portfolio import dex_receipt_booking as booking,wallet_swap as swaps
from app.portfolio.dex_receipt_order_source import order_source,order_reference
from tests.test_dex_receipt_booking import case as operator_case
from tests.test_dex_operator import write
from tests.test_dex_call_resolution import prepare,now


def finalized_wallet_order(case):
    vault,account,_,meta,rpc,_,_,_=case
    plan,call,_=prepare(vault,account,meta,rpc)
    sent=rpc('eth_sendTransaction',[plan['transaction']]);read=collect_call_resolution(rpc,call,sent)
    with vault.session() as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=now())
    legacy={key:read[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
    with vault.session() as db:swaps.check_swap(db,plan['id'],account_id=account,evidence=legacy,now=now())
    with vault.session() as db:original=order_source(db,'WALLET_SWAP',plan['id'],wallet_address=meta['owner'])
    return original,collect_order(rpc,sent)


@contextmanager
def same_block_case(directory,monkeypatch):
    with operator_case(directory,monkeypatch,evm_backend='anvil') as (values,ledger_account,worker):
        data,_,_,_,clock,mine,_=values;vault,account,_,_,meta,rpc,_,_=data
        write(rpc,meta,meta['wbnb'],'mint(address,uint256)',['address','uint256'],[meta['pool'],10**18])
        write(rpc,{**meta,'operator':meta['owner']},meta['usdt'],'approve(address,uint256)',
            ['address','uint256'],[meta['router'],10**18])
        plan,call,_=prepare(vault,account,meta,rpc)
        clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
        assert worker.run_once()['last_action']=='BUY'
        sent=rpc('eth_sendTransaction',[plan['transaction']]);rpc('evm_mine',[]);rpc('miner_start',[1])
        assert worker.run_once()['last_receipt_status']=='FINALIZED';worker.stop()
        read=collect_call_resolution(rpc,call,sent)
        with vault.session() as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=clock[0])
        legacy={key:read[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
        with vault.session() as db:swaps.check_swap(db,plan['id'],account_id=account,evidence=legacy,now=clock[0])
        with vault.session() as db:
            operator=next(row for row in booking.list_receipts(db,account_id=account,wallet_address=meta['owner']) if row['source_kind']=='OPERATOR')
            sources=[order_source(db,kind,identifier,wallet_address=meta['owner']) for kind,identifier in
                [('OPERATOR',operator['source_id']),('WALLET_SWAP',plan['id'])]]
        receipts=[json.loads(item.receipt_json) for item in sources]
        assert receipts[0]['block_number']==receipts[1]['block_number'] and receipts[0]['block_hash']==receipts[1]['block_hash']
        block=rpc('eth_getBlockByNumber',[hex(int(receipts[0]['block_number'])),False]);assert len(block['transactions'])==2
        observations={}
        for tx in block['transactions']:
            observed=collect_order(rpc,tx)
            with vault.session() as db:
                for item in sources:
                    if order_reference(db,item)('tx',tx)==item.transaction_ref:observations[item.key]=observed
        assert len(observations)==2
        yield SimpleNamespace(vault=vault,account=account,ledger_account=ledger_account,meta=meta,rpc=rpc,
            sources=sources,observations=observations,at=clock[0]+timedelta(seconds=1))
