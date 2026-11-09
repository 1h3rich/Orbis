"""Own mixed signed Anvil receipts and encrypted ledger, never owner stores."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import os
import json
import pytest
from sqlalchemy import select,func
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.api import create_real_router
from app.portfolio import models as m
from app.portfolio.dex_call_booking import _sources
from app.portfolio.dex_deployments import _identity
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event,account_snapshot,list_original_events
from app.portfolio.wallet_swap_booking import _trade
from app.web.mount import register_ui
from tests.dex_order_fixture import same_block_case

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(parents=True,exist_ok=True)
order_mode=os.environ.get('ORBIS_TEST_ECONOMIC_ORDER','')
multi_mode=os.environ.get('ORBIS_TEST_ECONOMIC_MULTI','')
stack=ExitStack();patch=pytest.MonkeyPatch();stack.callback(patch.undo);workers=[];reads=[];wallet_reads=[]
try:
    case=stack.enter_context(same_block_case(base,patch))
    source=next(item for item in case.sources if item.kind=='WALLET_SWAP')
    with case.vault.session()as db:
        financial_sources=list(_sources(db,case.meta['owner'],_identity(db,'wallet',case.meta['owner'])))
        financial=next(item for item in financial_sources if item.kind==source.kind and item.id==source.id)
    receipt=financial.receipt;trade=_trade(financial.trade_terms,receipt,datetime.fromisoformat(receipt['block_at']))
    trade=replace(trade,occurred_at=trade.occurred_at+timedelta(minutes=1))
    with case.vault.session()as db:manual_trade=append_event(db,case.ledger_account,str(uuid4()),trade).id
    with case.vault.session()as db:
        manual_gas=append_event(db,case.ledger_account,str(uuid4()),EventInput('FEE','BNB',trade.occurred_at,
            amount=Decimal('1E-18'),note='Parte del gas de la compra manual')).id
        originals=list_original_events(db,case.ledger_account)
    manual_extra_buy=manual_extra_sell=None;chain_inputs=[]
    if order_mode:
        at=datetime.fromisoformat(receipt['block_at'])
        for kind,quantity,price in [('BUY','0.02','2'),('SELL','0.01','4')]:
            with case.vault.session()as db:
                saved=append_event(db,case.ledger_account,str(uuid4()),EventInput(kind,'USDT',at,
                    asset='WBNB',quantity=Decimal(quantity),unit_price=Decimal(price)))
            if kind=='BUY':manual_extra_buy=saved.id
            else:manual_extra_sell=saved.id
        for item in financial_sources:
            value=_trade(item.trade_terms,item.receipt,datetime.fromisoformat(item.receipt['block_at']))
            chain_inputs.append({'quantity':str(value.quantity),'gross_amount':str(value.gross_amount)})
        if order_mode=='EXISTING':
            from app.portfolio import dex_receipt_order as orders,economic_receipt_acceptance as batches
            from app.portfolio.economic_receipt_source import ReceiptSelection
            for original in case.sources:
                scope={'context_account_id':case.account,'wallet_address':case.meta['owner'],
                    'evidence':case.observations[original.key],'now':case.at}
                with case.vault.session()as db:proposed=orders.preview_order(db,original.kind,original.id,**scope)
                with case.vault.session()as db:orders.confirm_order(db,original.kind,original.id,preview_digest=proposed['preview_digest'],**scope)
            selections=[ReceiptSelection(item.kind,item.id,case.account,case.meta['owner'],
                trade_event_id=manual_trade if item.kind=='WALLET_SWAP'else None,
                gas_event_ids=(manual_gas,)if item.kind=='WALLET_SWAP'else (),
                confirm_new_trade=item.kind!='WALLET_SWAP',confirm_new_gas=item.kind!='WALLET_SWAP')for item in case.sources]
            options={'batch_id':str(uuid4()),'recorded_at':max(datetime.now(timezone.utc),case.at)+timedelta(seconds=1),'wallet_addresses':[case.meta['owner']]}
            proposal=batches.preview_receipt_acceptance(case.vault,case.ledger_account,selections,**options)
            batches.confirm_receipt_acceptance(case.vault,case.ledger_account,selections,**options,
                preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=1))
        with case.vault.session()as db:originals=list_original_events(db,case.ledger_account)
    secondary_id=None;secondary_inputs=None;secondary_checked_at=None
    if multi_mode:
        from tests.dex_order_fixture import finalized_wallet_order
        from tests.test_dex_operator import write
        from app.portfolio.wallet_links import save_wallet_link
        alternate={**case.meta,'owner':case.meta['operator']}
        write(case.rpc,case.meta,case.meta['usdt'],'mint(address,uint256)',['address','uint256'],[alternate['owner'],10**18])
        write(case.rpc,alternate,case.meta['usdt'],'approve(address,uint256)',['address','uint256'],[case.meta['router'],10**18])
        with case.vault.session()as db:
            save_wallet_link(db,case.ledger_account,alternate['owner'],'0x38','Secundaria');db.commit()
        def wallet_now():
            block=case.rpc('eth_getBlockByNumber',['latest',False])
            return max(datetime.now(timezone.utc),case.at,datetime.fromtimestamp(int(block['timestamp'],16),timezone.utc))+timedelta(seconds=2)
        patch.setattr('tests.test_dex_call_resolution.now',wallet_now)
        patch.setattr('tests.dex_order_fixture.now',wallet_now)
        secondary_case=(case.vault,case.account,case.ledger_account,alternate,case.rpc,None,None,None)
        secondary,observation=finalized_wallet_order(secondary_case);secondary_id=secondary.id;secondary_checked_at=wallet_now()
        with case.vault.session()as db:
            all_sources=[*financial_sources,*_sources(db,alternate['owner'],_identity(db,'wallet',alternate['owner']))]
        secondary_inputs=[]
        for item in all_sources:
            value=_trade(item.trade_terms,item.receipt,datetime.fromisoformat(item.receipt['block_at']))
            secondary_inputs.append({'quantity':str(value.quantity),'gross_amount':str(value.gross_amount),'gas_wei':item.receipt['gas_fee_wei']})
    case.vault.lock()
    start=datetime.now(timezone.utc);clock_base=max(start,case.at,secondary_checked_at or start)+timedelta(seconds=2)
except BaseException:stack.close();raise

def now():return clock_base+(datetime.now(timezone.utc)-start)
def supervisor(v,c):
    result=DexBotSupervisor(v,c,clock=now);workers.append(result);return result
@contextmanager
def reader():
    def read(method,params):
        if any(value in method.lower()for value in ('send','sign')):raise AssertionError('Read-only accounting RPC')
        reads.append(method);return case.rpc(method,params)
    yield read
@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:
        for item in workers:item.shutdown();item.join(timeout=5);item.vault.lock()
        stack.close()

app=FastAPI(lifespan=lifespan)
app.include_router(create_real_router(case.vault.path,dex_rpc_factory=reader,dex_bot_supervisor_factory=supervisor))
register_ui(app)
from app.marketdata import service as market_service
def offline(*args,**kwargs):raise HTTPException(400,'No external market in owned accounting fixture')
market_service.candles=offline

@app.get('/__test/state')
def state():
    return {'fixture_id':base.name,'wallet':case.meta['owner'],'account_id':case.account,'ledger_id':case.ledger_account,
        'manual_trade':manual_trade,'manual_gas':manual_gas,'wallet_source_id':source.id,'clock_ms':int(now().timestamp()*1000),
        'order_mode':order_mode,'manual_extra_buy':manual_extra_buy,'manual_extra_sell':manual_extra_sell,'chain_inputs':chain_inputs,
        **({'secondary_wallet':case.meta['operator'],'secondary_source_id':secondary_id,'economic_inputs':secondary_inputs}if multi_mode else {})}

class Call(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    fixture_id:str
    method:str
    params:list=[]

@app.post('/__test/wallet-rpc')
def wallet_rpc(body:Call):
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    assert case.rpc('orbis_fixture_identity')=={'id':case.meta['fixtureId'],'chain':56}
    if body.method not in {'eth_chainId','eth_getBalance','eth_blockNumber','eth_getBlockByNumber','eth_getCode','eth_getTransactionCount','eth_gasPrice','eth_call'}:
        raise HTTPException(400,'Read-only fixture wallet')
    wallet_reads.append(body.method);return {'result':case.rpc(body.method,body.params)}

@app.get('/__test/book')
def book():
    vault=workers[0].vault
    with vault.session()as db:
        snapshot=account_snapshot(db,case.ledger_account)
        original_map={item.id:item for item in list_original_events(db,case.ledger_account)}
        return {'events':len(original_map),'originals_preserved':all(original_map.get(item.id)==item for item in originals),
            'cash':{k:str(v)for k,v in snapshot.cash.items()},'contributions':{k:str(v)for k,v in snapshot.contributions.items()},
            'positions':{asset:str(value.quantity)for (asset,quote),value in snapshot.positions.items()},
            'cost_basis':{asset:str(value.cost_basis)for (asset,quote),value in snapshot.positions.items()},
            'realized_pnl':{asset:str(value.realized_pnl)for (asset,quote),value in snapshot.positions.items()},
            'order_proofs':db.scalar(select(func.count()).select_from(m.RealDexReceiptOrderProof)),
            'acceptances':db.scalar(select(func.count()).select_from(m.RealEconomicAcceptance)),
            'accepted_ids':[row.id for row in db.scalars(select(m.RealEconomicAcceptance))],
            'accepted_payloads':{row.id:row.payload_digest for row in db.scalars(select(m.RealEconomicAcceptance))},
            'operator_bookings':db.scalar(select(func.count()).select_from(m.RealDexReceiptBooking)),
            'wallet_bookings':db.scalar(select(func.count()).select_from(m.RealWalletSwapBooking)),
            'rpc_reads':len(reads),'wallet_reads':len(wallet_reads)}
