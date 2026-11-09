"""Owned signed source factories, shared by gas journal/acceptance integration."""
from contextlib import contextmanager,ExitStack,nullcontext
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from uuid import uuid4
import json

from sqlalchemy import select
from app.live_execution.dex_gas_order import collect_order
from app.portfolio.dex_gas_order_source import gas_source,gas_reference
from app.portfolio import dex_deployments,dex_gas_funding,dex_gas_return,dex_native_resolution,wallet_swap
from app.portfolio.models import RealDexControlIntent
from tests.test_dex_deployments import setup,PASSWORD
from tests.dex_evm_fixture import evm


def now():return datetime.now(timezone.utc)


def discover(vault,kind,identifier,wallet,rpc):
    with vault.session()as db:
        source=gas_source(db,kind,identifier,wallet_address=wallet);reference=gas_reference(db,source)
        height=int(json.loads(source.receipt_json)['block_number'])
        block=rpc('eth_getBlockByNumber',[hex(height),False])
        matches=[tx for tx in block['transactions']if reference('tx',tx)==source.transaction_ref]
    assert len(matches)==1
    return matches[0]


@contextmanager
def gas_case(directory,kind='CALL_RESOLUTION',*,typed=False):
    with ExitStack()as stack:
        password=PASSWORD
        if kind=='CALL_RESOLUTION':
            from tests.test_dex_call_booking import resolved_case
            from tests.test_wallet_swap import PASSWORD as SWAP_PASSWORD
            values=stack.enter_context(resolved_case(directory,'APPROVE',typed=typed))
            vault,account,_,meta,rpc,_,_,resolved=values;identifier=resolved['id'];password=SWAP_PASSWORD
        elif kind in ('GAS_FUNDING','NATIVE_RESOLUTION'):
            from tests.test_dex_gas_funding import case,prepare,claim
            values=stack.enter_context(case(directory,evm_backend='anvil'))
            vault,account,_,draft,meta,rpc,_,_=values;plan=prepare(values);claim(values,plan)
            tx=dict(plan['transaction'])
            if typed:
                tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(1200000000),maxPriorityFeePerGas=hex(100000000),accessList=[])
            if kind=='NATIVE_RESOLUTION':tx.update(to=meta['owner'],value='0x0')
            sent=rpc('eth_sendTransaction',[tx])
            if kind=='GAS_FUNDING':
                with vault.session()as db:dex_gas_funding.report_funding(db,plan['id'],account_id=account,tx_hash=sent,now=now())
                with vault.session()as db:dex_gas_funding.check_funding(db,plan['id'],account_id=account,evidence=collect_order(rpc,sent),now=now())
                identifier=plan['id']
            else:
                from app.live_execution.dex_native_resolution import collect_resolution
                args=dict(account_id=account,wallet_address=meta['owner'],evidence=collect_resolution(rpc,sent),accept_changes=True,now=now())
                with vault.session()as db:preview=dex_native_resolution.preview_resolution(db,'GAS_FUNDING',plan['id'],**args)
                with vault.session()as db:resolved=dex_native_resolution.confirm_resolution(db,'GAS_FUNDING',plan['id'],
                    preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],**args)
                identifier=resolved['id']
        elif kind=='OPERATOR':
            from tests.test_dex_operator_journal import case,prepared_execution,evidence
            from app.live_execution.dex_trade import read_stop_plan
            from app.portfolio import dex_operator_journal
            vault,account,meta,rpc,deployment,grant,_=stack.enter_context(case(directory,evm_backend='anvil'))
            plan=read_stop_plan(rpc,meta['owner'],int(deployment['nonce']),500,gas=100000,gas_price=10**9,max_gas_wei=10**14,now=now())
            session,prepared=prepared_execution(vault,account,grant,plan);stack.callback(session.revoke);hashes=[]
            def send(raw):
                sent=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(sent);return sent
            prepared.broadcast_once(send,now=now())
            with vault.session()as db:dex_operator_journal.check_intent(db,prepared.identifier,account_id=account,evidence=evidence(meta,rpc,plan,hashes[0]),now=now())
            identifier=prepared.identifier
        elif kind=='GAS_RETURN':
            from tests.dex_bot_worker_fixture import running_case
            from app.live_execution.dex_bot_worker import DexBotWorker
            from app.live_execution.dex_gas_return import read_return_state,plan_return
            from eth_account import Account
            from eth_utils import to_checksum_address
            values,started,claim,connection,clock,mine,market=stack.enter_context(running_case(directory,evm_backend='anvil'))
            vault,account,manager,draft,meta,rpc,_,binding=values
            worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
                market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0]);worker.stop()
            clock[0]+=timedelta(seconds=1);mine()
            plan=plan_return(read_return_state(rpc,meta['owner'],draft['operator_address'],now=clock[0]),gas_price=10**9,max_gas_wei=21000*10**9,now=clock[0])
            identifier=str(uuid4())
            with vault.session()as db:review=dex_gas_return.prepare_return(db,identifier=identifier,account_id=account,binding_id=binding['id'],draft_id=draft['id'],
                wallet_address=meta['owner'],drafts=manager,plan=plan,now=clock[0])
            with vault.session()as db:dex_gas_return.reserve_return(db,identifier,account_id=account,wallet_address=meta['owner'],drafts=manager,
                draft_id=draft['id'],plan=plan,plan_digest=review['plan_digest'],now=clock[0])
            fields={k:to_checksum_address(v)if k=='to'else v if k=='data'else int(v,16)for k,v in plan.transaction().items()if k not in ('from','type')}
            with manager._copy(draft['id'],account,meta['owner'],clock[0])as (_,key):signed=Account.sign_transaction(fields,bytes(key))
            sent='0x'+signed.hash.hex()
            with vault.session()as db:dex_gas_return.record_signature(db,identifier,account_id=account,plan=plan,raw=bytes(signed.raw_transaction),tx_hash=sent,now=clock[0])
            with vault.session()as db:dex_gas_return.mark_broadcast(db,identifier,account_id=account,plan_digest=review['plan_digest'],tx_hash=sent,now=clock[0])
            assert rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])==sent
            with vault.session()as db:dex_gas_return.check_return(db,identifier,account_id=account,evidence=collect_order(rpc,sent),now=clock[0])
        else:
            vault,account,at=setup(directory);stack.callback(vault.lock)
            meta,rpc=stack.enter_context(evm(directory/'evm',backend='anvil'))
            if kind=='DEPLOYMENT':
                from tests.test_dex_deployment_receipt import deployment_on_evm
                plan,observed=deployment_on_evm(vault,account,at,meta,rpc,typed=typed)
                with vault.session()as db:dex_deployments.check_deployment(db,plan['id'],account_id=account,evidence=observed,now=now())
                identifier=plan['id']
            elif kind=='CONTROL':
                from tests.test_dex_allowance_journal import ready
                ready(vault,account,at,meta,rpc)
                with vault.session()as db:identifier=next(db.scalars(select(RealDexControlIntent.id)))
            elif kind=='DEPLOYMENT_RESOLUTION':
                from tests.test_dex_deployment_resolution import replacement_case,review,commit
                plan,_,observed=replacement_case(vault,account,meta,rpc,cancel=True,typed=typed)
                result=commit(vault,account,meta,plan,observed,review(vault,account,meta,plan,observed));identifier=result['id']
            elif kind=='WALLET_SWAP':
                from tests.test_dex_call_resolution import prepare
                from app.live_execution.dex_call_resolution import collect_call_resolution
                plan,call,_=prepare(vault,account,meta,rpc,action='APPROVE')
                sent=rpc('eth_sendTransaction',[plan['transaction']]);observed=collect_call_resolution(rpc,call,sent)
                old={name:observed[name]for name in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
                with vault.session()as db:wallet_swap.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=now())
                with vault.session()as db:wallet_swap.check_swap(db,plan['id'],account_id=account,evidence=old,now=now())
                identifier=plan['id']
            else:raise AssertionError(kind)
        with vault.session()as db:source=gas_source(db,kind,identifier,wallet_address=meta['owner'])
        tx_hash=discover(vault,kind,identifier,meta['owner'],rpc);observed=collect_order(rpc,tx_hash)
        yield SimpleNamespace(vault=vault,account=account,kind=kind,id=identifier,meta=meta,rpc=rpc,source=source,password=password,
            scope={'context_account_id':account,'wallet_address':meta['owner'],'evidence':observed,'now':max(now(),source.updated_at+timedelta(seconds=1))})


