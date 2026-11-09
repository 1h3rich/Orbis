"""Owned encrypted REAL vault; real codec/client, controlled HTTP boundaries."""
from copy import deepcopy
import json
import subprocess
import sys

import httpx

from app.integrations.grvt_position_config import POSITION_CONFIG_URL
from app.live_execution.grvt_perp_preflight import collect_perp_preflight
from tests.test_grvt_mainnet_submit import live_case
from tests.test_grvt_perp_plan import plan_source


def perp_case(directory,*,mode='ISOLATED',direction='LONG'):
    case=live_case(directory)
    case.settings.update(account_balance='100',account_equity='100')
    case.terms=plan_source(direction,mode).terms
    from app.live_execution.grvt_perp_plan import PerpEntryTerms
    raw=case.terms.payload();raw['budget']['fee_rate_ceiling']='0.001'
    case.terms=PerpEntryTerms.model_validate(raw)
    case.config={'instrument':'BTC_USDT_Perp','margin_type':mode,'leverage':'5','min_leverage':'1','max_leverage':'50'}
    case.config_calls=[];case.config_hook=None;case.mark='100'
    old_factory=case.factory;old_public=case.public
    def factory():
        client=old_factory();post=client._post
        def private(url,body,headers):
            if url!=POSITION_CONFIG_URL:return post(url,body,headers)
            assert body=={'sub_account_id':'123456'}
            case.config_calls.append(deepcopy(body))
            if case.config_hook:case.config_hook(len(case.config_calls))
            return httpx.Response(200,request=httpx.Request('POST',url),json={'results':[deepcopy(case.config)]})
        client._post=private
        return client
    def public(url,body):
        reply=old_public(url,body)
        if not url.endswith('/ticker'):return reply
        data=reply.json();data['result']['mark_price']=case.mark
        return httpx.Response(200,request=httpx.Request('POST',url),json=data)
    case.factory=factory;case.public=public
    return case


def preflight(case,*,checked=None):
    return collect_perp_preflight(case.factory(),case.terms,public_post=case.public,
        clock_ns=checked or (lambda:case.clock[0]))


def cold_read(path,parent,password):
    """A different interpreter replays the encrypted record with no network."""
    code='''import json,sys
from pathlib import Path
import httpx
def forbidden(*args,**kwargs):raise AssertionError('cold read attempted network')
httpx.post=forbidden;httpx.get=forbidden
from app.portfolio.vault import RealVault
from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
args=json.loads(sys.stdin.read());vault=RealVault(Path(args['path']));vault.unlock(args['password'])
value=read_mainnet_bracket(vault,args['parent'])
print(json.dumps({'status':value['status'],'authorization':value['authorization'],
 'claim_schema':value['claim']['schema'],'source':value['claim']['source'],
 'reconciled':value['financially_reconciled'],
 'net':value['reconciliation']['result']['net_realized_pnl_usdt'] if value['reconciliation'] else None}))
vault.lock()
'''
    result=subprocess.run([sys.executable,'-c',code],input=json.dumps({'path':str(path),'parent':parent,'password':password}),
        text=True,capture_output=True,check=True,timeout=30)
    return json.loads(result.stdout)
