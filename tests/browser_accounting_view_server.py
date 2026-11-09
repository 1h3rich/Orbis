"""Own encrypted accepted book, live portfolio API, offline prices, no orders."""
from contextlib import ExitStack,asynccontextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
import os

from fastapi import FastAPI
from app.portfolio import api as portfolio_api
from app.portfolio import valuation_history
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event,create_account,transfer_asset_between_accounts,list_events
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.web.mount import register_ui
from tests.test_accounting_consumers import corrected_case
from tests.test_wallet_swap import PASSWORD
from uuid import uuid4

base=Path(os.environ['ORBIS_TEST_EXECUTION_DIRECTORY']);base.mkdir(parents=True,exist_ok=True)
stack=ExitStack();held={};workers=[]
def transfer(case,at):
    vault,_,owner,*_=case
    with vault.session() as db:
        target=create_account(db,'Destino aceptado','USDT');db.commit();held['target']=target.id
    with vault.session() as db:
        transfer_asset_between_accounts(db,owner,target.id,str(uuid4()),asset='WBNB',quote_currency='USDT',
            quantity=Decimal('0.5'),occurred_at=at+timedelta(seconds=1))
    return at+timedelta(seconds=1)
try:
    case,at,sale,_,_,_,accepted,_=stack.enter_context(corrected_case(base,before_accept=transfer))
    vault,_,owner,*_=case
    with vault.session() as db:
        owner_events=[portfolio_api._event_payload(event)for event in list_events(db,owner)]
        target_events=[portfolio_api._event_payload(event)for event in list_events(db,held['target'])]
        history_account=create_account(db,'Evolución propia','EUR').id
        long_account=create_account(db,'Evolución de diez años','EUR').id
        midnight=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
        first=midnight-timedelta(days=4)
        older=midnight-timedelta(days=3670)
        for event in (
            EventInput('CONTRIBUTION','EUR',first,amount=Decimal('100')),
            EventInput('BUY','EUR',first+timedelta(seconds=1),'BTC',Decimal('1'),Decimal('20')),
            EventInput('WITHDRAWAL','EUR',first+timedelta(days=2),amount=Decimal('10'),fee=Decimal('1')),
        ):
            append_event(db,history_account,str(uuid4()),event)
        for event in (
            EventInput('CONTRIBUTION','EUR',older,amount=Decimal('1000')),
            EventInput('BUY','EUR',older+timedelta(seconds=1),'BTC',Decimal('1'),Decimal('100')),
        ):
            append_event(db,long_account,str(uuid4()),event)
    assert target_events[0]['gross_amount']!=target_events[0]['accounting']['gross_amount']=='1.5'
    vault.lock()
except BaseException:stack.close();raise

def supervisor(v,c):
    item=DexBotSupervisor(v,c);workers.append(item);return item
def forbidden_rpc():raise AssertionError('Accounting display cannot access signing, orders or RPC')
def price(*args,**kwargs):
    return {'price':'10','source':'owned-offline','as_of':datetime.now(timezone.utc).isoformat(),'stale':False}
portfolio_api.get_configured_price_with_meta=price
def daily(symbol, *, provider, timeframe, limit, since=None, before=None):
    assert symbol=='BTC/EUR' and timeframe=='1d'
    if before is not None:
        assert since is None and provider=='bitstamp' and 1<=limit<=1000
        missing=int((midnight-timedelta(days=2000)).timestamp())
        return {'available':True,'symbol':symbol,'resolved_provider':provider,'timeframe':timeframe,'candles':[
            {'t':stamp,'c':'125.1234567890123456789','closed':True}
            for stamp in range(before-limit*86400,before,86400) if stamp!=missing]}
    assert since is not None
    prices=[(0,'30'),(2,'40'),(3,'50'),(4,'99999')]
    return {'available':True,'symbol':symbol,'resolved_provider':provider,'timeframe':timeframe,'candles':[
        {'t':int((first+timedelta(days=offset)).timestamp()),'c':price,'closed':offset<4}
        for offset,price in prices]}
valuation_history.candles=daily

@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:
        for item in workers:item.shutdown();item.join(timeout=5);item.vault.lock()
        stack.close()

app=FastAPI(lifespan=lifespan)
def no_market_db():yield None
app.dependency_overrides[portfolio_api.get_db]=no_market_db
app.include_router(portfolio_api.create_real_router(vault.path,dex_rpc_factory=forbidden_rpc,dex_bot_supervisor_factory=supervisor))
register_ui(app)

@app.get('/__test/state')
def state():
    return {'fixture_id':base.name,'password':PASSWORD,'owner_account':owner,'target_account':held['target'],
        'sale_id':sale.id,'accepted_id':accepted['id'],'owner_events':owner_events,'target_events':target_events,
        'history_account':history_account,'long_history_account':long_account,'history_first_day':first.date().isoformat()}
