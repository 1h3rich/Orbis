"""Owned signed permission/swap receipts; HTTP/UI never submit transactions."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import os

from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict
from sqlalchemy import select,func
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.api import create_real_router
from app.portfolio import models as m
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event,account_snapshot,list_original_events
from app.portfolio.wallet_swap_booking import _trade
from app.web.mount import register_ui
from tests.test_economic_gas_acceptance import ready
from tests.dex_order_fixture import finalized_wallet_order

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(parents=True,exist_ok=True)
booked=os.environ.get('ORBIS_TEST_ECONOMIC_GAS_BOOKED')=='1'
stack=ExitStack();workers=[];reads=[];wallet_reads=[]
try:
    case,stored=stack.enter_context(ready(base,booked=booked))
    manual_trade=manual_gas=trade_id=None
    if not booked:
        values=(case.vault,case.account,case.target,case.meta,case.rpc,None,None,None)
        trade_source,_=finalized_wallet_order(values);trade_id=trade_source.id
        from app.portfolio.dex_call_booking import _wallet_source
        from app.portfolio.wallet_swap import swap_record
        with case.vault.session() as db:
            row,terms,state=swap_record(db,trade_id);source=_wallet_source(db,row,terms,state,case.meta['owner'])
        at=datetime.fromisoformat(source.receipt['block_at'])
        trade=replace(_trade(source.trade_terms,source.receipt,at),occurred_at=at+timedelta(minutes=1))
        with case.vault.session() as db:manual_trade=append_event(db,case.target,str(uuid4()),trade).id
        with case.vault.session() as db:
            manual_gas=append_event(db,case.target,str(uuid4()),EventInput('FEE','BNB',trade.occurred_at,
                amount=Decimal('1E-18'),note='Owned partial permission gas')).id
    with case.vault.session() as db:
        gas_proof=db.scalar(select(m.RealDexGasOrderProof).where(m.RealDexGasOrderProof.resolution_id==case.id))
        assert gas_proof is not None
        db.delete(gas_proof);db.commit()
        originals=list_original_events(db,case.target)
    case.vault.lock();start=datetime.now(timezone.utc);clock_base=max(start,case.scope['now'])+timedelta(seconds=3)
except BaseException:stack.close();raise

def now():return clock_base+(datetime.now(timezone.utc)-start)
def supervisor(vault,connection):
    item=DexBotSupervisor(vault,connection,clock=now);workers.append(item);return item
@contextmanager
def reader():
    def read(method,params):
        assert not any(part in method.lower()for part in ('send','sign')),method
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
def offline(*args,**kwargs):raise HTTPException(400,'No external market in owned gas fixture')
market_service.candles=offline

@app.get('/__test/state')
def state():
    return {'fixture_id':base.name,'wallet':case.meta['owner'],'account_id':case.account,'ledger_id':case.target,
        'gas_id':case.id,'trade_id':trade_id,'manual_trade':manual_trade,'manual_gas':manual_gas,
        'booked':booked,'clock_ms':int(now().timestamp()*1000)}

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
    with workers[0].vault.session() as db:
        current=account_snapshot(db,case.target);events={event.id:event for event in list_original_events(db,case.target)}
        return {'events':len(events),'initial_events':len(originals),'originals_preserved':all(events.get(e.id)==e for e in originals),
            'cash':{k:str(v)for k,v in current.cash.items()},'positions':{a:str(p.quantity)for(a,q),p in current.positions.items()},
            'contributions':{k:str(v)for k,v in current.contributions.items()},
            'gas_proofs':db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof)),
            'trade_proofs':db.scalar(select(func.count()).select_from(m.RealDexReceiptOrderProof)),
            'acceptances':db.scalar(select(func.count()).select_from(m.RealEconomicAcceptance)),
            'gas_bookings':db.scalar(select(func.count()).select_from(m.RealDexReceiptBooking)),
            'wallet_bookings':db.scalar(select(func.count()).select_from(m.RealWalletSwapBooking)),
            'rpc_reads':len(reads)}
