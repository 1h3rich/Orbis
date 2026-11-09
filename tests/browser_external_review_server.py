"""Only owned SQLCipher + actual signed Anvil transfers for the full local UI."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal as D
from uuid import uuid4
import json,os

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict
from sqlalchemy import select,func

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.api import create_real_router
from app.portfolio import models as m
from app.portfolio.repository import account_snapshot,list_original_events,create_account,append_event
from app.portfolio.domain import EventInput
from app.portfolio.wallet_links import save_wallet_link
from app.web.mount import register_ui
from tests.external_batch_fixture import batch_case
from tests.test_dex_external_observation import external_case

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(parents=True,exist_ok=True)
stack=ExitStack();mode=os.environ.get('ORBIS_TEST_EXTERNAL_MODE','BATCH');workers=[];reads=[]
if mode=='CAPTURE':
    raw=stack.enter_context(external_case(base,'SELF'))
    with raw.vault.session()as db:
        peer=create_account(db,'Secundaria','USDT').id
        save_wallet_link(db,raw.account,raw.meta['owner'],'0x38','Principal');db.commit()
    at=datetime.fromtimestamp(int(raw.evidence['block']['timestamp'],16),timezone.utc)-timedelta(hours=1)
    for currency,amount in(('USDT','100'),('BNB','1')):
        with raw.vault.session()as db:append_event(db,raw.account,str(uuid4()),EventInput('CONTRIBUTION',currency,at,amount=D(amount)))
    case=SimpleNamespace(vault=raw.vault,account=raw.account,peer=peer,meta=raw.meta,recorded_at=raw.now,entries=(SimpleNamespace(current=raw),))
else:case=stack.enter_context(batch_case(base))
with case.vault.session()as db:originals={account:list_original_events(db,account)for account in(case.account,case.peer)}
case.vault.lock();started=datetime.now(timezone.utc);stamp=max(started,case.recorded_at)+timedelta(seconds=2)
def now():return stamp+(datetime.now(timezone.utc)-started)
def supervisor(v,c):
    result=DexBotSupervisor(v,c,clock=now);workers.append(result);return result
@contextmanager
def reader():
    def rpc(method,params):
        if 'send'in method.lower()or'sign'in method.lower():raise AssertionError('Read-only browser fixture')
        reads.append(method);return case.entries[0].current.rpc(method,params)
    yield rpc
@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:
        for w in workers:w.shutdown();w.join(timeout=5);w.vault.lock()
        stack.close()
app=FastAPI(lifespan=lifespan);app.include_router(create_real_router(case.vault.path,dex_rpc_factory=reader,dex_bot_supervisor_factory=supervisor));register_ui(app)

@app.get('/__test/state')
def state():return {'fixture_id':base.name,'wallet':case.meta['owner'],'secondary_wallet':case.meta['operator'],
    'account_id':case.account,'peer_id':case.peer,'source_ids':[item.current.source.id for item in case.entries],
    'clock_ms':int(now().timestamp()*1000),'hashes':[item.current.hash for item in case.entries],'mode':mode}

class Call(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    fixture_id:str
    method:str
    params:list=[]
@app.post('/__test/wallet-rpc')
def wallet_rpc(body:Call):
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    if body.method not in{'eth_chainId','eth_getBalance','eth_blockNumber','eth_getBlockByNumber','eth_getCode','eth_getTransactionCount','eth_gasPrice','eth_call'}:raise HTTPException(400,'Read-only fixture')
    return {'result':case.entries[0].current.rpc(body.method,body.params)}

@app.get('/__test/book')
def book():
    with workers[0].vault.session()as db:
        snapshots={account:account_snapshot(db,account)for account in originals}
        return {'events':db.scalar(select(func.count()).select_from(m.RealEvent)),
            'acceptances':db.scalar(select(func.count()).select_from(m.RealEconomicAcceptance)),
            'groups':db.scalar(select(func.count()).select_from(m.RealDexExternalAcceptanceGroup)),
            'books':db.scalar(select(func.count()).select_from(m.RealDexExternalBooking)),
            'originals_preserved':all({event.id:event for event in list_original_events(db,account)}.get(old.id)==old for account,events in originals.items()for old in events),
            'positions':{account:{asset:{'quantity':str(position.quantity),'cost':str(position.cost_basis)}for(asset,_),position in value.positions.items()}for account,value in snapshots.items()},
            'cash':{account:{asset:str(amount)for asset,amount in value.cash.items()}for account,value in snapshots.items()},'rpc_reads':len(reads)}
