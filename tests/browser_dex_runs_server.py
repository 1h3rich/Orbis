"""Identified disposable EVM with tested origin, approval and gas already set."""
from contextlib import ExitStack,asynccontextmanager,nullcontext
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,os

from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.live_execution.dex_operator_transport import BnbOperatorSender
from app.live_execution.dex_gas_return_transport import BnbGasReturnSender
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from tests.dex_bot_worker_fixture import running_case
from tests.dex_dca_worker_fixture import dca_running_case
from tests.dex_bot_source_fixture import DEX_EMA_RULE
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(parents=True,exist_ok=True)
stack=ExitStack()
try:
    dca=os.environ.get('ORBIS_TEST_DEX_DCA')=='1'
    dca_average=os.environ.get('ORBIS_TEST_DEX_DCA_AVERAGE')=='1'
    dca_volume=os.environ.get('ORBIS_TEST_DEX_DCA_VOLUME')=='1'
    recovered_fixture=os.environ.get('ORBIS_TEST_DEX_RECOVERED_RENEWAL')=='1'
    recovered_context=None;initial_run=None
    operational=os.environ.get('ORBIS_TEST_DEX_OPERATIONAL')=='1'
    order_proof_fixture=os.environ.get('ORBIS_TEST_DEX_ORDER_PROOF')=='1'
    changes={'expires_at':int(datetime.now(timezone.utc).timestamp())+7200} if operational else None
    if recovered_fixture:
        from pytest import MonkeyPatch
        from tests.dex_recovered_renewal_fixture import recovered_case
        patch=MonkeyPatch();stack.callback(patch.undo)
        recovered_context=stack.enter_context(recovered_case(base,patch,dca=dca,research=True))
        values,initial_run,_,_,clock,mine,market=recovered_context['case']
    else:
        case=dca_running_case(base,activate=False,research=True,average_rule=DEX_EMA_RULE if dca_average else None,volume=dca_volume) if dca else running_case(base,activate=False,research=True,
            draft_ttl_seconds=3600 if operational else 900,policy_changes=changes,
            evm_backend='anvil' if order_proof_fixture else None)
        values,_,_,_,clock,mine,market=stack.enter_context(case)
    vault,account,manager,draft,meta,rpc,_,binding=values
    from tests.mexc_research_fixture import historical_reserve
    research_source=market.research_source
    extra_reserve=historical_reserve(research_source.lab,research_source.version['version_id'],
        before_t=research_source.dataset['start_t']-(86400 if dca else 60),symbol='BNB/USDT',timeframe='1d' if dca else '1m',
        automation=research_source.store if dca else None)
    booking_fixture=os.environ.get('ORBIS_TEST_DEX_BOOKING')=='1'
    if booking_fixture:
        from app.portfolio.wallet_links import save_wallet_link
        from app.portfolio.repository import append_event
        from app.portfolio.domain import EventInput
        from decimal import Decimal
        from uuid import uuid4
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Wallet propia');db.commit()
        for currency,amount in [('USDT','100'),('BNB','0.01')]:
            with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION',currency,clock[0]-timedelta(days=1),amount=Decimal(amount)))
    backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=clock[0])
    (base/'operator-backup.json').write_text(json.dumps(backup));manager.revoke();vault.lock()
    os.environ['ORBIS_BSC_RPC_URL']=meta['rpc']
except BaseException:stack.close();raise
posts=[];supervisors=[];owner_posts=[];native_posts=[]
renewal=os.environ.get('ORBIS_TEST_DEX_RENEWAL')=='1'
returning=os.environ.get('ORBIS_TEST_DEX_RETURN')=='1'
charting=os.environ.get('ORBIS_TEST_DEX_CHART')=='1'

def return_sender(plan):
    actual=BnbGasReturnSender(plan,clock=lambda:clock[0]);original=actual.send
    def send(raw):
        assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
        result=original(raw);native_posts.append(result);return result
    actual.send=send;return actual

def factory(v,c):
    def sender(plan):
        actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
        def send(raw):
            assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
            result=original(raw);posts.append({'buy':plan.buy,'hash':result});return result
        actual.send=send;return actual
    result=DexBotSupervisor(v,c,clock=lambda:clock[0],market_factory=lambda:market,interval_seconds=.2,
        worker_kwargs={'market':market,'rpc_factory':lambda:nullcontext(rpc),'sender_factory':sender})
    supervisors.append(result);return result

@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:
        for supervisor in supervisors:supervisor.shutdown();supervisor.join(timeout=5)
        stack.close()

app=FastAPI(lifespan=lifespan)
app.include_router(create_real_router(vault.path,**({'dca_paper':market.research_source.paper} if dca else {'spot_paper':market.research_source.paper}),dex_rpc_factory=lambda:nullcontext(rpc),dex_bot_supervisor_factory=factory,
    dex_return_sender_factory=return_sender if returning else None));register_ui(app)
if dca_average or dca_volume:
    from app.automation.api import create_automation_router
    from app.automation.paper_dca import create_paper_dca_router
    from app.lab.api import create_lab_router
    paper=research_source.paper
    app.include_router(create_automation_router(paper.store.path,paper.lab.path))
    app.include_router(create_paper_dca_router(paper.store.path,paper.lab.path,paper=paper))
    app.include_router(create_lab_router(paper.lab.path))

