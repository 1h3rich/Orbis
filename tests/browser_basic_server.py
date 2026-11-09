"""Owned empty installation; real app/routes/worker, only public adapter controlled."""
from pathlib import Path
from decimal import Decimal
import json,os,time
from pydantic import BaseModel,ConfigDict

PROCESS_STARTED_NS=time.time_ns()
ROOT=Path(os.environ['ORBIS_BASIC_DIRECTORY']).resolve()
assert ROOT.name == 'data'
assert os.environ.get('ORBIS_BASIC_PHASE') in {'create', 'reopen'}
assert os.environ.get('ORBIS_BASIC_FIXTURE')=='1'
for name in ('ORBIS_LAB_PATH','ORBIS_AUTOMATION_PATH','ORBIS_SPOT_STRATEGY_PATH','ORBIS_PAPER_PERP_PATH','ORBIS_REAL_VAULT_PATH'):
    assert Path(os.environ[name]).resolve().is_relative_to(ROOT)
assert os.environ['ORBIS_DATABASE_URL']=='sqlite://'
ROOT.mkdir(exist_ok=True,parents=True)
clock_file=ROOT/'clock.json'
if clock_file.exists():state=json.loads(clock_file.read_text())
else:
    assert os.environ['ORBIS_BASIC_PHASE'] == 'create'
    assert not any((ROOT/name).exists() for name in ('lab.db', 'spot.db', 'real.db'))
    ns=time.time_ns();end=ns//10**9//3600*3600
    state={'clock_ns':(end+10)*10**9,'end':end,'start':end-120*3600,'bid':'219','ask':'219.01','entries_advanced':0}
    clock_file.write_text(json.dumps(state))
def clock_ns():return state['clock_ns']
def candle(t):
    price=Decimal(100)+(t-state['start'])//3600
    return {'t':t,'o':str(price),'h':str(price+1),'l':str(price-1),'c':str(price),'v':'10'}
from app.lab import api as lab_api
from app.marketdata.adapters.mexc import MexcAdapter
class OwnedMarket(MexcAdapter):
    def get_candles(self,symbol,*,timeframe,limit,since=None,before=None,**_):
        assert symbol=='BTC/USDT' and timeframe=='1h'
        end=min(before or clock_ns()//10**9//3600*3600,clock_ns()//10**9//3600*3600)
        start=since if since is not None else end-limit*3600
        return [candle(t) for t in range(max(start,state['start']),end,3600)][:limit]
    def get_paper_quote(self,symbol):
        assert symbol=='BTC/USDT'
        return {'symbol':symbol,'source':'mexc-spot-public','event_time_ns':clock_ns(),'bid':state['bid'],'ask':state['ask'],'last':state['bid']}
# Preserve real download pagination, validation and provenance; replace only
# the public provider in this separate owned process, never a product adapter.
lab_api.MexcAdapter=OwnedMarket
from app import main
from app.lab.access import lab_access
from app.automation.spot_paper_access import spot_paper_access
key_origin=clock_ns()
def key_clock():return (clock_ns()-key_origin)/10**9
lab_access._clock=spot_paper_access._clock=key_clock
main._spot_strategy_paper.adapter_factory=OwnedMarket
main._spot_strategy_paper.clock_ns=clock_ns
main._spot_strategy_worker.clock_ns=clock_ns
app=main.app

@app.get('/__test/state')
def fixture_state():
    return {'fixture_id':ROOT.parent.name,**state,'server':{'pid':os.getpid(),'started':PROCESS_STARTED_NS},'worker':main._spot_strategy_worker.snapshot(),
            'real_exists':Path(os.environ['ORBIS_REAL_VAULT_PATH']).exists()}
class Advance(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fixture_id:str
    action:str
@app.post('/__test/advance')
def advance(body:Advance):
    assert body.fixture_id==ROOT.parent.name
    if body.action=='ENTRY':
        assert state['entries_advanced']==0
        state['clock_ns']=(state['end']+3600+5)*10**9
        state.update(bid='220',ask='220.01',entries_advanced=1)
    elif body.action=='TARGET':
        assert state['entries_advanced']==1
        state['clock_ns']+=10*10**9;state.update(bid='300',ask='300.01')
    else:raise ValueError('Unknown owned fixture control')
    clock_file.write_text(json.dumps(state))
    main._spot_strategy_worker.wake()
    return fixture_state()