@contextmanager
def mixed_order_case(directory,monkeypatch):
    """Actual operator BUY and unrelated wallet permission in one owned block."""
    from tests.test_dex_receipt_booking import case
    from tests.test_dex_call_resolution import prepare
    from app.live_execution.dex_call_resolution import collect_call_resolution
    from app.portfolio import dex_receipt_booking
    from app.portfolio.dex_receipt_order_source import order_source,order_reference
    with case(directory,monkeypatch,evm_backend='anvil')as (values,_,worker):
        data,_,_,_,clock,mine,_=values;vault,account,_,_,meta,rpc,_,_=data
        plan,call,_=prepare(vault,account,meta,rpc,action='APPROVE',token='WBNB')
        clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
        assert worker.run_once()['last_action']=='BUY'
        sent=rpc('eth_sendTransaction',[plan['transaction']]);rpc('evm_mine',[]);rpc('miner_start',[1])
        assert worker.run_once()['last_receipt_status']=='FINALIZED';worker.stop()
        observed=collect_call_resolution(rpc,call,sent)
        old={name:observed[name]for name in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
        with vault.session()as db:wallet_swap.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=clock[0])
        with vault.session()as db:wallet_swap.check_swap(db,plan['id'],account_id=account,evidence=old,now=clock[0])
        with vault.session()as db:
            financial=next(item for item in dex_receipt_booking.list_receipts(db,account_id=account,wallet_address=meta['owner'])if item['source_kind']=='OPERATOR')
            trade=order_source(db,'OPERATOR',financial['source_id'],wallet_address=meta['owner'])
            gas=gas_source(db,'WALLET_SWAP',plan['id'],wallet_address=meta['owner'])
            height=int(json.loads(trade.receipt_json)['block_number'])
            block=rpc('eth_getBlockByNumber',[hex(height),False]);assert len(block['transactions'])==2
            tx=next(value for value in block['transactions']if order_reference(db,trade)('tx',value)==trade.transaction_ref)
        stamp=max(now(),clock[0],trade.updated_at,gas.updated_at)+timedelta(seconds=1)
        scope={'context_account_id':account,'wallet_address':meta['owner'],'now':stamp}
        yield SimpleNamespace(vault=vault,account=account,meta=meta,rpc=rpc,trade=trade,gas=gas,
            trade_scope={**scope,'evidence':collect_order(rpc,tx)},gas_scope={**scope,'evidence':collect_order(rpc,sent)})
