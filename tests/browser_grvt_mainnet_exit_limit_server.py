"""Grupo con salida fuera del tope original; transporte GRVT sintético."""
import os,time
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import JSONResponse

directory=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY']);os.environ['ORBIS_DATABASE_URL']='sqlite://'
for key,file in [('ORBIS_LAB_PATH','lab.db'),('ORBIS_AUTOMATION_PATH','dca.db'),('ORBIS_PAPER_PERP_PATH','perp.db'),
    ('ORBIS_SPOT_STRATEGY_PATH','spot.db'),('ORBIS_REAL_VAULT_PATH','boot-real.db')]:os.environ[key]=str(directory/file)
from tests.test_grvt_mainnet_exit_limit import expensive_exit_case
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
case=expensive_exit_case(directory);case.vault.lock();offset=[0]
def clock():case.clock[0]=max(case.clock[0],time.time_ns()+offset[0]);return case.clock[0]
def public(url,body):clock();return case.public(url,body)
def waiter(stop,seconds):offset[0]+=int(seconds*1_000_000_000);clock();return stop.wait(.01)
app=FastAPI();app.include_router(create_real_router(case.vault.path,grvt_mainnet_factory=lambda key:case.factory(),
    grvt_mainnet_public_post=public,grvt_mainnet_clock_ns=clock,grvt_mainnet_waiter=waiter))
register_ui(app)
@app.middleware('http')
async def isolated(request,call_next):
    if not request.url.path.startswith(('/ui','/real','/__test')):return JSONResponse({'detail':'Fuera de la prueba'},status_code=404)
    return await call_next(request)
@app.get('/__test/state')
def state():return {'fixture_id':directory.name,'parent':case.parent,'sends':len(case.sent),'reductions':len(case.settings['reduction_posts']),
    'positions':case.settings['positions'],'balance':case.settings['account_balance']}
