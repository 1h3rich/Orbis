"""API local real, SQLCipher y firmas; sólo la frontera HTTP de GRVT es controlada."""

from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import create_real_router
from tests.test_grvt_mainnet_submit import live_case,PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER

ROOT='/real/exchanges/grvt/mainnet'


@contextmanager
def api_case(directory,case_factory=live_case,waiter=None):
    case=case_factory(directory);case.vault.lock()
    app=FastAPI();app.include_router(create_real_router(case.vault.path,
        grvt_mainnet_factory=lambda key:case.factory(),grvt_mainnet_public_post=case.public,
        grvt_mainnet_clock_ns=lambda:case.clock[0],grvt_mainnet_waiter=waiter(case) if waiter else None))
    with TestClient(app,base_url='http://127.0.0.1') as api:
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers={'X-Orbis-Vault-Token':token}
        case.vault.unlock(PASSWORD)
        yield case,api,headers
        api.post('/real/vault/lock',headers=headers)
    case.vault.lock()


def connect(api,headers):
    revision=api.get(ROOT,headers={key:value for key,value in headers.items() if key.lower()!='origin'},
        params={'sub_account_id':'123456'}).json()['permission_revision']
    return api.post(ROOT+'/connect',headers=headers,json={'sub_account_id':'123456','api_key':'synthetic-mainnet-api-key',
        'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER,'permission_revision':revision,'confirmation':'CONECTAR_GRVT_MAINNET'})


def terms():
    return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','direction':'LONG','size':'0.5','entry_price':'100',
        'stop_loss':'95','take_profit':'110','entry_cap':'100','exit_cap':'100','exit_seconds':3600,
        'max_spread_bps':25,'max_slippage_bps':25}


def test_explicit_review_then_one_mainnet_send_and_local_recovery_after_lock(tmp_path):
    with api_case(tmp_path) as (case,api,headers):
        assert api.get(ROOT,params={'sub_account_id':'123456'}).status_code==401
        assert api.get(ROOT+'/subaccounts').status_code==401
        assert connect(api,{**headers,'Origin':'https://foreign.invalid'}).status_code==403
        connection=connect(api,headers);assert connection.status_code==200,connection.text
        assert connection.headers['cache-control']=='no-store'
        revision=connection.json()['permission_revision']
        assert api.post(ROOT+'/preview',headers=headers,json={**terms(),'permission_revision':revision,'network':'TESTNET'}).status_code==422
        preview=api.post(ROOT+'/preview',headers=headers,json={**terms(),'permission_revision':revision})
        assert preview.status_code==200,preview.text
        review=preview.json();parent=review['review']['parent_client_order_id']
        assert len(case.sent)==0 and review['review']['network']=='MAINNET'
        body={**review,'permission_revision':revision,'confirmation':'ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET'}
        result=api.post(ROOT+'/send',headers=headers,json=body)
        assert result.status_code==200,result.text
        assert result.json()['group']['status']=='ACKNOWLEDGED' and len(case.sent)==1
        assert api.post(ROOT+'/send',headers=headers,json=body).status_code==409 and len(case.sent)==1
        history=api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).json()
        assert history['groups'][0]['parent_client_order_id']==parent
        assert api.get(ROOT+'/subaccounts',headers=headers).json()['sub_account_ids']==['123456']
        for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key','synthetic-mainnet-session'):
            assert secret not in str(result.json()) and secret not in str(history)
        api.post('/real/vault/lock',headers=headers)
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers={'X-Orbis-Vault-Token':token}
        history=api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).json()
        assert history['connected'] is False and history['groups'][0]['parent_client_order_id']==parent