@app.get('/__test/state')
def state():return {'fixture_id':base.name,'wallet':meta['owner'],'account_id':account,'binding_id':binding['id'],
    'extra_reserve_id':extra_reserve['id'],
    'initial_run_id':initial_run['id'] if initial_run else None,'primary_reserve_id':research_source.validation['id'],
    'recovered_fixture':recovered_fixture,'dca_fixture':dca,
    **({'paper_session_id':research_source.session['id']} if dca_average or dca_volume else {}),
    'clock_ms':int(clock[0].timestamp()*1000),'booking_fixture':booking_fixture,'order_proof_fixture':order_proof_fixture,
    'sends':len(posts),'buys':sum(item['buy'] for item in posts),'sells':sum(not item['buy'] for item in posts),'wallet_sends':len(owner_posts),
    'native_returns':len(native_posts),'return_fixture':returning,
    'operator_balance_wei':str(int(rpc('eth_getBalance',[draft['operator_address'],'latest']),16)),
    'owner_balance_wei':str(int(rpc('eth_getBalance',[meta['owner'],'latest']),16))}

@app.get('/__test/workers-idle')
def workers_idle():
    workers=[]
    for supervisor in supervisors:
        with supervisor._lock:workers.extend(supervisor._workers.values())
    return {'idle':not any(worker.alive for worker in workers)}

class Call(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fixture_id:str
    method:str
    params:list=[]

@app.post('/__test/wallet-rpc')
def wallet_call(body:Call):
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
    if body.method=='eth_sendTransaction' and renewal:
        from app.live_execution.dex_artifact import executor_address
        tx=body.params[0]
        assert len(body.params)==1 and tx['from'].lower()==meta['owner'].lower()
        assert tx['to'].lower()==executor_address(meta['owner'],int(values[6]['nonce'])).lower()
        assert int(tx.get('value','0x0'),16)==0
        result=rpc(body.method,body.params);owner_posts.append(result)
        return {'result':result}
    if body.method not in {'eth_chainId','eth_getBalance','eth_blockNumber','eth_getBlockByNumber','eth_getCode',
            'eth_getTransactionCount','eth_gasPrice','eth_estimateGas'}:raise HTTPException(400,'Read-only fixture wallet')
    return {'result':rpc(body.method,body.params)}

@app.post('/__test/advance')
def advance(body:Call):
    if body.fixture_id!=base.name or body.method not in {'entry','exit','tick'}:raise HTTPException(400,'Wrong fixture advance')
    if not dca:
        clock[0]+=timedelta(seconds=10 if body.method=='tick' else 1)
        if body.method=='exit':market.bid,market.ask='1.2','1.21'
        mine();return state()
    # Only this identified fixture may advance simulated time. Never jump
    # its clock/block midway through a production worker's read/decision.
    workers=[]
    for supervisor in supervisors:
        with supervisor._lock:workers.extend(supervisor._workers.values())
    with ExitStack() as held:
        for worker in workers:held.enter_context(worker._cycle_lock)
        clock[0]+=timedelta(seconds=10 if body.method=='tick' else 1);mine()
    for worker in workers:
        for _ in range(2):
            worker.run_once()
            # If its thread won the nonblocking cycle lock, wait for that
            # same cycle rather than advancing past an unobserved instant.
            with worker._cycle_lock:pass
    return state()


def chart_candles(symbol,*,provider,timeframe,limit,since=None,before=None):
    if not charting or provider!='mexc' or symbol!='BNB/USDT' or timeframe!=('1d' if dca else '1m'):
        raise HTTPException(400,'Wrong chart fixture market')
    seconds=86400 if dca else 60
    source=[{**bar,'closed':bar['t']+seconds<=int(clock[0].timestamp())} for bar in market.rows if bar['t']<=int(clock[0].timestamp())]
    source.sort(key=lambda bar:bar['t'])
    # Explicitly synthetic older OHLC belongs only to this fixture adapter.
    first=source[0]
    source=[{**first,'t':first['t']-seconds*i,'closed':True} for i in range(36,0,-1)]+source
    matching=[bar for bar in source if before is None or bar['t']<before]
    rows=matching[-min(limit,12):]
    return {'available':True,'resolved_provider':'mexc','symbol':symbol,'timeframe':timeframe,'candles':rows,
            'older_pagination_supported':True,'history_exhausted':len(matching)<=len(rows)}


# Replace the public adapter in this identified fixture process. An appended
# HTTP route would lose to register_ui's earlier route and reach the internet.
from app.marketdata import service as market_service
market_service.candles=chart_candles


@app.get('/__test/book')
def ledger_state():
    assert booking_fixture
    from app.portfolio.repository import account_snapshot,list_events
    from app.portfolio.models import RealDexReceiptBooking,RealDexReceiptOrderProof
    from sqlalchemy import select
    active=supervisors[0].vault
    assert active.unlocked
    with active.session() as db:
        book=account_snapshot(db,account)
        return {'bookings':len(list(db.scalars(select(RealDexReceiptBooking)))),
            'order_proofs':len(list(db.scalars(select(RealDexReceiptOrderProof)))),
            'cash':{key:str(value) for key,value in book.cash.items()},'contributions':{key:str(value) for key,value in book.contributions.items()},
            'positions':{asset:str(position.quantity) for (asset,quote),position in book.positions.items()},
            'events':len(list_events(db,account))}
