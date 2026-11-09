"""Owned encrypted LAB/PAPER; real main/lifecycle/API/worker with public fixtures."""
import json
import os
from pathlib import Path
from threading import RLock
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict
from sqlalchemy import create_engine,event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT=Path(os.environ['ORBIS_BASIC_DIRECTORY']).resolve()
PHASE=os.environ['ORBIS_BASIC_PHASE']
assert PHASE in ('create','reopen') and os.environ['ORBIS_BASIC_FIXTURE']=='1'
for name in ('ORBIS_LAB_PATH','ORBIS_AUTOMATION_PATH','ORBIS_SPOT_STRATEGY_PATH','ORBIS_PAPER_PERP_PATH','ORBIS_REAL_VAULT_PATH'):
    assert Path(os.environ[name]).resolve().is_relative_to(ROOT)
assert os.environ['ORBIS_DATABASE_URL']=='sqlite://'
assert Path(os.environ['ORBIS_LAB_PATH']).name=='lab.db'
assert Path(os.environ['ORBIS_PAPER_PERP_PATH']).name=='paper.db'
ROOT.mkdir(parents=True,exist_ok=True)

from tests.perp_observed_archive_fixture import LAB_KEY,PAPER_KEY,NS,owned_archive
from tests.perp_observed_source_fixture import OwnedPublicAdapter
from tests.test_perp_partial_simulation import TARGETS
from app.derivatives.perp_observed_wire import read_observed_seed
from app.derivatives.paper_access import paper_perp_access
from app.lab.access import lab_access