def test_review_is_bound_to_exact_terms_session_and_revision(tmp_path):
    with api_case(tmp_path) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        reviewed=api.post(ROOT+'/preview',headers=headers,json={**terms(),'permission_revision':revision}).json()
        altered={**reviewed,'review':{**reviewed['review'],'draft':{**reviewed['review']['draft'],'size':'0.6'}},
            'permission_revision':revision,'confirmation':'ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET'}
        assert api.post(ROOT+'/send',headers=headers,json=altered).status_code==409 and not case.sent
        assert api.post(ROOT+'/stop',headers=headers,json={'sub_account_id':'123456','confirmation':'DETENER_GRVT_MAINNET'}).status_code==200
        original={**reviewed,'permission_revision':revision,'confirmation':'ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET'}
        assert api.post(ROOT+'/send',headers=headers,json=original).status_code==409 and not case.sent


def test_lost_send_reply_is_read_by_the_reviewed_parent_without_another_post(tmp_path):
    with api_case(tmp_path) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        reviewed=api.post(ROOT+'/preview',headers=headers,json={**terms(),'permission_revision':revision}).json()
        case.settings['lose']=True
        body={**reviewed,'permission_revision':revision,'confirmation':'ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET'}
        assert api.post(ROOT+'/send',headers=headers,json=body).status_code==409
        assert api.post(ROOT+'/send',headers=headers,json=body).status_code==409 and len(case.sent)==1
        parent=reviewed['review']['parent_client_order_id']
        result=api.get(ROOT+'/groups/'+parent,headers=headers)
        assert result.status_code==200 and result.json()['group']['status']=='POST_CLAIMED'


def advancing_waiter(case):
    def wait(stop,seconds):
        case.clock[0]+=int(seconds*1_000_000_000)
        return stop.wait(.001)
    return wait


def wait_job(api,headers,parent):
    import time
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        result=api.get(ROOT+'/groups/'+parent,headers=headers).json()
        job=result.get('job') if result.get('pending') else result['group']['job']
        if not job['active']:return result if result.get('pending') else result['group']
        time.sleep(.02)
    raise AssertionError('worker did not finish')


def test_sizing_uses_server_decimals_and_cannot_send_or_exceed_budget(tmp_path):
    with api_case(tmp_path) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        body={'sub_account_id':'123456','permission_revision':revision,'instrument':'BTC_USDT_Perp','direction':'LONG',
            'entry_cap':'100','exit_cap':'120','stop_percent':'3','take_percent':'8'}
        result=api.post(ROOT+'/sizing',headers=headers,json=body)
        assert result.status_code==200,result.text
        draft=result.json()['draft']
        assert draft['size']=='0.999' and draft['entry_price']=='100.01'
        assert draft['stop_loss']=='97.01' and draft['take_profit']=='108.02'
        assert not case.sent and result.json()['authorization'] is False
        assert api.post(ROOT+'/sizing',headers=headers,json={**body,'entry_cap':'1001'}).status_code==409
        assert api.post(ROOT+'/sizing',headers=headers,json={**body,'direction':'SHORT'}).status_code==200
        assert api.post(ROOT+'/sizing',headers=headers,json={**body,'stop_percent':'NaN'}).status_code==422


