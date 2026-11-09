"""API → trabajador efímero → diarios reales; proveedor y reloj ficticios."""

from contextlib import contextmanager
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import create_real_router
from tests.test_grvt_testnet_bracket_emergency import emergency_case
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_strategy_close import close_source
from tests.test_grvt_testnet_signer import KEY,SIGNER


ROOT='/real/exchanges/grvt/testnet/brackets'


@contextmanager
def close_api(directory,*,waiter=None,public_hook=None,clock_tick_ns=0):
    case=emergency_case(directory); request=close_source(case); logins=[]
    def factory(key): logins.append(True); return case.reader
    def advance_time(stop,seconds): case.clock[0]+=6_000_000_000; return stop.is_set()
    def public(url,payload):
        if public_hook is not None: public_hook(url,payload)
        return case.public(url,payload)
    def clock():
        at=case.clock[0]; case.clock[0]+=clock_tick_ns; return at
    app=FastAPI()
    app.include_router(create_real_router(directory/'real.db',lab_path=case.venue.lab.path,
        paper_perp_path=case.venue.paper.path,grvt_testnet_factory=factory,
        grvt_testnet_bracket_public_post=public,grvt_testnet_bracket_clock_ns=clock,
        grvt_testnet_bracket_close_waiter=waiter or advance_time))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        token=client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        yield case,request,client,{'X-Orbis-Vault-Token':token},logins
    case.second.lock(); case.vault.lock()


def body(request):
    return {'action_request_id':request,'api_key':'fake-testnet-close-key','dedicated_private_key':KEY.hex(),
        'expected_signer':SIGNER,'max_attempts':2,'ttl_seconds':120,
        'confirmation':'EJECUTAR_CIERRE_ESTRATEGIA_GRVT_TESTNET'}


def wait_finished(client,path,headers):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        response=client.get(path,headers=headers)
        assert response.status_code==200,response.text
        data=response.json()
        if data['run'] is not None and not data['run']['active']: return data
        time.sleep(.01)
    raise AssertionError('El trabajador no terminó')


def test_local_api_runs_the_whole_close_and_recovers_the_receipt_without_more_clicks(tmp_path):
    with close_api(tmp_path) as (case,request,client,headers,logins):
        path=ROOT+'/'+case.origin['client_order_id']+'/strategy-close'
        assert client.get(path).status_code==401
        assert client.post(path+'/start',json=body(request)).status_code==401
        started=client.post(path+'/start',headers=headers,json=body(request))
        assert started.status_code==202,started.text
        assert started.headers['cache-control']=='no-store'
        finished=wait_finished(client,path,headers)
        assert finished['run']['status']=='COMPLETE'
        assert finished['run']['last_result']['remaining_position']=='0'
        assert len(case.controls['sent'])==len(logins)==1
        assert isinstance(finished['request']['deadline_ns'],str)
        again=client.post(path+'/start',headers=headers,json=body(request))
        assert again.status_code==202 and len(case.controls['sent'])==len(logins)==1
        panel=client.get(ROOT,headers=headers,params={'sub_account_id':case.cap.sub_account_id})
        assert panel.json()['groups'][0]['strategy_close']['run']['status']=='COMPLETE'
        for secret in (KEY.hex(),SIGNER,'fake-testnet-close-key'):
            assert secret not in started.text and secret not in str(finished) and secret not in panel.text


def test_preparation_preserves_the_signal_while_the_user_enters_keys_and_locks_sources(tmp_path):
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    with close_api(tmp_path) as (case,request,client,headers,logins):
        path=ROOT+'/'+case.origin['client_order_id']+'/strategy-close'
        prepared=client.post(path+'/prepare',headers=headers,json={'action_request_id':request,'max_attempts':2,
            'ttl_seconds':120,'confirmation':'PREPARAR_CIERRE_ESTRATEGIA_GRVT_TESTNET'})
        assert prepared.status_code==200,prepared.text
        assert not logins and not case.controls['sent']
        case.clock[0]+=21_000_000_000
        paper_perp_access.lock(case.venue.paper.path); lab_access.lock(case.venue.lab.path)
        assert client.post(path+'/start',headers=headers,json=body(request)).status_code==202
        assert wait_finished(client,path,headers)['run']['status']=='COMPLETE'
        assert len(case.controls['sent'])==1


@pytest.mark.parametrize('ttl',[10,300])
def test_duration_boundaries_allow_verification_time_without_extending_the_grant(tmp_path,ttl):
    with close_api(tmp_path,clock_tick_ns=1) as (case,request,client,headers,logins):
        path=ROOT+'/'+case.origin['client_order_id']+'/strategy-close'
        started_at=case.clock[0]
        prepared=client.post(path+'/prepare',headers=headers,json={'action_request_id':request,'max_attempts':1,
            'ttl_seconds':ttl,'confirmation':'PREPARAR_CIERRE_ESTRATEGIA_GRVT_TESTNET'})
        assert prepared.status_code==200,prepared.text
        grant=prepared.json()['request']
        assert 0<int(grant['deadline_ns'])-int(grant['created_at_ns'])<=ttl*1_000_000_000
        assert int(grant['deadline_ns'])<=started_at+ttl*1_000_000_000+5
        assert not logins and not case.controls['sent']


