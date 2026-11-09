"""API OCO protegida: diario/motores reales, nunca credenciales del usuario."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from contextlib import contextmanager
import pytest

from app.portfolio.api import create_real_router
from tests.test_grvt_testnet_oco import ready
from tests.test_grvt_testnet_journal import SUB, CID


@contextmanager
def oco_session(directory, *, direction='LONG'):
    from tests.grvt_oco_ui_fixture import OcoUiVenue,PASSWORD
    from tests.test_grvt_testnet_oco import STAMP
    clock = [STAMP+10_000_000_000]
    venue = OcoUiVenue(directory,clock_ns=lambda:clock[0],direction=direction)
    app = FastAPI()
    app.include_router(create_real_router(directory/'real.db',grvt_testnet_factory=venue.factory,
        grvt_testnet_oco_public_post=venue.public,grvt_testnet_oco_clock_ns=lambda:clock[0]))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        token = client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        yield client,{'X-Orbis-Vault-Token':token},venue,clock


ROOT = '/real/exchanges/grvt/testnet/oco'


def preview_request(client,headers,*,direction='LONG',**changes):
    body = {'sub_account_id':SUB,'parent_client_order_id':CID,'api_key':'fake-testnet-key',
        'max_order_notional':'1000','stop_loss':'49000' if direction == 'LONG' else '51000',
        'take_profit':'53000' if direction == 'LONG' else '47000',
        'max_slippage_bps':25,'ttl_minutes':60,**changes}
    return client.post(ROOT+'/preview',headers=headers,json=body)


def signed_request(prepared):
    from tests.test_grvt_testnet_protection import KEY,SIGNER
    return {'preview':prepared['preview'],'preview_mac':prepared['preview_mac'],
        'api_key':'fake-testnet-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER,
        'confirmation':'FIRMAR_OCO_GRVT_TESTNET'}


def test_protection_panel_lists_local_source_without_contacting_the_exchange(tmp_path):
    vault = ready(tmp_path); vault.lock()
    calls = []
    def factory(key):
        calls.append('login')
        raise AssertionError('El listado local no autentica con GRVT')
    app = FastAPI()
    app.include_router(create_real_router(tmp_path/'real.db',grvt_testnet_factory=factory))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        path = '/real/exchanges/grvt/testnet/oco?sub_account_id='+SUB
        assert client.get(path).status_code == 401
        unlocked = client.post('/real/vault/unlock',json={'passphrase':'long test passphrase'})
        assert unlocked.status_code == 200,unlocked.text
        headers = {'X-Orbis-Vault-Token':unlocked.json()['token']}
        response = client.get(path,headers=headers)
        assert response.status_code == 200,response.text
        assert response.headers['cache-control'] == 'no-store'
        data = response.json()
        assert data['network'] == 'TESTNET' and data['authorization'] is False
        assert data['sources'][0]['parent_client_order_id'] == CID
        assert data['sources'][0]['side'] == 'SELL' and data['sources'][0]['size'] == '0.01'
        assert data['groups'] == [] and data['stopped'] is False
        assert calls == []


def test_preview_derives_full_position_and_prices_without_signing_or_sending(tmp_path):
    from tests.grvt_oco_ui_fixture import OcoUiVenue,PASSWORD
    venue = OcoUiVenue(tmp_path)
    app = FastAPI()
    app.include_router(create_real_router(tmp_path/'real.db',grvt_testnet_factory=venue.factory,
        grvt_testnet_oco_public_post=venue.public))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        unlocked = client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()
        response = client.post('/real/exchanges/grvt/testnet/oco/preview',
            headers={'X-Orbis-Vault-Token':unlocked['token']},json={'sub_account_id':SUB,
                'parent_client_order_id':CID,'api_key':'fake-testnet-key','max_order_notional':'1000',
                'stop_loss':'49000','take_profit':'53000','max_slippage_bps':25,'ttl_minutes':60})
        assert response.status_code == 200,response.text
        data = response.json()
        assert data['network'] == 'TESTNET' and data['can_submit'] is False
        assert data['preview']['size'] == '0.01' and data['preview']['side'] == 'SELL'
        assert [leg['limit_price'] for leg in data['preview']['legs']] == ['48877.5','52867.5']
        assert venue.bulk_sends == 0 and venue.orders == {}
        assert 'fake-testnet-key' not in response.text


def test_reviewed_protection_sends_one_native_pair_and_cannot_be_replayed(tmp_path):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        prepared = preview_request(client,headers)
        assert prepared.status_code == 200,prepared.text
        payload = signed_request(prepared.json())
        response = client.post(ROOT+'/send',headers=headers,json=payload)
        assert response.status_code == 200,response.text
        group = response.json()['group']
        assert group['status'] == 'ACKNOWLEDGED'
        assert group['protection_guaranteed'] is False and group['reconciliation'] is None
        assert venue.bulk_sends == 1 and len(venue.orders) == 2
        assert all(o['reduce_only'] is True for o in venue.orders.values())
        calls = list(venue.calls)
        duplicate = client.post(ROOT+'/send',headers=headers,json=payload)
        assert duplicate.status_code == 409 and venue.calls == calls
        history = client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()
        assert len(history['groups']) == 1 and history['groups'][0]['group_id'] == group['group_id']
        assert not history['sources'][0]['eligible']
        for secret in (payload['dedicated_private_key'],payload['expected_signer'],payload['api_key']):
            assert secret not in response.text and secret not in str(history)


def test_user_can_observe_audit_and_reopen_a_financially_reconciled_pair(tmp_path):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        prepared = preview_request(client,headers).json()
        sent = client.post(ROOT+'/send',headers=headers,json=signed_request(prepared))
        assert sent.status_code == 200,sent.text
        path = ROOT+'/'+prepared['preview']['group_id']
        key = {'api_key':'fake-testnet-key'}
        observed = client.post(path+'/observe',headers=headers,json=key)
        assert observed.status_code == 200,observed.text
        assert [row['status'] for row in observed.json()['group']['last_observation']['orders']] == ['OPEN','OPEN']
        # Sin estados terminales ni dos cuentas estables, no hay recibo financiero.
        consent = {'confirmation':'CONCILIAR_OCO_GRVT_TESTNET'}
        assert client.post(path+'/reconcile',headers=headers,json=consent).status_code == 409
        clock[0] += 1_000_000_000; venue.trigger()
        first = client.post(path+'/audit',headers=headers,json=key)
        assert first.status_code == 200,first.text
        assert first.json()['group']['financial_audit_count'] == 1
        assert client.post(path+'/reconcile',headers=headers,json=consent).status_code == 409
        clock[0] += 5_100_000_000
        second = client.post(path+'/audit',headers=headers,json=key)
        assert second.status_code == 200,second.text
        panel = client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()
        assert panel['groups'][0]['can_reconcile'] is True
        reconciled = client.post(path+'/reconcile',headers=headers,json=consent)
        assert reconciled.status_code == 200,reconciled.text
        receipt = reconciled.json()['group']['reconciliation']
        assert receipt['result']['positions'] == []
        assert receipt['result']['net_realized_pnl_usdt'] == '29.93'
        assert receipt['mainnet_order_sent'] is False and receipt['real_book_events_created'] is False
        assert client.post(path+'/reconcile',headers=headers,json=consent).json()['group']['reconciliation'] == receipt
        assert venue.bulk_sends == 1 and venue.cancel_sends == 0
        client.post('/real/vault/lock',headers=headers)
        from tests.grvt_oco_ui_fixture import PASSWORD
        token = client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        panel = client.get(ROOT,headers={'X-Orbis-Vault-Token':token},params={'sub_account_id':SUB}).json()
        assert panel['groups'][0]['reconciliation'] == receipt and panel['sources'] == []


def test_explicit_cancel_removes_both_exits_but_keeps_position_and_stop(tmp_path):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        prepared = preview_request(client,headers).json()
        assert client.post(ROOT+'/send',headers=headers,json=signed_request(prepared)).status_code == 200
        path = ROOT+'/'+prepared['preview']['group_id']
        consent = {'api_key':'fake-testnet-key','confirmation':'RETIRAR_PROTECCIONES_OCO_TESTNET',
            'acknowledge_unprotected_position':True}
        calls = list(venue.calls)
        refused = client.post(path+'/cancel',headers=headers,json={**consent,'acknowledge_unprotected_position':False})
        assert refused.status_code == 422 and venue.calls == calls
        cancelled = client.post(path+'/cancel',headers=headers,json=consent)
        assert cancelled.status_code == 200,cancelled.text
        assert all(row['status'] == 'CANCELLED' for row in cancelled.json()['group']['last_observation']['orders'])
        assert cancelled.json()['group']['reconciliation'] is None
        assert venue.cancel_sends == 1 and str(venue.position) == '0.01'
        assert client.post(path+'/cancel',headers=headers,json=consent).status_code == 409
        assert venue.cancel_sends == 1
        for interval in (1_000_000_000,5_100_000_000):
            clock[0] += interval
            audit = client.post(path+'/audit',headers=headers,json={'api_key':'fake-testnet-key'})
            assert audit.status_code == 200,audit.text
        result = client.post(path+'/reconcile',headers=headers,json={'confirmation':'CONCILIAR_OCO_GRVT_TESTNET'})
        assert result.status_code == 200,result.text
        assert result.json()['group']['reconciliation']['result']['positions'] == [['BTC_USDT_Perp','0.01']]
        panel = client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()
        assert panel['stopped'] is True and not panel['sources'][0]['eligible']


@pytest.mark.parametrize('change',['size','price','cap','sub','timestamp','mac','age','session','stop'])
def test_untrusted_or_revoked_preview_is_rejected_before_exchange_login(tmp_path,change):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        data = preview_request(client,headers).json()
        if change == 'size': data['preview']['size'] = '1'
        if change == 'price': data['preview']['legs'][0]['trigger_price'] = '1'
        if change == 'cap': data['preview']['max_order_notional'] = '10000'
        if change == 'sub': data['preview']['sub_account_id'] = '456789'
        if change == 'timestamp': data['preview']['created_at_ns'] = int(data['preview']['created_at_ns'])
        if change == 'mac': data['preview_mac'] = '0'*64
        if change == 'age': clock[0] += 121_000_000_000
        if change == 'session':
            from tests.grvt_oco_ui_fixture import PASSWORD
            client.post('/real/vault/lock',headers=headers)
            token = client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
            headers = {'X-Orbis-Vault-Token':token}
        if change == 'stop':
            stopped = client.post('/real/exchanges/grvt/testnet/subaccounts/'+SUB+'/stop',
                headers=headers,json={'confirmation':'DETENER_GRVT_TESTNET'})
            assert stopped.status_code == 200,stopped.text
        calls = list(venue.calls)
        response = client.post(ROOT+'/send',headers=headers,json=signed_request(data))
        assert response.status_code == 409,response.text
        assert response.headers['cache-control'] == 'no-store'
        assert venue.calls == calls and venue.bulk_sends == 0
        assert client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()['groups'] == []


@pytest.mark.parametrize('change',[{'max_order_notional':1000},{'max_order_notional':'1001'},
    {'max_slippage_bps':True},{'max_slippage_bps':101},{'ttl_minutes':0},
    {'size':'1'},{'api_key':123},{'api_key':''},{'sub_account_id':'000\n'},
    {'parent_client_order_id':'1'},{'stop_loss':'NaN'}])
def test_preview_validation_does_not_echo_credentials_or_contact_provider(tmp_path,change):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        response = preview_request(client,headers,**change)
        assert response.status_code in (409,422),response.text
        assert response.headers['cache-control'] == 'no-store'
        assert 'fake-testnet-key' not in response.text
        assert venue.calls == [] and venue.bulk_sends == 0


def test_lost_bulk_reply_is_recovered_after_lock_without_resending(tmp_path):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        data = preview_request(client,headers).json()
        venue.lose_create_reply = True
        sent = client.post(ROOT+'/send',headers=headers,json=signed_request(data))
        assert sent.status_code == 409 and venue.bulk_sends == 1
        panel = client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()
        assert panel['groups'][0]['status'] == 'POST_CLAIMED'
        client.post('/real/vault/lock',headers=headers)
        from tests.grvt_oco_ui_fixture import PASSWORD
        token = client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers = {'X-Orbis-Vault-Token':token}
        calls = list(venue.calls)
        assert client.post(ROOT+'/send',headers=headers,json=signed_request(data)).status_code == 409
        assert preview_request(client,headers).status_code == 409 and venue.calls == calls
        observed = client.post(ROOT+'/'+data['preview']['group_id']+'/observe',headers=headers,
            json={'api_key':'fake-testnet-key'})
        assert observed.status_code == 200,observed.text
        assert all(row['status'] == 'OPEN' for row in observed.json()['group']['last_observation']['orders'])
        assert venue.bulk_sends == 1


def test_preflight_failure_can_discard_only_unclaimed_preparation(tmp_path):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        data = preview_request(client,headers).json()
        venue.reject_auth = True
        assert client.post(ROOT+'/send',headers=headers,json=signed_request(data)).status_code == 409
        group = client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()['groups'][0]
        assert group['status'] == 'PREPARED' and venue.bulk_sends == 0
        path = ROOT+'/'+group['group_id']+'/abandon'
        calls = list(venue.calls)
        result = client.post(path,headers=headers,json={'confirmation':'DESCARTAR_OCO_GRVT_TESTNET'})
        assert result.status_code == 200,result.text
        assert result.json()['group']['status'] == 'ABANDONED' and venue.calls == calls
        assert client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()['sources'][0]['eligible']


def test_short_position_generates_buy_to_close_with_matching_native_triggers(tmp_path):
    with oco_session(tmp_path,direction='SHORT') as (client,headers,venue,clock):
        response = preview_request(client,headers,direction='SHORT')
        assert response.status_code == 200,response.text
        preview = response.json()['preview']
        assert preview['side'] == 'BUY' and preview['size'] == '0.01'
        assert [leg['limit_price'] for leg in preview['legs']] == ['51127.5','47117.5']
        sent = client.post(ROOT+'/send',headers=headers,json=signed_request(response.json()))
        assert sent.status_code == 200,sent.text
        assert all(order['legs'][0]['is_buying_asset'] is True for order in venue.orders.values())


def test_review_validity_never_outlives_the_native_orders(tmp_path):
    with oco_session(tmp_path) as (client,headers,venue,clock):
        response = preview_request(client,headers,ttl_minutes=1)
        assert response.status_code == 200,response.text
        data = response.json()
        assert data['preview']['expires_at_ms'] <= int(data['preview']['signature_expiration_ns'])//1_000_000


def test_private_routes_require_local_origin_and_current_vault_session(tmp_path):
    from uuid import uuid4
    with oco_session(tmp_path) as (client,headers,venue,clock):
        path = ROOT+'/'+str(uuid4())
        requests = [(ROOT+'/preview',{'api_key':'do-not-echo-api-key'}),
            (ROOT+'/send',{'dedicated_private_key':'do-not-echo-signing-key'}),
            *[(path+'/'+action,{'api_key':{'secret':'do-not-echo-api-key'}})
              for action in ('observe','audit','reconcile','cancel','abandon')]]
        for route,body in requests:
            assert client.post(route,json=body).status_code == 401
            external = client.post(route,headers={**headers,'Origin':'https://foreign.example'},json=body)
            assert external.status_code == 403
            invalid = client.post(route,headers=headers,json=body)
            assert invalid.status_code == 422
            assert 'do-not-echo' not in invalid.text
        assert venue.calls == []


def test_invented_group_does_not_authenticate_or_apply_stop(tmp_path):
    from uuid import uuid4
    with oco_session(tmp_path) as (client,headers,venue,clock):
        path = ROOT+'/'+str(uuid4())
        for action in ('observe','audit','cancel'):
            body = {'api_key':'fake-testnet-key'}
            if action == 'cancel': body.update(confirmation='RETIRAR_PROTECCIONES_OCO_TESTNET',acknowledge_unprotected_position=True)
            assert client.post(path+'/'+action,headers=headers,json=body).status_code == 409
        assert venue.calls == []
        assert client.get(ROOT,headers=headers,params={'sub_account_id':SUB}).json()['stopped'] is False
