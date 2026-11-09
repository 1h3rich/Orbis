"""Owned encrypted originals and actual read-only HTTP parser, no venue access."""
import os
from pathlib import Path

ROOT=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY']).resolve()
assert 'verification' in ROOT.parts
os.environ['ORBIS_DATABASE_URL']='sqlite://'
for name,file in (('ORBIS_LAB_PATH','lab.db'),('ORBIS_AUTOMATION_PATH','automation.db'),
    ('ORBIS_SPOT_STRATEGY_PATH','spot.db'),('ORBIS_PAPER_PERP_PATH','paper.db'),('ORBIS_REAL_VAULT_PATH','real.db')):
    os.environ[name]=str(ROOT/file)

import httpx
import pytest
from fastapi import FastAPI
from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_observed_api import create_observed_paper_router
from app.derivatives.perp_observed_service import ObservedPaperService
from app.integrations.grvt_position_config import POSITION_CONFIG_URL
from app.integrations.grvt_readonly import GrvtReadOnlyClient, AUTH_URL, TRADES_ROOT
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from tests.test_perp_live_review import reviewed_case

patch=pytest.MonkeyPatch()
lab,paper,archive,id,clock,fixture=reviewed_case(ROOT,patch)
archive.detach(id)
patch.setattr('app.portfolio.api.time.time_ns',lambda:clock['now'])
calls=[]
row=dict(fixture.row)
address='0x'+'2'*40


def private(url,body,headers):
    calls.append(url)
    if url==AUTH_URL:
        assert body=={'api_key':'owned-browser-view-key'}
        return httpx.Response(200,request=httpx.Request('POST',url),json={'status':'success','sub_account_id':'123'},
            headers={'set-cookie':'gravity=owned-browser-cookie; Path=/','x-grvt-account-id':address})
    assert url.startswith(TRADES_ROOT+'/')
    if url.endswith('/aggregated_account_summary'):
        data={'result':{'main_account_id':address,'total_equity':'1000','total_sub_account_equity':'1000','spot_balances':[]}}
    elif url.endswith('/account_summary'):
        data={'result':{'sub_account_id':'123','settle_currency':'USDT','event_time':str(clock['now']),
            'total_equity':'1000','maintenance_margin':'0','unrealized_pnl':'0','positions':[],
            'spot_balances':[{'currency':'USDT','balance':'1000'}]}}
    else:
        assert url==POSITION_CONFIG_URL and body=={'sub_account_id':'123'}
        data={'results':[row]}
    return httpx.Response(200,request=httpx.Request('POST',url),json=data)


app=FastAPI()
app.include_router(create_real_router(ROOT/'real.db',lab_path=lab.path,paper_perp_path=paper.path,
    grvt_factory=lambda key:GrvtReadOnlyClient(key,http_post=private,clock_ns=lambda:clock['now'])))
app.include_router(create_observed_paper_router(ObservedPaperService(lab,archive)))
register_ui(app)


@app.get('/__test/state')
def state():return {'session_id':id,'private_calls':calls,'config':row}


@app.post('/__test/mismatch')
def mismatch():
    row.update(margin_type='ISOLATED' if fixture.row['margin_type']=='CROSS' else 'CROSS',leverage='1',max_leverage='1')
    return {'changed':True}


@app.post('/__test/lock-paper')
def lock_paper():
    paper_perp_access.lock(paper.path)
    return {'locked':True}