def test_explicit_recovery_closes_only_residue_and_conciles_exact_net(tmp_path):
    from tests.test_grvt_mainnet_recovery import partial_case
    with api_case(tmp_path,partial_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        body={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':3,
            'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}
        result=api.post(ROOT+'/groups/'+case.parent+'/recover',headers=headers,json=body)
        assert result.status_code==200,result.text
        group=wait_job(api,headers,case.parent)
        assert group['job']['status']=='COMPLETE',group
        assert group['status']=='RECONCILED' and group['result']['net_realized_pnl_usdt']=='1.96'
        assert len(case.settings['reduction_posts'])==1
        assert group['reductions'][0]['state']['traded_size']=='0.3'
        assert int(group['exit_expiration_ns'])>case.clock[0]
        assert case.settings['reduction_posts'][0]['orders'][0]['legs'][0]['size']=='0.3'
        assert api.post(ROOT+'/groups/'+case.parent+'/recover',headers=headers,json=body).status_code==409
        assert len(case.settings['reduction_posts'])==1


def test_lost_reduction_reply_is_recovered_without_second_ioc(tmp_path):
    from tests.test_grvt_mainnet_recovery import partial_case
    with api_case(tmp_path,partial_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];case.settings['reduction_lost']=True
        result=api.post(ROOT+'/groups/'+case.parent+'/recover',headers=headers,json={
            'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':3,
            'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'})
        assert result.status_code==200,result.text
        group=wait_job(api,headers,case.parent)
        assert group['job']['status']=='COMPLETE',group
        assert len(case.settings['reduction_posts'])==1 and group['result']['net_realized_pnl_usdt']=='1.96'


def view_terms(api, headers, *, sub='123456'):
    state = api.get(ROOT, headers=headers, params={'sub_account_id': sub}).json()
    return {'sub_account_id': sub, 'permission_revision': state['permission_revision'],
            'api_key': 'synthetic-mainnet-view-key'}


def test_view_reconnects_to_archived_group_and_conciles_without_signing(tmp_path):
    from tests.test_grvt_mainnet_finance import closed_case

    with api_case(tmp_path, closed_case) as (case, api, headers):
        data = view_terms(api, headers)
        path = ROOT + '/connect-view'
        assert api.post(path, json=data).status_code == 401
        assert api.post(path, headers={**headers, 'Origin': 'https://outside.invalid'}, json=data).status_code == 403
        assert api.post(path, headers=headers, json={**data, 'dedicated_private_key': KEY.hex()}).status_code == 422
        assert api.post(path, headers=headers, json=view_terms(api, headers, sub='654321')).status_code == 409

        connection = api.post(path, headers=headers, json=data)
        assert connection.status_code == 200, connection.text
        assert connection.headers['Cache-Control'] == 'no-store'
        assert data['api_key'] not in connection.text
        state = connection.json()
        assert state['connected'] is True and state['can_sign'] is False and state['authorization'] is False
        assert state['permission_revision'] != data['permission_revision']
        scope = {key: state['permission_revision'] if key == 'permission_revision' else data[key]
                 for key in ('sub_account_id', 'permission_revision')}

        group = ROOT + '/groups/' + case.parent
        assert api.post(ROOT + '/preview', headers=headers, json={**terms(), **scope}).status_code == 409
        assert api.post(group + '/recover', headers=headers, json={**scope,
            'confirmation': 'RECUPERAR_POSICION_GRVT_MAINNET'}).status_code == 409
        assert api.post(group + '/renew-preview', headers=headers, json=scope).status_code == 409
        first = api.post(group + '/live', headers=headers, json=scope)
        assert first.status_code == 200 and first.json()['group']['result'] is None
        case.clock[0] += 6_000_000_000
        second = api.post(group + '/live', headers=headers, json=scope)
        assert second.status_code == 200, second.text
        assert second.json()['group']['result']['net_realized_pnl_usdt'] == '4.95'
        assert len(case.sent) == 1 and not case.settings['cancel_posts'] and not case.settings['reduction_posts']

        assert api.post(ROOT + '/disconnect', headers=headers, json={**scope,
            'confirmation': 'DESCONECTAR_GRVT_MAINNET'}).status_code == 200
        assert api.post(group + '/live', headers=headers, json=scope).status_code == 409
        assert api.get(group, headers=headers).json()['group']['result']['net_realized_pnl_usdt'] == '4.95'


def test_view_rejects_mismatched_exchange_identity_and_preserves_a_trade_session(tmp_path):
    from tests.test_grvt_mainnet_recovery import partial_case

    with api_case(tmp_path, partial_case) as (case, api, headers):
        factory = case.factory

        def different_account():
            client = factory()
            client._account = '0x' + '3' * 40
            return client

        case.factory = different_account
        data = view_terms(api, headers)
        assert api.post(ROOT + '/connect-view', headers=headers, json=data).status_code == 409
        case.factory = factory
        assert api.post(ROOT + '/connect-view', headers=headers, json=data).status_code == 200

        signed = connect(api, headers)
        assert signed.status_code == 200 and signed.json()['can_sign'] is True
        assert api.post(ROOT + '/connect-view', headers=headers, json=view_terms(api, headers)).status_code == 409
        same = api.get(ROOT, headers=headers, params={'sub_account_id': '123456'}).json()
        assert same['can_sign'] is True and same['permission_revision'] == signed.json()['permission_revision']


@pytest.mark.parametrize('direction,fill_size,remaining',[
    ('LONG',None,'0'), ('SHORT',None,'0'), ('LONG','0.1','0.2'),
])
def test_view_recovers_lost_ioc_from_archived_id_without_sending(tmp_path,direction,fill_size,remaining):
    from tests.test_grvt_mainnet_recovery import prepared_case,send_case

    def interrupted(directory):
        case=prepared_case(directory,direction=direction)
        case.settings.update(reduction_lost=True,reduction_fill_size=fill_size)
        attempt=send_case(case)['reductions'][0]
        assert attempt['acknowledgement'] is None and attempt['state'] is None
        case.clock[0]+=121_000_000_000  # The original authority has expired.
        return case

    with api_case(tmp_path,interrupted) as (case,api,headers):
        path=ROOT+'/groups/'+case.parent
        order=case.settings['reduction_posts'][0]['orders'][0]['metadata']['client_order_id']
        original=api.get(path,headers=headers).json()['group']
        assert original['reductions'][0]['state'] is None and original['recovery']['remaining_size_from_orders'] is None
        connection=api.post(ROOT+'/connect-view',headers=headers,json=view_terms(api,headers))
        assert connection.status_code==200 and connection.json()['can_sign'] is False
        scope={'sub_account_id':'123456','permission_revision':connection.json()['permission_revision']}
        lookup=case.settings['orders'][order]
        terminal=lookup['state'].copy()
        if fill_size is None:
            lookup['state'].update(status='OPEN',traded_size=['0'])
            pending=api.post(path+'/live',headers=headers,json=scope)
            assert pending.status_code==200,pending.text
            group=pending.json()['group']
            assert group['reductions'][0]['state']['status']=='OPEN'
            assert group['recovery']['remaining_size_from_orders'] is None and group['financial_audit_count']==0
            lookup['state'].update(terminal,update_time=str(case.clock[0]))
        first=api.post(path+'/live',headers=headers,json=scope)
        assert first.status_code==200,first.text
        group=first.json()['group']
        assert group['reductions'][0]['client_order_id']==order
        assert group['reductions'][0]['state']['traded_size']==(fill_size or '0.3')
        assert group['recovery']['remaining_size_from_orders']==remaining and group['result'] is None
        assert group['financial_audit_count']==int(remaining=='0')

        case.clock[0]+=6_000_000_000
        second=api.post(path+'/live',headers=headers,json=scope)
        assert second.status_code==200,second.text
        group=second.json()['group']
        if remaining=='0':
            assert group['result']['net_realized_pnl_usdt']=='1.96' and group['result']['positions']==[]
            assert group['financial_audit_count']==2
        else:
            assert group['result'] is None and group['financial_audit_count']==0
            assert group['recovery']['remaining_size_from_orders']=='0.2'
        assert api.post(path+'/recover',headers=headers,json={**scope,
            'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}).status_code==409
        assert len(case.sent)==1 and not case.settings['cancel_posts'] and len(case.settings['reduction_posts'])==1
        api.post(ROOT+'/disconnect',headers=headers,json={**scope,'confirmation':'DESCONECTAR_GRVT_MAINNET'})
        assert api.get(path,headers=headers).json()['group']==group


def test_view_does_not_settle_a_full_ioc_when_the_exchange_balance_disagrees(tmp_path):
    from tests.test_grvt_mainnet_recovery import prepared_case,send_case

    def inconsistent(directory):
        case=prepared_case(directory)
        case.settings['reduction_lost']=True
        send_case(case)
        case.settings.update(account_balance='10001.95',account_equity='10001.95')
        return case

    with api_case(tmp_path,inconsistent) as (case,api,headers):
        path=ROOT+'/groups/'+case.parent
        connection=api.post(ROOT+'/connect-view',headers=headers,json=view_terms(api,headers))
        assert connection.status_code==200,connection.text
        scope={'sub_account_id':'123456','permission_revision':connection.json()['permission_revision']}
        assert api.post(path+'/live',headers=headers,json=scope).status_code==409
        group=api.get(path,headers=headers).json()['group']
        assert group['recovery']['remaining_size_from_orders']=='0'
        assert group['result'] is None and group['financial_audit_count']==0
        assert len(case.settings['reduction_posts'])==1 and not case.settings['cancel_posts']


def _interrupted_history_case(directory):
    from tests.test_grvt_mainnet_recovery import prepared_case, send_case
    from tests.test_grvt_perp_limit_execution import enable_order_history

    case = prepared_case(directory)
    case.settings['reduction_lost'] = True
    attempt = send_case(case)['reductions'][0]
    assert attempt['state'] is None and attempt['acknowledgement'] is None
    case.clock[0] += 121_000_000_000
    enable_order_history(case, *case.settings['orders'])
    return case


def test_view_recovers_original_group_and_ioc_from_bounded_order_history(tmp_path):
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket

    with api_case(tmp_path, _interrupted_history_case) as (case, api, headers):
        saved = read_mainnet_bracket(case.vault, case.parent)
        opening = saved['claim']['issued_at_ns']
        ioc = saved['recovery']['reductions'][0]['claim']
        case.settings['history_requests'].clear()
        path = ROOT + '/groups/' + case.parent
        connection = api.post(ROOT + '/connect-view', headers=headers, json=view_terms(api, headers))
        assert connection.status_code == 200 and not connection.json()['can_sign']
        scope = {'sub_account_id':'123456','permission_revision':connection.json()['permission_revision']}
        first = api.post(path + '/live', headers=headers, json=scope)
        assert first.status_code == 200, first.text
        assert first.json()['group']['financial_audit_count'] == 1
        case.clock[0] += 6_000_000_000
        second = api.post(path + '/live', headers=headers, json=scope)
        assert second.status_code == 200, second.text
        group = second.json()['group']
        assert group['result']['net_realized_pnl_usdt'] == '1.96'
        assert group['reductions'][0]['client_order_id'] == ioc['client_order_id']
        requests = [body for name, body in case.settings['history_requests'] if name == 'order_history']
        assert len(requests) == 14
        windows = {(int(body['start_time']), int(body['end_time'])) for body in requests}
        assert {start for start, _ in windows} == {opening-1_000_000_001, ioc['issued_at_ns']-1_000_000_001}
        assert all(end == ioc['expiration_ns'] for start, end in windows if start == ioc['issued_at_ns']-1_000_000_001)
        assert all((row['kind'],row['base'],row['quote'],row['limit']) == (['PERPETUAL'],['BTC'],['USDT'],500)
                   for row in requests)
        assert len(case.sent) == len(case.settings['reduction_posts']) == 1 and not case.settings['cancel_posts']


@pytest.mark.parametrize('failure', ['duplicate', 'stale-active', 'altered-signature', 'altered-trigger', 'outside-window', 'unauthorized'])
def test_unproven_ioc_in_order_history_keeps_grvt_recovery_pending(tmp_path, failure):
    def uncertain(directory):
        case = _interrupted_history_case(directory)
        cid = case.settings['reduction_posts'][0]['orders'][0]['metadata']['client_order_id']
        lookup = case.settings['order_lookup_errors']
        for other in set(lookup) - {cid}:
            del lookup[other]
        history = case.settings['order_history']

        def damaged(body):
            page = history(body)
            for row in page['result']:
                if row['metadata']['client_order_id'] != cid:
                    continue
                if failure == 'duplicate':
                    page['result'] = [*page['result'], {**row, 'order_id':'0xother'}]
                elif failure == 'stale-active':
                    row['state']['status'] = 'OPEN'
                elif failure == 'altered-signature':
                    row['signature']['nonce'] ^= 1
                elif failure == 'altered-trigger':
                    row['metadata']['trigger'] = {'trigger_type':'STOP_LOSS'}
                elif failure == 'outside-window':
                    row['metadata']['create_time'] = str(int(body['start_time']))
            return page

        case.settings['order_history'] = damaged
        if failure == 'unauthorized':
            lookup[cid] = (401,1008)
        return case

    with api_case(tmp_path, uncertain) as (case, api, headers):
        path = ROOT + '/groups/' + case.parent
        connection = api.post(ROOT + '/connect-view', headers=headers, json=view_terms(api, headers))
        assert connection.status_code == 200
        scope = {'sub_account_id':'123456','permission_revision':connection.json()['permission_revision']}
        assert api.post(path + '/live', headers=headers, json=scope).status_code == 409
        group = api.get(path, headers=headers).json()['group']
        assert group['result'] is None and group['financial_audit_count'] == 0
        assert group['reductions'][0]['state'] is None and group['recovery']['remaining_size_from_orders'] is None
        requests = [body for name, body in case.settings['history_requests'] if name == 'order_history']
        assert len(requests) == (0 if failure == 'unauthorized' else 2)
        assert len(case.settings['reduction_posts']) == 1 and not case.settings['cancel_posts']


def test_view_connection_cannot_reappear_after_vault_lock_during_account_read(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.test_grvt_mainnet_recovery import partial_case

    with api_case(tmp_path, partial_case) as (case, api, headers):
        reading, resume = Event(), Event()

        def hold_summary(url, body):
            if url.endswith('/account_summary'):
                reading.set()
                assert resume.wait(10)

        case.settings['private_hook'] = hold_summary
        data = view_terms(api, headers)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(api.post, ROOT + '/connect-view', headers=headers, json=data)
            try:
                assert reading.wait(10)
                assert api.post('/real/vault/lock', headers=headers).status_code == 200
            finally:
                resume.set()
            assert pending.result(timeout=10).status_code == 409
        token = api.post('/real/vault/unlock', json={'passphrase': PASSWORD}).json()['token']
        state = api.get(ROOT, headers={'X-Orbis-Vault-Token': token},
                        params={'sub_account_id': '123456'}).json()
        assert state['connected'] is False and state['can_sign'] is False


def test_native_exit_live_poll_conciles_without_cancelling_or_reducing(tmp_path):
    from tests.test_grvt_mainnet_finance import closed_case
    with api_case(tmp_path,closed_case) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        body={'sub_account_id':'123456','permission_revision':revision}
        path=ROOT+'/groups/'+case.parent+'/live'
        first=api.post(path,headers={**headers,'X-Orbis-Passive':'1'},json=body)
        assert first.status_code==200,first.text
        assert first.json()['group']['result'] is None
        case.clock[0]+=6_000_000_000
        second=api.post(path,headers=headers,json=body)
        assert second.status_code==200,second.text
        assert second.json()['group']['result']['net_realized_pnl_usdt']=='4.95'
        assert not case.settings['cancel_posts'] and not case.settings['reduction_posts']
        assert api.post(path,headers=headers,json={**body,'sub_account_id':'654321'}).status_code==409


def test_stop_revokes_an_active_recovery_before_its_first_ioc_and_rearm_requires_receipt(tmp_path):
    import threading
    from tests.test_grvt_mainnet_recovery import partial_case
    reached=threading.Event()
    def paused(case):
        def wait(stop,seconds):
            reached.set();return stop.wait(5)
        return wait
    with api_case(tmp_path,partial_case,paused) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        body={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':3,
            'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}
        path=ROOT+'/groups/'+case.parent+'/recover'
        assert api.post(path,headers=headers,json=body).status_code==200
        assert reached.wait(5)
        assert api.post(path,headers=headers,json=body).status_code==409
        result=api.post(ROOT+'/stop',headers=headers,json={'sub_account_id':'123456','confirmation':'DETENER_GRVT_MAINNET'})
        assert result.status_code==200,result.text
        group=wait_job(api,headers,case.parent)
        assert group['job']['status']=='STOPPED' and not case.settings['reduction_posts']
        history=api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).json()
        assert history['stop']['stopped'] is True and history['can_sign'] is False
        # Reconectar firma no borra STOP ni habilita otra entrada pendiente.
        revision=connect(api,headers).json()['permission_revision']
        assert api.post(ROOT+'/rearm',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,
            'stop_revision':history['stop']['revision'],'confirmation':'REARMAR_GRVT_MAINNET'}).status_code==409
        assert api.post(ROOT+'/preview',headers=headers,json={**terms(),'permission_revision':revision}).status_code==409


def test_lock_revokes_recovery_and_reopen_restores_history_without_authority(tmp_path):
    import threading
    from tests.test_grvt_mainnet_recovery import partial_case
    reached=threading.Event()
    def paused(case):
        def wait(stop,seconds):reached.set();return stop.wait(5)
        return wait
    with api_case(tmp_path,partial_case,paused) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        assert api.post(ROOT+'/groups/'+case.parent+'/recover',headers=headers,json={'sub_account_id':'123456',
            'permission_revision':revision,'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}).status_code==200
        assert reached.wait(5)
        assert api.post('/real/vault/lock',headers=headers).status_code==200
        assert api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).status_code==401
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        reopened={'X-Orbis-Vault-Token':token};group=wait_job(api,reopened,case.parent)
        assert not group['job']['active'] and not case.settings['reduction_posts']
        result=api.get(ROOT,headers=reopened,params={'sub_account_id':'123456'}).json()
        assert not result['connected'] and not result['can_sign'] and result['groups'][0]['recovery']['requested']
        assert api.post(ROOT+'/groups/'+case.parent+'/recover',headers=reopened,json={'sub_account_id':'123456',
            'permission_revision':result['permission_revision'],'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}).status_code==409


def test_rearm_is_explicit_revision_bound_and_never_sends_an_order(tmp_path):
    from tests.test_grvt_mainnet_finance import closed_case
    with api_case(tmp_path,closed_case) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision']
        for _ in range(2):
            result=api.post(ROOT+'/groups/'+case.parent+'/live',headers=headers,json={'sub_account_id':'123456','permission_revision':revision})
            assert result.status_code==200,result.text
            case.clock[0]+=6_000_000_000
        assert result.json()['group']['result'] is not None
        assert api.post(ROOT+'/stop',headers=headers,json={'sub_account_id':'123456','confirmation':'DETENER_GRVT_MAINNET'}).status_code==200
        revision=connect(api,headers).json()['permission_revision']
        history=api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).json()
        body={'sub_account_id':'123456','permission_revision':revision,'stop_revision':history['stop']['revision'],
            'confirmation':'REARMAR_GRVT_MAINNET'}
        assert api.post(ROOT+'/rearm',headers=headers,json={**body,'stop_revision':0}).status_code==409
        result=api.post(ROOT+'/rearm',headers=headers,json=body)
        assert result.status_code==200,result.text
        assert result.json()['permission_revision']!=revision and result.json()['can_sign'] is True
        assert api.post(ROOT+'/rearm',headers=headers,json=body).status_code==409
        assert len(case.sent)==1
