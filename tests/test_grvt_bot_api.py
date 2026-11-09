"""Controles de acceso, revocación y recuperación HTTP del ciclo automático."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import create_real_router
from tests.grvt_bracket_ui_fixture import BracketUiVenue
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY, SIGNER


ROOT = '/real/exchanges/grvt/testnet/bots'


@contextmanager
def bot_api(directory, *, public_hook=None,min_notional=None):
    venue = BracketUiVenue(directory)
    origin = venue.origin.seed('LONG',defer_open=True)
    venue.recorded['private'].clear()
    def public(url,body):
        if public_hook: public_hook(url,body)
        reply=venue.public(url,body)
        if min_notional is not None and url.endswith('/instrument'):
            payload=reply.json(); payload['result']['min_notional']=min_notional
            return venue.origin.response(url,payload)
        return reply
    app = FastAPI(); app.include_router(create_real_router(directory/'real.db',
        lab_path=venue.origin.lab.path,paper_perp_path=venue.origin.paper.path,
        grvt_testnet_factory=venue.factory,grvt_testnet_bracket_public_post=public))
    with TestClient(app,base_url='http://127.0.0.1') as api:
        token = api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers = {'X-Orbis-Vault-Token':token}
        data = {'api_key':'fake-testnet-key-'+venue.cap.sub_account_id,
            'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER,
            'session_id':origin['session_id'],'sub_account_id':venue.cap.sub_account_id,
            'entry_cap':'50','exit_cap':'100','wait_seconds':120,'exit_seconds':120,
            'permission_revision':api.get(ROOT,params={'sub_account_id':venue.cap.sub_account_id},headers=headers).json()['control_revision'],
            'confirmation':'ACTIVAR_UN_CICLO_AUTOMATICO_GRVT_TESTNET'}
        yield venue,origin,api,headers,data
        api.post('/real/vault/lock',headers=headers)
    venue.vault.lock()


def finished(api,path,headers):
    until = time.monotonic()+8
    while time.monotonic() < until:
        response = api.get(path,headers=headers)
        assert response.status_code == 200,response.text
        if not response.json()['active']: return response.json()
        time.sleep(.01)
    raise AssertionError('El ciclo no se detuvo')


def test_private_boundaries_and_an_explicit_single_episode(tmp_path):
    with bot_api(tmp_path) as (venue,origin,api,headers,data):
        assert api.get(ROOT+'/sources').status_code == 401
        assert api.post(ROOT+'/start',json=data).status_code == 401
        assert api.post(ROOT+'/start',json=data,headers={**headers,'Origin':'https://foreign.invalid'}).status_code == 403
        assert api.post(ROOT+'/start',json={**data,'network':'MAINNET'},headers=headers).status_code == 422
        assert api.post(ROOT+'/start',json={**data,'max_entries':20},headers=headers).status_code == 422
        available = api.get(ROOT+'/sources',headers=headers)
        assert any(item['session_id'] == origin['session_id'] for item in available.json()['items'])
        response = api.post(ROOT+'/start',json=data,headers=headers)
        assert response.status_code == 202,response.text
        assert response.headers['cache-control'] == 'no-store'
        run = response.json()['id']; path = ROOT+'/'+run
        assert response.json()['status'] == 'WAITING_SIGNAL'
        assert venue.bulk_sends == 0 and not venue.recorded['private']
        assert api.post(ROOT+'/start',json=data,headers=headers).status_code == 409
        assert api.get(path).status_code == 401
        chart = path+'/executions'
        query = {'sub_account_id':data['sub_account_id']}
        assert api.get(chart,params=query).status_code == 401
        assert api.get(chart,params=query,headers={**headers,'Origin':'https://foreign.invalid'}).status_code == 403
        assert api.post(path+'/stop',json={'confirmation':'DETENER_CICLO_AUTOMATICO_GRVT_TESTNET'}).status_code == 401
        assert api.post(path+'/rearm',json={}).status_code == 401
        assert api.post(path+'/rearm',headers={**headers,'Origin':'https://foreign.invalid'},json={}).status_code == 403
        assert api.post(path+'/stop',headers=headers,json={'confirmation':'DETENER_CICLO_AUTOMATICO_GRVT_TESTNET'}).status_code == 200
        result = finished(api,path,headers)
        assert result['status'] == 'STOPPED' and result['permission_remaining_seconds'] == 0
        assert api.get(chart,params={'sub_account_id':'99999999'},headers=headers).status_code == 404
        fills = api.get(chart,params=query,headers=headers)
        assert fills.status_code == 200 and fills.headers['cache-control'] == 'no-store'
        assert fills.json()['network'] == 'TESTNET' and fills.json()['executions'] == [] and fills.json()['total'] == 0
        export = path+'/research-export'
        body = {**query,'confirmation':'EXPORTAR_JSONL_TESTNET'}
        assert api.post(export,json=body).status_code == 401
        assert api.post(export,json=body,headers={**headers,'Origin':'https://foreign.invalid'}).status_code == 403
        assert api.post(export,json=query,headers=headers).status_code == 422
        assert api.post(export,json={**body,'sub_account_id':'99999999'},headers=headers).status_code == 404
        empty = api.post(export,json=body,headers=headers)
        assert empty.status_code == 409 and empty.headers['cache-control'] == 'no-store'
        assert 'todavía no tiene señales seleccionadas' in empty.json()['detail']
        assert not venue.recorded['private']
        assert api.post(ROOT+'/start',json=data,headers=headers).status_code == 409
        for secret in (KEY.hex(),SIGNER,data['api_key']): assert secret not in str(result)


def test_impossible_minimum_is_an_explicit_configuration_error_without_a_grant(tmp_path):
    with bot_api(tmp_path,min_notional='100') as (venue,origin,api,headers,data):
        reply=api.post(ROOT+'/start',headers=headers,json=data)
        assert reply.status_code == 409
        assert reply.headers['cache-control'] == 'no-store'
        assert reply.json()['detail'] == 'Mínimos de GRVT o protección incompatibles; revisa los topes de entrada y salida'
        state=api.get(ROOT,params={'sub_account_id':venue.cap.sub_account_id},headers=headers).json()
        assert state['items'] == [] and state['control_revision'] == data['permission_revision']
        assert not venue.recorded['private'] and venue.bulk_sends == 0
        started=api.post(ROOT+'/start',headers=headers,json={**data,'entry_cap':'150','exit_cap':'200'})
        assert started.status_code == 202,started.text
        path=ROOT+'/'+started.json()['id']
        api.post(path+'/stop',headers=headers,json={'confirmation':'DETENER_CICLO_AUTOMATICO_GRVT_TESTNET'})
        assert finished(api,path,headers)['status'] == 'STOPPED'


def test_pending_public_read_does_not_block_status_history_or_stop(tmp_path):
    entered = Event(); release = Event(); armed = Event()
    def hook(url,body):
        if armed.is_set() and url.endswith('/ticker'):
            entered.set(); assert release.wait(5)
    with bot_api(tmp_path,public_hook=hook) as (venue,origin,api,headers,data):
        response = api.post(ROOT+'/start',headers=headers,json=data)
        assert response.status_code == 202,response.text
        armed.set()
        path = ROOT+'/'+response.json()['id']
        venue.origin.clock['now'] = int(time.time()); venue.origin.monitor.clock_ns = time.time_ns
        venue.origin.monitor.poll(origin['session_id'])
        assert entered.wait(5)
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                state = pool.submit(api.get,path,headers=headers).result(timeout=.5)
                assert state.status_code == 200 and state.json()['active']
                history = pool.submit(api.get,ROOT,headers=headers,
                    params={'sub_account_id':venue.cap.sub_account_id}).result(timeout=.5)
                assert history.status_code == 200 and history.json()['history_pending'] is True
                stopped = pool.submit(api.post,path+'/stop',headers=headers,
                    json={'confirmation':'DETENER_CICLO_AUTOMATICO_GRVT_TESTNET'}).result(timeout=.5)
                assert stopped.status_code == 200
        finally: release.set()
        assert finished(api,path,headers)['status'] == 'STOPPED' and venue.bulk_sends == 0


def test_subaccount_stop_revokes_a_start_that_has_not_arrived_yet(tmp_path):
    with bot_api(tmp_path) as (venue,origin,api,headers,data):
        result = api.post(ROOT+'/stop',headers=headers,json={
            'sub_account_id':venue.cap.sub_account_id,'confirmation':'DETENER_CICLO_AUTOMATICO_GRVT_TESTNET'})
        assert result.status_code == 200 and result.json()['stop_requested']
        assert api.post(ROOT+'/start',headers=headers,json=data).status_code == 409
        assert not venue.recorded['private'] and venue.bulk_sends == 0


def test_bounded_policy_requires_explicit_aggregate_limits_and_is_revoked_by_the_same_stop(tmp_path):
    with bot_api(tmp_path) as (venue,origin,api,headers,data):
        incomplete = {**data,'max_entries':3}
        assert api.post(ROOT+'/start',headers=headers,json=incomplete).status_code == 422
        policy = {**incomplete,'total_entry_cap':'150','loss_cap':'10','wait_seconds':10800}
        response = api.post(ROOT+'/start',headers=headers,json=policy)
        assert response.status_code == 202,response.text
        saved = response.json()
        assert saved['schema'] == 'orbis.grvt.testnet.bot.run.v2' and saved['max_entries'] == 3
        assert saved['entry_count'] == saved['completed_entries'] == 0
        assert saved['total_entry_cap'] == '150' and saved['loss_cap'] == '10'
        assert api.post(ROOT+'/'+saved['id']+'/stop',headers=headers,
            json={'confirmation':'DETENER_CICLO_AUTOMATICO_GRVT_TESTNET'}).status_code == 200
        assert finished(api,ROOT+'/'+saved['id'],headers)['status'] == 'STOPPED'
        assert venue.bulk_sends == 0 and not venue.recorded['private']


@pytest.mark.parametrize('action',['lock','protect-view','operations','subaccount'])
def test_global_revocation_does_not_restart_a_waiting_bot_after_unlock(tmp_path,action):
    with bot_api(tmp_path) as (venue,origin,api,headers,data):
        response = api.post(ROOT+'/start',headers=headers,json=data)
        assert response.status_code == 202,response.text
        run = response.json()['id']
        if action == 'operations':
            assert api.post('/real/operations/stop',json={'confirmation':'DETENER_Y_BLOQUEAR'}).status_code == 200
        elif action == 'subaccount':
            assert api.post('/real/exchanges/grvt/testnet/subaccounts/'+venue.cap.sub_account_id+'/stop',
                headers=headers,json={'confirmation':'DETENER_GRVT_TESTNET'}).status_code == 200
        else: assert api.post('/real/vault/'+action,headers=headers).status_code == 200
        token = api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        recovered = finished(api,ROOT+'/'+run,{'X-Orbis-Vault-Token':token})
        assert recovered['status'] == 'STOPPED' and not recovered['active']
        assert venue.bulk_sends == 0 and not venue.recorded['private']
