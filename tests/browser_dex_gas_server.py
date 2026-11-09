"""Identified disposable EVM + pre-bound bot for the native BNB wallet flow."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
import json,os
from types import SimpleNamespace
from uuid import uuid4
from datetime import timedelta
from decimal import Decimal
from eth_account import Account
from eth_utils import to_checksum_address
from pathlib import Path
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict

from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from tests.test_dex_gas_funding import case,now
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(exist_ok=True,parents=True)
stack=ExitStack()
native=os.environ.get('ORBIS_TEST_NATIVE_RESOLUTION')=='1';fixture_key=None
try:
    vault,account,manager,draft,meta,rpc,deployment,binding=stack.enter_context(case(base))
    backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
    if native:
        with manager._copy(draft['id'],account,meta['owner'],now()) as (_,key):fixture_key=bytes(key)
        from app.portfolio.wallet_links import save_wallet_link
        from app.portfolio.repository import append_event
        from app.portfolio.domain import EventInput
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',now()-timedelta(days=1),amount=Decimal('1')))
    (base/'operator-backup.json').write_text(json.dumps(backup));manager.revoke();vault.lock()
except BaseException:stack.close();raise
reads=[];sends=[];funding_cancel_hash=None;return_cancel_hash=None;return_attempts=[];native_returns=[]
if native:os.environ['ORBIS_BSC_RPC_URL']=meta['rpc']

@contextmanager
def return_sender(plan):
    from app.live_execution.dex_gas_return_transport import BnbGasReturnSender
    if not return_attempts:
        def lost(raw):
            assert Account.recover_transaction(raw).lower()==draft['operator_address']
            return_attempts.append(plan)
            raise TimeoutError('Own EVM fixture: original return did not arrive')
        yield SimpleNamespace(send=lost)
    else:
        with BnbGasReturnSender(plan) as actual:
            def send(raw):
                result=actual.send(raw);native_returns.append(result);return_attempts.append(plan);return result
            yield SimpleNamespace(send=send)

@contextmanager
def factory():
    def read(method,params):
        assert method in {'eth_chainId','eth_getTransactionCount','eth_getBalance','eth_getBlockByNumber',
            'eth_getCode','eth_call','eth_getTransactionByHash','eth_getTransactionReceipt'}
        reads.append(method);return rpc(method,params)
    yield read

@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:stack.close()

app=FastAPI(lifespan=lifespan)
app.include_router(create_real_router(vault.path,dex_rpc_factory=factory,dex_return_sender_factory=return_sender if native else None));register_ui(app)

@app.get('/__test/state')
def state():return {'fixture_id':base.name,'wallet':meta['owner'],'operator':draft['operator_address'],'account_id':account,
    'binding_id':binding['id'],'sends':len(sends),'reads':len(reads),'native_fixture':native,'funding_cancel_hash':funding_cancel_hash,
    'return_cancel_hash':return_cancel_hash,'return_attempts':len(return_attempts),'native_returns':len(native_returns),
    'operator_balance':str(int(rpc('eth_getBalance',[draft['operator_address'],'latest']),16))}

class Call(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fixture_id:str
    method:str
    params:list=[]

@app.post('/__test/wallet-rpc')
def wallet_call(body:Call):
    global funding_cancel_hash
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
    if body.method not in {'eth_chainId','eth_getBalance','eth_blockNumber','eth_getBlockByNumber','eth_getCode',
            'eth_getTransactionCount','eth_gasPrice','eth_estimateGas','eth_sendTransaction'}:raise HTTPException(400,'Wrong method')
    if body.method=='eth_sendTransaction':
        assert len(body.params)==1
        tx=body.params[0]
        assert tx['from'].lower()==meta['owner'].lower() and tx['to'].lower()==draft['operator_address']
        assert tx['data']=='0x' and 0<int(tx['value'],16)<=2*10**15 and int(tx['chainId'],16)==56
        sends.append(tx)
        if native and len(sends)==1:
            funding_cancel_hash=rpc('eth_sendTransaction',[{**tx,'to':meta['owner'],'value':'0x0'}])
            return {'result':funding_cancel_hash}
    return {'result':rpc(body.method,body.params)}

@app.post('/__test/native-cancel')
def native_cancel(body:Call):
    global return_cancel_hash
    if not native or body.fixture_id!=base.name or body.method!='cancel_return' or not return_attempts:raise HTTPException(400,'Wrong fixture')
    if return_cancel_hash is None:
        plan=return_attempts[0]
        tx={'chainId':56,'nonce':plan.snapshot.operator_nonce,'to':to_checksum_address(draft['operator_address']),
            'value':0,'data':'0x','gas':21000,'gasPrice':10**9}
        signed=Account.sign_transaction(tx,fixture_key)
        return_cancel_hash=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
    return {'hash':return_cancel_hash}
