"""Only owned Anvil/vaults: unresolved manual/bot call with a mined replacement."""
from contextlib import ExitStack,asynccontextmanager,contextmanager,nullcontext
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import uuid4
import os

from eth_account import Account
from eth_utils import to_checksum_address
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,ConfigDict

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.live_execution.dex_bot_worker import DexBotWorker
from app.portfolio.api import create_real_router
from app.portfolio import dex_call_resolution as resolution
from app.portfolio.domain import EventInput
from app.portfolio.models import RealDexOperatorIntent,RealDexReceiptOrderProof
from app.portfolio.repository import append_event,account_snapshot,list_events
from app.portfolio.wallet_links import save_wallet_link
from app.web.mount import register_ui
from tests.dex_evm_fixture import evm
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_call_resolution import prepare,now
from tests.test_dex_deployments import setup
from tests.test_dex_operator import write
from sqlalchemy import select

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);base.mkdir(parents=True,exist_ok=True)
kind=os.environ.get('ORBIS_TEST_CALL_KIND','WALLET_SWAP');outcome=os.environ.get('ORBIS_TEST_CALL_OUTCOME','EXECUTED')
original_wallet=os.environ.get('ORBIS_TEST_ORDER_WALLET_ORIGINAL')=='1'
assert not original_wallet or (kind=='WALLET_SWAP' and outcome=='EXECUTED')
assert kind in {'WALLET_SWAP','OPERATOR'} and outcome in {'EXECUTED','CANCELLED'}
stack=ExitStack();supervisors=[];reads=[];wallet_reads=[];held=Event();release=Event();gate=False
try:
    if kind=='OPERATOR':
        values,started,claim,connection,clock,mine,market=stack.enter_context(running_case(base,evm_backend='anvil',policy_changes={'max_gas_price':str(2*10**9)}))
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        os.environ['ORBIS_BSC_RPC_URL']=meta['rpc']
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
        assert worker.run_once()['pending_receipt']
        with vault.session() as db:
            raw=db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==binding['grant_id']))
            _,terms,_,source=resolution.source_record(db,'OPERATOR',raw.id,owner=meta['owner'])
        tx=source.expected_call();fields={'to':to_checksum_address(tx['to']),'data':tx['data'],'value':0,'chainId':56,
            'nonce':source.nonce,'gas':int(terms['gas'])+1000,'gasPrice':1200000000}
        if outcome=='CANCELLED':fields.update(to=to_checksum_address(draft['operator_address']),data='0x',gas=21000)
        with manager._copy(draft['id'],account,meta['owner'],clock[0]) as (_,key):signed=Account.sign_transaction(fields,bytes(key))
        tx_hash=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
        clock[0]+=timedelta(seconds=2);mine();rpc('miner_start',[1])
        connection.revoke();manager.revoke()  # saved run stays ACTIVE; browser must stop it
    else:
        vault,account,_=setup(base);meta,rpc=stack.enter_context(evm(base/'evm',backend='anvil'))
        if outcome=='EXECUTED':
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
            for token in ('usdt','wbnb'):
                for recipient in (meta['owner'],meta['pool']):write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**18])
            write(rpc,{**meta,'operator':meta['owner']},meta['usdt'],'approve(address,uint256)',['address','uint256'],[meta['router'],10**18])
        plan,source,_=prepare(vault,account,meta,rpc)
        rpc('miner_stop',[]);original_hash=rpc('eth_sendTransaction',[plan['transaction']])
        tx={**plan['transaction'],'gasPrice':hex(2*10**9)}
        if outcome=='CANCELLED':tx.update(to=meta['owner'],data='0x',gas='0x5208')
        tx_hash=original_hash if original_wallet else rpc('eth_sendTransaction',[tx])
        rpc('evm_mine',[]);rpc('miner_start',[1]);clock=[now()]
        if original_wallet:
            from app.live_execution.dex_call_resolution import collect_call_resolution
            from app.portfolio import wallet_swap as swaps
            evidence=collect_call_resolution(rpc,source,tx_hash)
            legacy={key:evidence[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
            with vault.session() as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=tx_hash,now=clock[0])
            with vault.session() as db:swaps.check_swap(db,plan['id'],account_id=account,evidence=legacy,now=clock[0])
    with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
    for currency,amount in [('USDT','100'),('BNB','0.01')]:
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION',currency,clock[0]-timedelta(days=1),amount=Decimal(amount)))
    vault.lock()
except BaseException:stack.close();raise

@contextmanager
def reader():
    if gate:held.set();assert release.wait(15)
    def read(method,params):
        assert 'send' not in method.lower()
        reads.append(method);return rpc(method,params)
    yield read

def factory(v,c):
    item=DexBotSupervisor(v,c,clock=lambda:clock[0]);supervisors.append(item);return item

@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:
        release.set()
        for supervisor in supervisors:supervisor.shutdown();supervisor.join(timeout=5)
        vault.lock();stack.close()

app=FastAPI(lifespan=lifespan)
app.include_router(create_real_router(vault.path,dex_rpc_factory=reader,dex_bot_supervisor_factory=factory));register_ui(app)
# All charts in this fixture are offline; actual execution data remains the
# signed mined receipt. No request can accidentally reach a public provider.
from app.marketdata import service as market_service
def offline(*args,**kwargs):raise HTTPException(400,'No external market in owned call fixture')
market_service.candles=offline

@app.get('/__test/state')
def state():
    resolved=None
    if supervisors and supervisors[0].vault.unlocked:
        with supervisors[0].vault.session() as db:resolved=resolution.status(db,kind,source.id) is not None
    return {'fixture_id':base.name,'wallet':meta['owner'],'account_id':account,'source_id':source.id,'kind':kind,'resolved':resolved,
    'outcome':outcome,'original_wallet':original_wallet,'tx_hash':tx_hash,'clock_ms':int(clock[0].timestamp()*1000),'held':held.is_set(),
    'api_reads':len(reads),'wallet_reads':len(wallet_reads),
    'amount_units':source.terms['trade']['amount'] if kind=='OPERATOR' else source.terms['amount_units']}

class Call(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    fixture_id:str
    method:str
    params:list=[]

@app.post('/__test/wallet-rpc')
def wallet_rpc(body:Call):
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
    if body.method not in {'eth_chainId','eth_getBalance','eth_blockNumber','eth_getBlockByNumber','eth_getCode','eth_getTransactionCount','eth_gasPrice','eth_estimateGas','eth_call'}:
        raise HTTPException(400,'Read-only fixture wallet')
    wallet_reads.append(body.method);return {'result':rpc(body.method,body.params)}

@app.post('/__test/control')
def control(body:Call):
    global gate
    if body.fixture_id!=base.name:raise HTTPException(400,'Wrong fixture')
    if body.method=='hold':gate=True;release.clear();held.clear()
    elif body.method=='release':gate=False;release.set()
    else:raise HTTPException(400,'Wrong control')
    return state()

@app.get('/__test/book')
def book():
    current=supervisors[0].vault
    with current.session() as db:
        snapshot=account_snapshot(db,account)
        return {'events':len(list_events(db,account)),'order_proofs':len(list(db.scalars(select(RealDexReceiptOrderProof)))),
            'cash':{k:str(v) for k,v in snapshot.cash.items()},
            'positions':{asset:str(p.quantity) for (asset,quote),p in snapshot.positions.items()}}
