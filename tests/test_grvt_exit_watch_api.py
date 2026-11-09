"""API local → observador → cierre real del motor, fronteras externas ficticias."""

from contextlib import contextmanager
from decimal import Decimal
import time
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.paper_math import PerpQuote
from app.portfolio.api import create_real_router
from tests.test_grvt_filled_strategy_close import filled_case
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER


ROOT='/real/exchanges/grvt/testnet/brackets'


@contextmanager
def watch_api(directory,*,prices=('100','108.1'),stream_hook=None,factory_hook=None):
    venue,reader,public,clock,state=filled_case(directory); frames=iter(prices); logins=[]
    class Stream:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def recv(self,*,timeout):
            if stream_hook is not None: stream_hook()
            clock[0]+=1_000_000_000
            value=next(frames,'100')
            return 'BTC/USDT:PERP',PerpQuote(venue.cap.instrument,clock[0],Decimal(value),Decimal('99.99'),Decimal('100.01'))
    def factory(key):
        logins.append(True)
        if factory_hook is not None: factory_hook()
        return venue.factory(key)
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    app=FastAPI()
    app.include_router(create_real_router(directory/'real.db',grvt_testnet_factory=factory,
        grvt_testnet_bracket_public_post=public,grvt_testnet_bracket_clock_ns=lambda:clock[0],
        grvt_testnet_bracket_close_waiter=wait,grvt_testnet_exit_stream_factory=lambda symbol,clock:Stream()))
    with TestClient(app,base_url='http://127.0.0.1') as api:
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        venue.permission_revision=api.get(ROOT+'/'+venue.source['client_order_id']+'/strategy-close',
            headers={'X-Orbis-Vault-Token':token}).json()['control_revision']
        yield venue,api,{'X-Orbis-Vault-Token':token},clock,logins
    venue.vault.lock()


def body(venue):
    return {'api_key':'fake-testnet-key-'+venue.cap.sub_account_id,'dedicated_private_key':KEY.hex(),
        'expected_signer':SIGNER,'max_attempts':2,'ttl_seconds':120,
        'permission_revision':venue.permission_revision,
        'confirmation':'ACTIVAR_SALIDAS_ESTRATEGIA_GRVT_TESTNET'}