patch=pytest.MonkeyPatch()
if PHASE=='create':
    assert not (ROOT/'seed.json').exists()
    partial_targets=TARGETS if os.environ.get('ORBIS_TEST_PERP_OBSERVED_PARTIAL')=='1' else None
    lab,paper,_,origin,seed,clock=owned_archive(ROOT,patch,limited=os.environ.get('ORBIS_TEST_PERP_ENTRY')=='LIMIT',
        partial_targets=partial_targets)
    # >1,000 closed fixture bars exercise real UI history paging. They are
    # controlled public observations, not a claim about the actual provider.
    raw=seed.payload();old=int(seed.received_at_ns)//NS;start=old+1200*60
    for index,stamp in enumerate(range(old,start,60)):
        price=Decimal('100')+Decimal(index%100-50)/100
        raw['history'].append({'t':stamp,'o':str(price),'h':str(price+Decimal('.12')),
            'l':str(price-Decimal('.12')),'c':str(price+Decimal('.05')),'v':'10'})
    raw['received_at_ns']=raw['quote']['event_time_ns']=str(start*NS)
    for name in ('rules','specification'):raw[name]['fetched_at_ns']=str(start*NS)
    raw['funding']=[{**raw['funding'][-1],'funding_time':str(start//3600*3600*NS)}]
    seed=read_observed_seed(raw);seed.start()
    state={'now':int(seed.received_at_ns),'target':int(seed.received_at_ns)}
    (ROOT/'seed.json').write_text(json.dumps(seed.payload()))
    (ROOT/'clock.json').write_text(json.dumps(state))
    (ROOT/'fixture.json').write_text(json.dumps({'validation_id':origin.validation_id,
        'profile_id':origin.reference().profile_id,'dataset_id':origin.reference().dataset_id}))
    lab_access.lock(lab.path);paper_perp_access.lock(paper.path);patch.undo()
else:
    seed=read_observed_seed(json.loads((ROOT/'seed.json').read_text()))
    state=json.loads((ROOT/'clock.json').read_text())
fixture=json.loads((ROOT/'fixture.json').read_text())
patch.setattr('app.marketdata.adapters.grvt.time.time_ns',lambda:state['now'])
adapter=OwnedPublicAdapter(seed,state)
if state['now']>=int(seed.received_at_ns)+60*NS:
    stamp=int(seed.received_at_ns)//NS;adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
# The adapter fixture names its clock field "now"; expose the same controlled
# instant to all production collectors. Each quote advances at most ten seconds.
adapter.clock={'now':state['now']}
original_ticker=adapter.get_ticker
def ticker(symbol):
    state['now']=min(state['target'],state['now']+10*NS);adapter.clock['now']=state['now']
    (ROOT/'clock.json').write_text(json.dumps(state))
    return original_ticker(symbol)
adapter.get_ticker=ticker
original_rules=adapter.get_margin_rules
def margin_rules(symbol):
    snapshot=original_rules(symbol)
    if state['now']>=int(seed.received_at_ns)+30*NS:
        snapshot['risk_brackets'][0]['maintenance_margin_rate']='0.011'
    return snapshot
adapter.get_margin_rules=margin_rules
from app.marketdata.adapters.grvt import GrvtAdapter
for name in ('get_instrument_spec','get_margin_rules','get_ticker'):
    patch.setattr(GrvtAdapter,name,lambda self,symbol,name=name:getattr(adapter,name)(symbol))

from app import main
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_worker import ObservedSessionWorker

main._observed_paper.archive.clock_ns=lambda:state['now']
observed_sources=[]
def observed_source():
    observed_sources.append(True);return GrvtObservedSource(adapter=adapter,clock_ns=lambda:state['now'])
main._observed_paper.source_factory=observed_source
main._observed_paper.worker_factory=lambda archive,id,source,**options:ObservedSessionWorker(archive,id,source,poll_seconds=.03,**options)
app=main.app

# Keep unrelated legacy UI routes on one owned in-memory database across HTTP
# threads; no personal/plaintext file and no hidden preferences 500 responses.
from app.database.database import Base,get_db,enforce_sqlite_foreign_keys
ui_engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
event.listen(ui_engine,'connect',enforce_sqlite_foreign_keys);Base.metadata.create_all(ui_engine)
ui_session=sessionmaker(bind=ui_engine,autoflush=False,autocommit=False);ui_lock=RLock()
def owned_ui_db():
    with ui_lock:
        with ui_session() as db:yield db
app.dependency_overrides[get_db]=owned_ui_db

@app.get('/__observed_fixture/state')
def fixture_state():
    return {**fixture,'now':str(state['now']),'calls':adapter.calls,'observed_sources':len(observed_sources),
        'lab_locked':lab_access.get(ROOT/'lab.db') is None,
        'paper_locked':paper_perp_access.get(ROOT/'paper.db') is None,
        'coordinator':main._observed_paper.snapshot(),
        'real_exists':Path(os.environ['ORBIS_REAL_VAULT_PATH']).exists()}

class Advance(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    fixture_id: str
    action: str

@app.post('/__observed_fixture/advance')
def advance(body: Advance):
    if body.fixture_id!=ROOT.parent.name:raise HTTPException(403,'Owned fixture mismatch')
    if body.action=='ENTRY':
        stamp=int(seed.received_at_ns)//NS
        adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
        state['target']=int(seed.received_at_ns)+60*NS
    elif body.action=='EXIT':
        adapter.quote_override={'bid':'103','ask':'104'};state['target']=state['now']+10*NS
    elif body.action=='LIMIT_FILL':
        adapter.quote_override={'bid':'98','ask':'99'};state['target']=state['now']+10*NS
    elif body.action=='PARTIAL_FILL':
        adapter.quote_override={'bid':'107','ask':'108'};state['target']=state['now']+10*NS
    elif body.action=='GAP_ENTRY':
        stamp=state['now']//NS//60*60
        adapter.rows[stamp]={'t':stamp,'o':'100','h':'121','l':'99','c':'120','v':'10'}
        adapter.quote_override={'bid':'99','ask':'100','mark_price':'100'}
        state['target']=(stamp+60)*NS
    elif body.action=='GAP':
        state['now']+=30*NS;state['target']=state['now']
    elif body.action=='GAP_EXIT':
        adapter.quote_override={'bid':'103','ask':'104','mark_price':'100'}
        state['target']=state['now']+10*NS
    else:raise HTTPException(422,'Unknown owned market action')
    (ROOT/'clock.json').write_text(json.dumps(state));return fixture_state()
