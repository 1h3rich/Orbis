"""Owner-configured local EVM + encrypted LAB/PAPER; no external transactions."""
from contextlib import ExitStack,asynccontextmanager,contextmanager
from pathlib import Path
import os
from fastapi import FastAPI
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from app.portfolio.models import RealDexBotBinding,RealDexOperatorGrant,RealDexOperatorIntent
from app.automation.spot_paper_access import spot_paper_access
from app.automation.spot_strategy_paper import create_spot_strategy_router
from app.lab.api import create_lab_router
from tests.dex_bot_source_fixture import SourceCase
from tests.dex_evm_fixture import evm
from tests.test_dex_allowance_journal import ready
from tests.test_dex_deployments import setup,PASSWORD

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY']);(base/'source').mkdir(exist_ok=True,parents=True)
stack=ExitStack()
try:
    meta,rpc=stack.enter_context(evm(base/'evm'))
    source=SourceCase(base/'source',research=True);vault,account,now=setup(base)
    deployment=ready(vault,account,now,meta,rpc);vault.lock()
except BaseException:
    stack.close();raise
reads=[]

@contextmanager
def factory():
    def read(method,params):reads.append(method);return rpc(method,params)
    yield read

@asynccontextmanager
async def lifespan(_):
    try:yield
    finally:source.close();vault.lock();stack.close()

app=FastAPI(lifespan=lifespan)
app.include_router(create_real_router(vault.path,spot_paper=source.paper,dex_rpc_factory=factory))
app.include_router(create_lab_router(source.lab.path))
app.include_router(create_spot_strategy_router(source.paper.path,source.lab.path,paper=source.paper))
register_ui(app)

@app.get('/__test/state')
def state():
    return {'fixture_id':base.name,'wallet':meta['owner'],'account_id':account,'deployment_id':deployment['id'],
        'paper_session':source.session['id'],'reads':len(reads)}

@app.post('/__test/lock-paper')
def lock_paper():return spot_paper_access.lock(source.paper.path)