@pytest.mark.parametrize('route',['strategy-exit','native-recovery'])
def test_a_delayed_activation_from_before_stop_is_rejected_before_login(tmp_path,route):
    with watch_api(tmp_path) as (venue,api,headers,clock,logins):
        path=ROOT+'/'+venue.source['client_order_id']
        current=api.get(path+'/strategy-close',headers=headers).json()
        assert 'control_revision' in current
        data={**body(venue),'permission_revision':current['control_revision']}
        if route=='native-recovery': data['confirmation']='RECUPERAR_GRUPO_GRVT_TESTNET'
        assert api.post(path+'/strategy-close/stop',headers=headers,
            json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'}).status_code==200
        response=api.post(path+'/'+route+'/start',headers=headers,json=data)
        assert response.status_code==409,response.text
        assert not logins and venue.cancel_sends==venue.close_sends==0


def test_stop_is_archived_and_new_service_only_recovers_the_record(tmp_path):
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    entered,release=Event(),Event()
    def blocked(): entered.set(); assert release.wait(5)
    with watch_api(tmp_path,stream_hook=blocked) as (venue,api,headers,clock,logins):
        path=ROOT+'/'+venue.source['client_order_id']
        assert api.post(path+'/strategy-exit/start',headers=headers,json=body(venue)).status_code==202
        assert entered.wait(5)
        try:
            assert api.post(path+'/strategy-close/stop',headers=headers,
                json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'}).status_code==200
        finally: release.set()
        assert wait_finished(api,path,headers)['run']['status']=='STOPPED'
        history=api.get(path+'/exit-watches',headers=headers)
        assert history.status_code==200,history.text
        saved=history.json()['items'][0]
        assert saved['status']=='STOPPED' and saved['stop_cause']=='USER_STOP'
        assert saved['authorization'] is False and saved['event_count']>=3
        events=api.get(path+'/exit-watches/'+saved['id'],headers=headers).json()['events']
        assert events[0]['kind']=='STARTED' and events[-1]['kind']=='TERMINAL'
        assert api.get(path+'/exit-watches').status_code==401
        assert api.get(path+'/exit-watches/'+saved['id']).status_code==401
        def forbidden(key): raise AssertionError('La reapertura no autentica ni envía órdenes')
        new_service=GrvtStrategyCloseService(venue.vault,client_factory=forbidden,public_post=forbidden,clock_ns=lambda:clock[0])
        recovered=new_service.snapshot(venue.source['client_order_id'])
        assert recovered['run']['status']=='STOPPED' and recovered['run']['active'] is False
        assert recovered['run']['permission_remaining_seconds']==0
        assert len(logins)==1 and venue.cancel_sends==venue.close_sends==0


def wait_finished(api,path,headers):
    until=time.monotonic()+10
    while time.monotonic()<until:
        response=api.get(path+'/strategy-close',headers=headers)
        assert response.status_code==200,response.text
        result=response.json()
        if result['run'] is not None and not result['run']['active']: return result
        time.sleep(.01)
    raise AssertionError('Seguimiento sin resultado terminal')


def test_private_api_waits_for_price_and_closes_with_one_activation(tmp_path):
    with watch_api(tmp_path) as (venue,api,headers,clock,logins):
        path=ROOT+'/'+venue.source['client_order_id']; data=body(venue)
        assert api.post(path+'/strategy-exit/start',json=data).status_code==401
        assert api.post(path+'/strategy-exit/start',headers={**headers,'Origin':'https://foreign.example'},json=data).status_code==403
        assert api.post(path+'/strategy-exit/start',headers=headers,json={**data,'mainnet':True}).status_code==422
        assert not logins
        response=api.post(path+'/strategy-exit/start',headers=headers,json=data)
        assert response.status_code==202,response.text
        assert response.headers['cache-control']=='no-store'
        result=wait_finished(api,path,headers)
        assert result['run']['status']=='COMPLETE' and result['run']['watch']['observations']==2
        assert result['request']['schema']=='orbis.grvt.testnet.strategy.close.v2'
        assert result['request']['exit_reason']=='TARGET'
        assert venue.cancel_sends==venue.close_sends==len(logins)==1
        for secret in (KEY.hex(),SIGNER,data['api_key']): assert secret not in str(result) and secret not in response.text


def test_expired_group_recovery_has_its_own_private_confirmation_and_short_permission(tmp_path):
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    with watch_api(tmp_path,prices=()) as (venue,api,headers,clock,logins):
        path=ROOT+'/'+venue.source['client_order_id']
        clock[0]=read_bracket(venue.vault,venue.source['client_order_id'])['legs'][1]['expiration_ns']+1_000_000_000
        data={**body(venue),'confirmation':'RECUPERAR_GRUPO_GRVT_TESTNET'}
        assert api.post(path+'/native-recovery/start',json=data).status_code==401
        assert api.post(path+'/native-recovery/start',headers={**headers,'Origin':'https://foreign.example'},json=data).status_code==403
        assert api.post(path+'/native-recovery/start',headers=headers,json=body(venue)).status_code==422
        assert api.post(path+'/native-recovery/start',headers=headers,json={**data,'ttl_seconds':301}).status_code==422
        assert api.post(path+'/native-recovery/start',headers=headers,json={**data,'mainnet':True}).status_code==422
        assert not logins
        response=api.post(path+'/native-recovery/start',headers=headers,json=data)
        assert response.status_code==202,response.text
        assert response.headers['cache-control']=='no-store'
        result=wait_finished(api,path,headers)
        assert result['run']['status']=='COMPLETE' and result['run']['watch']['observations']==0
        assert result['request']['exit_reason']=='EXIT_PROTECTION_EXPIRED'
        history=api.get(path+'/exit-watches',headers=headers).json()['items'][0]
        assert history['mode']=='RECOVERY' and history['authorization'] is False
        assert venue.cancel_sends==venue.close_sends==1 and venue.position==0
        for secret in (KEY.hex(),SIGNER,data['api_key']): assert secret not in str(result) and secret not in response.text


def test_pending_authentication_does_not_block_status_or_stop(tmp_path):
    entered,release=Event(),Event()
    def stalled(): entered.set(); assert release.wait(5)
    with watch_api(tmp_path,factory_hook=stalled) as (venue,api,headers,clock,logins):
        path=ROOT+'/'+venue.source['client_order_id']
        with ThreadPoolExecutor(max_workers=3) as pool:
            start=pool.submit(api.post,path+'/strategy-exit/start',headers=headers,json=body(venue))
            assert entered.wait(5)
            try:
                status=pool.submit(api.get,path+'/strategy-close',headers=headers).result(timeout=.5)
                assert status.status_code==200 and status.json()['run']['active']
                stop=pool.submit(api.post,path+'/strategy-close/stop',headers=headers,
                    json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'}).result(timeout=.5)
                assert stop.status_code==200
            finally: release.set()
            assert start.result(timeout=5).status_code==202
        assert wait_finished(api,path,headers)['run']['status']=='STOPPED'
        assert venue.cancel_sends==venue.close_sends==0


@pytest.mark.parametrize('cause',['stop','lock','protect'])
def test_watching_can_be_revoked_without_closing_or_restarting_on_unlock(tmp_path,cause):
    entered,release=Event(),Event()
    def blocked(): entered.set(); assert release.wait(5)
    with watch_api(tmp_path,stream_hook=blocked) as (venue,api,headers,clock,logins):
        path=ROOT+'/'+venue.source['client_order_id']
        assert api.post(path+'/strategy-exit/start',headers=headers,json=body(venue)).status_code==202
        assert entered.wait(5)
        try:
            assert api.post(path+'/strategy-exit/start',headers=headers,json=body(venue)).status_code==409
            if cause=='stop':
                assert api.post(path+'/strategy-close/stop',headers=headers,
                    json={'confirmation':'DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET'}).status_code==200
            else:
                endpoint='/real/vault/lock' if cause=='lock' else '/real/vault/protect-view'
                assert api.post(endpoint,headers=headers).status_code==200
                assert api.get(path+'/strategy-close',headers=headers).status_code==401
                headers={'X-Orbis-Vault-Token':api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        finally: release.set()
        result=wait_finished(api,path,headers)
        assert result['run']['status']=='STOPPED' and result['request'] is None
        assert venue.cancel_sends==venue.close_sends==0 and len(logins)==1


@pytest.mark.parametrize('fault',['signer','boolean','too_long','confirmation'])
def test_invalid_activation_is_rejected_before_login(tmp_path,fault):
    with watch_api(tmp_path) as (venue,api,headers,clock,logins):
        data=body(venue)
        if fault=='signer': data['expected_signer']='0x'+'f'*40
        if fault=='boolean': data['max_attempts']=True
        if fault=='too_long': data['ttl_seconds']=86400
        if fault=='confirmation': data['confirmation']='OTHER'
        response=api.post(ROOT+'/'+venue.source['client_order_id']+'/strategy-exit/start',headers=headers,json=data)
        assert response.status_code==(422 if fault in ('boolean','confirmation') else 409),response.text
        assert not logins and venue.cancel_sends==venue.close_sends==0
        assert KEY.hex() not in response.text and data['api_key'] not in response.text


def test_a_hold_does_not_extend_permission_or_create_an_order_when_it_expires(tmp_path):
    with watch_api(tmp_path,prices=('100',)) as (venue,api,headers,clock,logins):
        path=ROOT+'/'+venue.source['client_order_id']; data={**body(venue),'ttl_seconds':30}
        assert api.post(path+'/strategy-exit/start',headers=headers,json=data).status_code==202
        result=wait_finished(api,path,headers)
        assert result['run']['status']=='EXPIRED' and result['request'] is None
        assert venue.cancel_sends==venue.close_sends==0 and venue.position==Decimal('0.5')


def test_a_stream_gap_stops_before_using_a_later_profitable_price(tmp_path):
    clock_ref=[]
    def jump(): clock_ref[0][0]+=6_000_000_000
    with watch_api(tmp_path,stream_hook=jump) as (venue,api,headers,clock,logins):
        clock_ref.append(clock); path=ROOT+'/'+venue.source['client_order_id']
        assert api.post(path+'/strategy-exit/start',headers=headers,json=body(venue)).status_code==202
        result=wait_finished(api,path,headers)
        assert result['run']['status']=='GAP' and result['request'] is None
        assert venue.cancel_sends==venue.close_sends==0
