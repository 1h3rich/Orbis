"""UI/API/PAPER/LAB/SQLCipher reales; HTTP privado y público GRVT controlados."""
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import JSONResponse

directory=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
os.environ['ORBIS_DATABASE_URL']='sqlite://'
for key,file in [('ORBIS_LAB_PATH','bootstrap-lab.db'),('ORBIS_AUTOMATION_PATH','bootstrap-dca.db'),
    ('ORBIS_PAPER_PERP_PATH','bootstrap-perp.db'),('ORBIS_SPOT_STRATEGY_PATH','bootstrap-spot.db'),('ORBIS_REAL_VAULT_PATH','bootstrap-real.db')]:
    os.environ[key]=str(directory/file)
from tests.test_grvt_mainnet_bot_service import service_case
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
case,origin,source,emit,settings,unused=service_case(directory,research=True);case.vault.lock()
app=FastAPI();app.include_router(create_real_router(case.vault.path,lab_path=origin.lab.path,paper_perp_path=origin.paper.path,
    grvt_mainnet_factory=unused.factory,grvt_mainnet_public_post=unused.public,grvt_mainnet_clock_ns=unused.clock,
    grvt_mainnet_waiter=unused.waiter,grvt_mainnet_bot_poll_seconds=.02))
register_ui(app)
@app.middleware('http')
async def isolated(request,call_next):
    if not request.url.path.startswith(('/ui','/real','/__test')) and not (os.environ.get('ORBIS_TEST_GRVT_CHART')=='1' and request.url.path=='/market/candles'):return JSONResponse({'detail':'Fuera de la prueba aislada'},status_code=404)
    return await call_next(request)
@app.get('/__test/state')
def state():
    return {'fixture_id':directory.name,'session_id':source['session_id'],'sends':len(case.sent),
        'reductions':len(case.settings['reduction_posts']),'positions':case.settings['positions'],'balance':case.settings['account_balance']}
@app.post('/__test/emit')
def signal():emit();return {'emitted':True}
@app.post('/__test/trigger')
def trigger():settings['mark']='96.9';return {'triggered':True}


if os.environ.get('ORBIS_TEST_GRVT_CHART')=='1':
    from app.marketdata import service as market_service
    def chart_candles(symbol,*,provider,timeframe,limit,since=None,before=None):
        assert provider=='grvt' and symbol=='BTC/USDT:PERP' and limit==1000
        step={'1h':3600,'4h':14400}[timeframe];now=case.clock[0]//1_000_000_000
        first=now//step*step-12*step if before is None else before-6*step
        rows=[{'t':first+i*step,'o':'100','h':'111','l':'95','c':'100','v':'10','closed':first+(i+1)*step<=now}
            for i in range(13 if before is None else 6)]
        return {'available':True,'resolved_provider':'grvt','symbol':symbol,'timeframe':timeframe,'candles':rows,
            'older_pagination_supported':True,'history_exhausted':before is not None}
    market_service.candles=chart_candles

