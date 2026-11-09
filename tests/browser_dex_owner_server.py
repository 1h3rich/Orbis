"""Owner setup on an identified disposable EVM, never production RPC/wallet."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
from pathlib import Path
import os
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from tests.dex_bot_source_fixture import SourceCase
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);(base/'source').mkdir(exist_ok=True,parents=True)
stack=ExitStack()
try:
    meta,rpc=stack.enter_context(evm(base/'evm'))
    source=SourceCase(base/'source',research=True);vault,account,now=setup(base);vault.lock()
except BaseException:
    stack.close();raise
reads=[];sends=[];replacement_hash=None
cancel_fixture=os.environ.get('ORBIS_TEST_DEX_CANCEL')=='1'
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
    finally:source.close();vault.lock();stack.close()
app=FastAPI(lifespan=lifespan)
app.include_router(create_real_router(vault.path,spot_paper=source.paper,dex_rpc_factory=factory));register_ui(app)
@app.get('/__test/state')
def state():return {'fixture_id':base.name,'wallet':meta['owner'],'operator':meta['operator'],
    'account_id':account,'reads':len(reads),'sends':len(sends),'cancel_fixture':cancel_fixture,'replacement_hash':replacement_hash}
class Call(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fixture_id:str
    method:str
    params:list=[]
@app.post('/__test/wallet-rpc')
def wallet_call(body:Call):
    global replacement_hash
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
    if body.method not in {'eth_chainId','eth_getBalance','eth_blockNumber','eth_getBlockByNumber','eth_getCode',
            'eth_getTransactionCount','eth_gasPrice','eth_estimateGas','eth_sendTransaction'}:raise HTTPException(400,'Wrong method')
    if body.method=='eth_sendTransaction':
        assert len(body.params)==1 and body.params[0]['from'].lower()==meta['owner'].lower()
        assert int(body.params[0].get('value','0x0'),16)==0
        if cancel_fixture and replacement_hash is None and body.params[0].get('to') is None:
            requested=body.params[0]
            cancellation={'from':meta['owner'],'to':meta['owner'],'nonce':requested['nonce'],'gas':'0x5208',
                'gasPrice':requested['gasPrice'],'value':'0x0','data':'0x'}
            replacement_hash=rpc('eth_sendTransaction',[cancellation]);sends.append(cancellation)
            return {'result':replacement_hash}
        sends.append(body.params[0])
    return {'result':rpc(body.method,body.params)}
