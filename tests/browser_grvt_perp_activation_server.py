"""Actual UI/API/supervised worker with encrypted originals and owned venue I/O."""
import os
from pathlib import Path
from threading import Event

directory=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY']).resolve()
assert 'verification' in directory.parts
os.environ['ORBIS_DATABASE_URL']='sqlite://'
for key,file in [('ORBIS_LAB_PATH','bootstrap-lab.db'),('ORBIS_AUTOMATION_PATH','bootstrap-dca.db'),
    ('ORBIS_PAPER_PERP_PATH','bootstrap-perp.db'),('ORBIS_SPOT_STRATEGY_PATH','bootstrap-spot.db'),
    ('ORBIS_REAL_VAULT_PATH','bootstrap-real.db')]:
    os.environ[key]=str(directory/file)

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.derivatives.perp_observed_api import create_observed_paper_router
from app.derivatives.perp_observed_service import ObservedPaperService
from tests.grvt_perp_coordinator_fixture import OwnedPerpCoordinator
from app.portfolio import api as module
from app.web.mount import register_ui
from app.marketdata import service as market_service

patch=pytest.MonkeyPatch()
optimized = os.environ.get('ORBIS_TEST_GRVT_PERP_OPTIMIZED') == '1'
owned=OwnedPerpCoordinator(directory/'owned',patch,optimized=optimized)
owned.close_on_watch=False
drive=Event()
def waiter(stop,seconds):
    while not drive.wait(.01):
        if stop.is_set():return
    if stop.wait(.03):return
    owned.waiter(stop,seconds)

path=owned.case.vault.path;owned.case.vault.lock()
access=module.RealAccess(path,lab_path=owned.owned.lab.path)
patch.setattr(module,'RealAccess',lambda *args,**kwargs:access)
app=FastAPI()
app.include_router(module.create_real_router(path,lab_path=owned.owned.lab.path,paper_perp_path=owned.owned.paper.path,
    grvt_mainnet_factory=owned.factory,grvt_mainnet_public_post=owned.case.public,
    grvt_mainnet_clock_ns=lambda:owned.case.clock[0],grvt_mainnet_waiter=waiter))
owned.case.vault=access.vault
owned.service=access._grvt_mainnet_bots
owned.service.perp_source_factory=owned.source;owned.service.perp_worker_waiter=owned.worker_waiter
app.include_router(create_observed_paper_router(ObservedPaperService(owned.owned.lab,owned.owned.origin.archive)))
register_ui(app)

def candles(symbol,*,provider,timeframe,limit,since=None,before=None):
    assert provider=='grvt' and symbol=='BTC/USDT:PERP' and limit==1000
    step={'1h':3600,'4h':14400,'1m':60}[timeframe]
    now=owned.case.clock[0]//10**9
    first=now//step*step-12*step if before is None else before-6*step
    return {'available':True,'resolved_provider':'grvt','symbol':symbol,'timeframe':timeframe,
        'candles':[{'t':first+i*step,'o':'100','h':'111','l':'95','c':'100','v':'10',
            'closed':first+(i+1)*step<=now} for i in range(13 if before is None else 6)],
        'older_pagination_supported':True,'history_exhausted':before is not None}
market_service.candles=candles

@app.middleware('http')
async def isolated(request,call_next):
    if not request.url.path.startswith(('/ui','/real','/__test','/market/candles','/paper/perp/observed')):
        return JSONResponse({'detail':'Fuera de la prueba aislada'},status_code=404)
    return await call_next(request)

@app.get('/__test/state')
def state():
    return {'fixture_id':directory.name,'session_id':owned.owned.origin.source().session_id,
        'sends':len(owned.case.sent),'reductions':len(owned.case.settings['reduction_posts']),
        'positions':owned.case.settings['positions'],'balance':owned.case.settings['account_balance'],
        'jobs':[{'id':job.saved['id'],'active':job.active,'status':job.status,'archive_error':job.archive_error}
            for job in owned.service._jobs.values()]}

@app.post('/__test/emit')
def emit():drive.set();return {'drive':True}

@app.post('/__test/trigger')
def trigger():owned.close_on_watch=True;return {'triggered':True}
