"""Only owned localhost Anvil wallets and encrypted test accounts; no mainnet."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4
import json,os
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict
from app.portfolio.api import create_real_router
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.repository import append_event
from app.portfolio.domain import EventInput
from app.web.mount import register_ui
from tests.dex_wallet_cancellation_fixture import pending_funding
from tests.test_dex_gas_funding import now
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(parents=True,exist_ok=True);stack=ExitStack()
try:
    values,source,original_hash=stack.enter_context(pending_funding(base))
    vault,account,manager,draft,meta,rpc,_,binding=values
    backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
    with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
    with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',now()-timedelta(days=1),amount=Decimal('1')))
    (base/'operator-backup.json').write_text(json.dumps(backup));manager.revoke();vault.lock()
except BaseException:stack.close();raise
os.environ['ORBIS_BSC_RPC_URL']=meta['rpc']
release=Event();mode='MINE';wallet_calls=[];cancels=[];fundings=[]
initial_value=sum(int(rpc('eth_getBalance',[address,'latest']),16) for address in (meta['owner'],draft['operator_address']))

def freshen():
    # Once our cancellation consumed the original nonce, mine genuine empty
    # blocks to keep subsequent public observations fresh while the wallet's
    # HTTP response is held. Never alter receipt/transaction JSON or signatures.
    if cancels:
        pending=int(rpc('eth_getTransactionCount',[meta['owner'],'pending']),16)
        latest=int(rpc('eth_getTransactionCount',[meta['owner'],'latest']),16)
        block=rpc('eth_getBlockByNumber',['latest',False])
        if pending==latest and int(now().timestamp())-int(block['timestamp'],16)>2:rpc('evm_mine',[])

@contextmanager
def reader():
    freshen()
    def read(method,params):
        if method not in {'eth_chainId','eth_getBalance','eth_getCode','eth_getBlockByNumber','eth_getTransactionCount',
                'eth_call','eth_getTransactionByHash','eth_getTransactionReceipt'}:raise ValueError('Read-only backend fixture')
        return rpc(method,params)
    yield read

@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:release.set();stack.close()

app=FastAPI(lifespan=lifespan);app.include_router(create_real_router(vault.path,dex_rpc_factory=reader));register_ui(app)

@app.get('/__test/state')
def state():
    freshen();fees=[]
    for tx_hash in [*cancels,*fundings]:
        receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt:fees.append(str(int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16)))
    total=sum(int(rpc('eth_getBalance',[address,'latest']),16) for address in (meta['owner'],draft['operator_address']))
    return {'fixture_id':base.name,'wallet_cancellation_fixture':True,'wallet':meta['owner'],'operator':draft['operator_address'],
        'account_id':account,'binding_id':binding['id'],'funding_id':source['id'],'original_hash':original_hash,'mode':mode,
        'wallet_calls':len(wallet_calls),'cancel_sends':len(cancels),'funding_sends':len(fundings),'cancel_hashes':cancels,
        'funding_hashes':fundings,'fees_wei':fees,'total_balance_loss_wei':str(initial_value-total),
        'clock_ms':int(now().timestamp()*1000)}

class Call(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    fixture_id:str
    method:str
    params:list=[]

@app.post('/__test/control')
def control(body:Call):
    global mode
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    if body.method=='release':release.set()
    elif body.method in {'MINE','HOLD_MINED','REJECT','FUND_PENDING'}:mode=body.method;release.clear()
    elif body.method=='mine_original':rpc('miner_start',[1]);rpc('evm_mine',[])
    else:raise HTTPException(400,'Wrong command')
    return {'ok':True}

@app.post('/__test/wallet-rpc')
def wallet_rpc(body:Call):
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
    if body.method=='eth_sendTransaction':
        if len(body.params)!=1:raise HTTPException(400,'One own fixture transaction only')
        tx=body.params[0];value=int(tx['value'],16)
        if (tx['from'].lower()!=meta['owner'].lower() or tx['chainId']!='0x38' or tx['data']!='0x'
                or int(tx['gas'],16)!=21000 or not 0<int(tx['gasPrice'],16)<=3*10**9):raise HTTPException(400,'Wrong fixture scope')
        own_cancel=value==0 and tx['to'].lower()==meta['owner'].lower()
        own_funding=value==5*10**14 and tx['to'].lower()==draft['operator_address']
        if not (own_cancel or own_funding):raise HTTPException(400,'Only own cancellation/funding fixture')
        wallet_calls.append({'cancel':own_cancel,'nonce':int(tx['nonce'],16)})
        if own_cancel and mode=='REJECT':return {'error':{'code':4001,'message':'Controlled user wallet rejection'}}
        if own_funding:rpc('miner_stop',[])
        tx_hash=rpc('eth_sendTransaction',[tx])
        if own_cancel:
            cancels.append(tx_hash);rpc('miner_start',[1]);rpc('evm_mine',[])
            if mode=='HOLD_MINED' and not release.wait(60):return {'error':{'code':-32000,'message':'Controlled held wallet reply timed out'}}
        else:fundings.append(tx_hash)
        return {'result':tx_hash}
    if body.method not in {'eth_chainId','eth_getBalance','eth_getCode','eth_blockNumber','eth_getBlockByNumber','eth_getTransactionCount',
            'eth_call','eth_gasPrice','eth_estimateGas','eth_getTransactionByHash','eth_getTransactionReceipt'}:raise HTTPException(400,'Wrong read')
    freshen();return {'result':rpc(body.method,body.params)}
