"""UI/API/SQLCipher reales; sólo el HTTP externo de GRVT está controlado."""
import os,time
from pathlib import Path
from decimal import Decimal
from fastapi import FastAPI
from fastapi.responses import JSONResponse

directory=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
os.environ['ORBIS_DATABASE_URL']='sqlite://'
for key,file in [('ORBIS_LAB_PATH','lab.db'),('ORBIS_AUTOMATION_PATH','dca.db'),('ORBIS_PAPER_PERP_PATH','perp.db'),
    ('ORBIS_SPOT_STRATEGY_PATH','spot.db'),('ORBIS_REAL_VAULT_PATH','bootstrap-real.db')]:os.environ[key]=str(directory/file)
from tests.test_grvt_mainnet_submit import live_case
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
if os.environ.get('ORBIS_TEST_GRVT_VIEW_IOC')=='1':
    from tests.test_grvt_mainnet_recovery import prepared_case,send_case
    case=prepared_case(directory)
    case.settings['reduction_lost']=True
    attempt=send_case(case)['reductions'][0]
    assert attempt['acknowledgement'] is None and attempt['state'] is None
    case.clock[0]+=121_000_000_000
    if os.environ.get('ORBIS_TEST_GRVT_VIEW_IOC_HISTORY')=='1':
        from tests.test_grvt_perp_limit_execution import enable_order_history
        enable_order_history(case, *case.settings['orders'])
else:
    case=live_case(directory)
case.vault.lock()
clock_offset=[max(0,case.clock[0]-time.time_ns())]
def clock():
    case.clock[0]=time.time_ns()+clock_offset[0]
    return case.clock[0]
def public(url,body):clock();return case.public(url,body)
def waiter(stop,seconds):
    clock_offset[0]+=int(seconds*1_000_000_000);clock()
    return stop.wait(.02)
app=FastAPI();app.include_router(create_real_router(case.vault.path,grvt_mainnet_factory=lambda key:case.factory(),
    grvt_mainnet_public_post=public,grvt_mainnet_clock_ns=clock,grvt_mainnet_waiter=waiter))
from app.live_execution.grvt_mainnet_session import GrvtMainnetSession
def endpoints(router):
    for route in router.routes:
        if hasattr(route,'original_router'):yield from endpoints(route.original_router)
        elif hasattr(route,'endpoint'):yield route
mainnet_service=next(cell.cell_contents for route in endpoints(app.router) if getattr(route,'path','').endswith('/exchanges/grvt/mainnet')
    for cell in route.endpoint.__closure__ or () if isinstance(cell.cell_contents,GrvtMainnetSession))
register_ui(app)
@app.middleware('http')
async def isolated(request,call_next):
    if not request.url.path.startswith(('/ui','/real','/__test')):return JSONResponse({'detail':'Fuera de la prueba aislada'},status_code=404)
    return await call_next(request)
@app.get('/__test/state')
def state():
    return {'fixture_id':directory.name,'sends':len(case.sent),'cancels':len(case.settings['cancel_posts']),'reductions':len(case.settings['reduction_posts']),
        'positions':case.settings['positions'],'balance':case.settings['account_balance'],
        'order_history_requests':sum(name=='order_history' for name,_ in case.settings['history_requests']),
        'jobs':{parent:mainnet_service.job(parent) for parent in list(mainnet_service._jobs)}}
@app.post('/__test/partial')
def partial():
    parent=case.settings['active_parent'];entry=case.settings['orders'][parent]
    stamp=clock();target=case.settings['orders'][str(int(parent)^2)];stop=case.settings['orders'][str(int(parent)^1)]
    target['state'].update(status='CANCELLED',traded_size=['0.2'],update_time=str(stamp));stop['state'].update(status='CANCELLED',update_time=str(stamp))
    def fill(order,trade,stamp,size,price,fee,pnl):
        return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':trade,'order_id':order['order_id'],
            'client_order_id':order['metadata']['client_order_id'],'event_time':str(stamp),'is_buyer':order['legs'][0]['is_buying_asset'],
            'is_taker':True,'size':size,'price':price,'fee':fee,'fee_currency':'USDT','realized_pnl':pnl,'builder_fee':'0','is_liquidation':False}
    case.settings['histories']['fill_history']=[fill(target,'partial-native-exit',stamp,'0.2','110','0.01','2'),
        fill(entry,'partial-native-entry',entry['metadata']['create_time'],'0.5','100','0.02','0')]
    case.settings.update(account_balance='10001.97',account_equity='10001.97',initial_margin='1',maintenance_margin='0.5',
        positions=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp','size':'0.3'}])
    return {'partial':True}
