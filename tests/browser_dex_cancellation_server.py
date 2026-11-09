"""Only identified, disposable Anvil accounts; no user vault or mainnet IO."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
import json,os
from types import SimpleNamespace
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict
from app.live_execution.dex_gas_cancellation_transport import BnbGasCancellationSender
from app.portfolio.api import create_real_router
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.repository import append_event
from app.portfolio.domain import EventInput
from app.web.mount import register_ui
from tests.dex_gas_cancellation_fixture import pending_return
from tests.test_dex_gas_funding import now
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(exist_ok=True,parents=True);stack=ExitStack()
try:
    values,original,original_hash,_=stack.enter_context(pending_return(base))
    vault,account,manager,draft,meta,rpc,_,binding=values
    backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
    with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
    with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',now()-timedelta(days=1),amount=Decimal('1')))
    (base/'operator-backup.json').write_text(json.dumps(backup));manager.revoke();vault.lock()
except BaseException:stack.close();raise
os.environ['ORBIS_BSC_RPC_URL']=meta['rpc'];posts=[]

@contextmanager
def sender(plan):
    with BnbGasCancellationSender(plan) as actual:
        def send(raw):
            posts.append(plan.gas_price)
            if len(posts)==1:raise TimeoutError('Controlled request did not arrive at owned fixture')
            result=actual.send(raw);rpc('miner_start',[1]);rpc('evm_mine',[]);return result
        yield SimpleNamespace(send=send)

@contextmanager
def reader():yield rpc

@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:stack.close()

app=FastAPI(lifespan=lifespan)
app.include_router(create_real_router(vault.path,dex_rpc_factory=reader,dex_cancel_sender_factory=sender));register_ui(app)

@app.get('/__test/state')
def state():return {'fixture_id':base.name,'cancellation_fixture':True,'wallet':meta['owner'],'account_id':account,
    'binding_id':binding['id'],'return_id':original['id'],'original_hash':original_hash,
    'cancel_posts':len(posts),'prices':posts,'operator_balance_wei':str(int(rpc('eth_getBalance',[draft['operator_address'],'latest']),16)),
    'clock_ms':int(now().timestamp()*1000)}

class Call(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    fixture_id:str
    method:str
    params:list=[]

@app.post('/__test/wallet-rpc')
def wallet_rpc(body:Call):
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
    if body.method not in {'eth_chainId','eth_getBalance','eth_blockNumber','eth_getBlockByNumber','eth_getCode',
            'eth_getTransactionCount','eth_gasPrice','eth_estimateGas'}:raise HTTPException(400,'Read-only wallet fixture')
    return {'result':rpc(body.method,body.params)}