@pytest.mark.parametrize('cause',['stop','lock','protect'])
def test_a_running_close_can_be_stopped_and_does_not_restart_after_unlock(tmp_path,cause):
    from threading import Event
    waiting=Event()
    def wait(stop,seconds): waiting.set(); return stop.wait(10)
    with close_api(tmp_path,waiter=wait) as (case,request,client,headers,logins):
        path=ROOT+'/'+case.origin['client_order_id']+'/strategy-close'
        assert client.post(path+'/start',headers=headers,json=body(request)).status_code==202
        assert waiting.wait(10)
        assert client.post(path+'/start',headers=headers,json=body(request)).status_code==409
        assert len(logins)==1
        assert client.post(path+'/stop',json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'}).status_code==401
        if cause=='stop':
            stopped=client.post(path+'/stop',headers=headers,json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'})
            assert stopped.status_code==200
            assert wait_finished(client,path,headers)['run']['status']=='STOPPED'
        else:
            target='/real/vault/lock' if cause=='lock' else '/real/vault/protect-view'
            assert client.post(target,headers=headers).status_code==200
            assert client.get(path,headers=headers).status_code==401
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            assert wait_finished(client,path,headers)['run']['status']=='STOPPED'
        assert len(case.controls['sent'])==len(logins)==1


@pytest.mark.parametrize('fault',['signer','expired','boolean','extra'])
def test_invalid_close_is_rejected_before_login_or_persistent_permission(tmp_path,fault):
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
    with close_api(tmp_path) as (case,request,client,headers,logins):
        payload=body(request); parent=case.origin['client_order_id']
        if fault=='signer': payload['expected_signer']='0x'+'f'*40
        if fault=='expired': case.clock[0]+=21_000_000_000
        if fault=='boolean': payload['max_attempts']=True
        if fault=='extra': payload['unnecessary_secret']='do-not-echo-this'
        response=client.post(ROOT+'/'+parent+'/strategy-close/start',headers=headers,json=payload)
        assert response.status_code==(422 if fault in ('boolean','extra') else 409),response.text
        assert not logins and not case.controls['sent'] and read_strategy_close(case.vault,parent) is None
        for secret in (KEY.hex(),payload['api_key'],'do-not-echo-this'): assert secret not in response.text


def test_history_poll_does_not_block_stop_and_revocation_prevents_a_new_post(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    def block(url,payload):
        if url.endswith('/instrument'): entered.set(); assert release.wait(10)
    with close_api(tmp_path,public_hook=block) as (case,request,client,headers,logins):
        path=ROOT+'/'+case.origin['client_order_id']+'/strategy-close'
        assert client.post(path+'/start',headers=headers,json=body(request)).status_code==202
        assert entered.wait(10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            history=pool.submit(client.get,ROOT,headers=headers,params={'sub_account_id':case.cap.sub_account_id})
            stopped=pool.submit(client.post,path+'/stop',headers=headers,
                json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'})
            try: assert stopped.result(timeout=2).status_code==200
            finally: release.set()
            assert history.result(timeout=10).status_code==200
        assert wait_finished(client,path,headers)['run']['status']=='STOPPED'
        assert not case.controls['sent'] and len(logins)==1


def test_all_close_routes_reject_anonymous_and_cross_origin_requests(tmp_path):
    with close_api(tmp_path) as (case,request,client,headers,logins):
        path=ROOT+'/'+case.origin['client_order_id']+'/strategy-close'
        for route in ('prepare','start','stop'):
            response=client.post(path+'/'+route,json=body(request))
            assert response.status_code==401
            response=client.post(path+'/'+route,headers={**headers,'Origin':'https://untrusted.invalid'},json=body(request))
            assert response.status_code==403
            assert KEY.hex() not in response.text
        assert client.get(path).status_code==401
        assert not logins and not case.controls['sent']


def test_a_queued_start_does_not_hold_the_http_lock_needed_to_stop_another_close(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    def block(url,payload):
        if url.endswith('/instrument'): entered.set(); assert release.wait(10)
    with close_api(tmp_path,public_hook=block) as (case,request,client,headers,logins):
        path=ROOT+'/'+case.origin['client_order_id']+'/strategy-close'
        assert client.post(path+'/start',headers=headers,json=body(request)).status_code==202
        assert entered.wait(10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            queued=pool.submit(client.post,ROOT+'/'+str(2**63+123)+'/strategy-close/start',headers=headers,json=body(request))
            # La segunda petición entra mientras el primer RPC sigue retenido.
            time.sleep(.05)
            stop=pool.submit(client.post,path+'/stop',headers=headers,
                json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'})
            try: assert stop.result(timeout=2).status_code==200
            finally: release.set()
            assert queued.result(timeout=10).status_code==409
        assert wait_finished(client,path,headers)['run']['status']=='STOPPED'
        assert not case.controls['sent']
