"""One-use exact reviews, actual activation, revocation and encrypted history."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from tests.grvt_perp_api_fixture import perp_api_case,preview_body,reviewed
from tests.test_grvt_mainnet_api import ROOT,connect
from tests.test_grvt_mainnet_bot_service import wait_for
from tests.test_grvt_mainnet_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER

BASE=ROOT+'/bots/perp'


@pytest.mark.parametrize('direction,mode',[('LONG','ISOLATED'),('SHORT','CROSS')])
def test_review_future_signal_full_cycle_known_id_single_use_and_cold_history(tmp_path,monkeypatch,direction,mode):
    with perp_api_case(tmp_path,monkeypatch,direction=direction,mode=mode) as (owned,access,api,headers):
        for url in (BASE+'/sources',ROOT+'/bots/capabilities'):
            assert api.get(url).status_code==401
        assert api.post(BASE+'/preview',json={}).status_code==401
        assert api.post(BASE+'/start',json={}).status_code==401
        capabilities=api.get(ROOT+'/bots/capabilities',headers=headers).json()
        assert capabilities['supervised_perpetual'] and capabilities['execution_authority']=='NONE'
        sources=api.get(BASE+'/sources',headers=headers)
        assert sources.status_code==200,sources.text
        [listed]=sources.json()['items']
        assert listed['verification']=='ON_PREVIEW_AND_START'
        exact=api.get(BASE+'/sources',params={'session_id':listed['session_id'],'offset':25},headers=headers)
        assert exact.headers['Cache-Control']=='no-store'
        assert exact.json()=={'network':'MAINNET','execution_authority':'NONE','items':[listed],'has_more':False,'offset':0}
        assert api.get(BASE+'/sources',params={'session_id':str(uuid4())},headers=headers).json()['items']==[]
        assert api.get(BASE+'/limit/sources',params={'session_id':listed['session_id']},headers=headers).json()['items']==[]
        body=reviewed(owned,api,headers);review=body['review'];run=review['run_id']
        assert review['schema']=='orbis.grvt.mainnet.perp.bot.review.v1'
        assert review['execution_authority']=='NONE' and not review['authorization']
        assert review['execution_scope']=='SUPERVISED_FUTURE_SIGNALS_AND_OWN_POSITION_CLOSE'
        assert review['sample_plan']['source']['account_equity']=='100'
        assert review['terms']['definition']['initial_capital']=='1000'
        assert 'signal_seed' not in review and len(json.dumps(review))<50_000
        assert not owned.case.sent and not api.get(ROOT+'/bots',headers=headers,
            params={'sub_account_id':'123456'}).json()['items']
        start=api.post(BASE+'/start',headers=headers,json=body)
        assert start.status_code==202,start.text
        assert start.json()['id']==run and start.json()['schema']=='orbis.grvt.mainnet.bot.grant.v5'
        assert api.post(BASE+'/start',headers=headers,json=body).status_code==409
        def finished():
            value=api.get(ROOT+'/bots/'+run,headers=headers).json().get('item')
            return value if value and not value['active'] else None
        done=wait_for(finished,seconds=45)
        assert done['status']=='ENTRY_LIMIT' and done['net_realized_pnl_usdt']=='-0.03',done
        assert len(owned.case.sent)==1 and len(owned.case.settings['reduction_posts'])==1
        group=api.get(ROOT+'/groups/'+done['parent_client_order_id'],headers=headers)
        assert group.status_code==200,group.text
        assert group.json()['group']['result']['net_realized_pnl_usdt']=='-0.03'
        for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key','synthetic-mainnet-session'):
            assert secret not in start.text+group.text+json.dumps(done)
        assert api.post('/real/vault/lock',headers=headers).status_code==200
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        cold=api.get(ROOT+'/bots/'+run,headers={'X-Orbis-Vault-Token':token}).json()['item']
        assert not cold['active'] and cold['status']=='ENTRY_LIMIT' and not cold['authorization']
        assert len(owned.case.sent)==1



def test_optimized_origin_is_sealed_and_a_changed_lab_version_revokes_it(tmp_path, monkeypatch):
    with perp_api_case(tmp_path, monkeypatch, optimized=True) as (owned, access, api, headers):
        body = reviewed(owned, api, headers)
        review = body['review']
        origin = owned.owned.origin.origin
        lab = review['lab_origin']
        assert lab == origin.review_lab_origin()
        assert lab['version'] == 2
        assert lab['version_id'] == review['source']['research']['profile']['strategy_version_id']
        assert lab['optimization']['version_id'] == lab['version_id']
        assert review['source']['paper']['closed_trades'] == 1
        assert review['source']['paper']['observed_bars'] == 1
        assert not review['authorization'] and not owned.case.sent
        forged = deepcopy(body)
        forged['review']['lab_origin']['optimization']['optimization_hash'] = '0'*64
        assert api.post(BASE+'/start', headers=headers, json=forged).status_code == 409
        with owned.owned.lab.connect() as db:
            db.execute('UPDATE strategy_versions SET version=version+1 WHERE id=?', (lab['version_id'],))
        before = list(owned.owned.adapter.calls)
        assert api.post(BASE+'/start', headers=headers, json=body).status_code == 409
        assert owned.owned.adapter.calls == before
        assert not owned.service._jobs and not owned.case.sent

def test_changed_terms_model_origin_mac_and_foreign_origin_never_start(tmp_path,monkeypatch):
    with perp_api_case(tmp_path,monkeypatch) as (owned,access,api,headers):
        body=reviewed(owned,api,headers)
        changes=[]
        changed=deepcopy(body);changed['review']['terms']['budget']['entry_cash_limit']='99';changes.append(changed)
        changed=deepcopy(body);changed['review']['source']['paper']['head_hash']='f'*64;changes.append(changed)
        changed=deepcopy(body);changed['review']['supervision_engine']='untrusted';changes.append(changed)
        changed=deepcopy(body);changed['review_mac']='0'*64;changes.append(changed)
        for changed in changes:assert api.post(BASE+'/start',headers=headers,json=changed).status_code==409
        assert api.post(BASE+'/start',headers={**headers,'Origin':'https://foreign.invalid'},json=body).status_code==403
        assert api.post(ROOT+'/bots/start',headers=headers,json={**body,'confirmation':'ACTIVAR_BOT_GRVT_MAINNET'}).status_code==409
        assert not owned.service._jobs and not owned.case.sent


@pytest.mark.parametrize('fault',['expired','connection','bot_stop','paper_epoch','lab_epoch','original_bytes'])
def test_review_loses_authority_on_expiry_revision_epoch_or_original_byte_change(tmp_path,monkeypatch,fault):
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    from tests.perp_observed_archive_fixture import LAB_KEY,PAPER_KEY
    with perp_api_case(tmp_path,monkeypatch) as (owned,access,api,headers):
        body=reviewed(owned,api,headers)
        before=len(owned.owned.adapter.calls)
        if fault=='expired':owned.case.clock[0]+=121*10**9
        elif fault=='connection':access._grvt_mainnet.disconnect('123456')
        elif fault=='bot_stop':owned.service.stop_subaccount('123456')
        elif fault=='paper_epoch':
            paper_perp_access.lock(owned.owned.paper.path);paper_perp_access.unlock(owned.owned.paper.path,PAPER_KEY)
        elif fault=='lab_epoch':
            lab_access.lock(owned.owned.lab.path);lab_access.unlock(owned.owned.lab.path,LAB_KEY)
        else:
            with owned.owned.paper.connect() as db:
                row=db.execute('SELECT sequence,input_blob FROM paper_observed_events WHERE session_id=? ORDER BY sequence LIMIT 1',
                    (body['review']['source']['session_id'],)).fetchone()
                raw=bytearray(row['input_blob']);raw[-1]^=1
                db.execute('UPDATE paper_observed_events SET input_blob=? WHERE session_id=? AND sequence=?',
                    (bytes(raw),body['review']['source']['session_id'],row['sequence']));db.commit()
        assert api.post(BASE+'/start',headers=headers,json=body).status_code==409
        assert not owned.case.sent and not owned.service._jobs
        assert len(owned.owned.adapter.calls)==before


def test_read_only_review_requires_explicit_dedicated_budget_and_closed_originals(tmp_path,monkeypatch):
    with perp_api_case(tmp_path,monkeypatch) as (owned,access,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        body=preview_body(owned,revision)
        for bad in ({**body,'dedicated_account_confirmed':False},{**body,'dedicated_account_confirmed':1},
            {**body,'entry_cash_limit':2.5},{**body,'private_key':'forbidden'}):
            assert api.post(BASE+'/preview',headers=headers,json=bad).status_code==422
        for bad in ({**body,'collateral_limit':'99'},{**body,'fee_rate_ceiling':'0.02'},
            {**body,'entry_notional_limit':'1001'}):
            assert api.post(BASE+'/preview',headers=headers,json=bad).status_code==409
        assert not owned.service._jobs and not owned.case.sent


def test_review_expiring_during_startup_cannot_create_a_late_grant(tmp_path,monkeypatch):
    with perp_api_case(tmp_path,monkeypatch) as (owned,access,api,headers):
        body=reviewed(owned,api,headers)
        changed=[]
        def delayed_login(url,request):
            if url.endswith('/login') and not changed:
                changed.append(True);owned.case.clock[0]+=121*10**9
                owned.owned.clock['now']=owned.case.clock[0]
        owned.case.settings['private_hook']=delayed_login
        response=api.post(BASE+'/start',headers=headers,json=body)
        assert response.status_code==409 and changed
        assert not owned.case.sent and not owned.service._jobs
        assert not api.get(ROOT+'/bots',headers=headers,params={'sub_account_id':'123456'}).json()['items']


def test_concurrent_start_requests_consume_one_review_and_create_one_worker(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with perp_api_case(tmp_path,monkeypatch) as (owned,access,api,headers):
        body=reviewed(owned,api,headers);gate=Barrier(2)
        def start():
            gate.wait(5)
            return api.post(BASE+'/start',headers=headers,json=body).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls=[pool.submit(start) for _ in range(2)]
            statuses=[call.result(30) for call in calls]
        assert sorted(statuses)==[202,409]
        run=body['review']['run_id']
        done=wait_for(lambda:(item if not (item:=owned.service.snapshot(run))['active'] else None),seconds=45)
        assert done['status']=='ENTRY_LIMIT' and done['completed_entries']==1
        assert len(owned.service._jobs)==1 and len(owned.case.sent)==1
