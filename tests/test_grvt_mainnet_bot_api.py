"""Activación REAL por revisión, autoridad acotada y privacidad de vista."""
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_grvt_mainnet_bot_service import service_case,wait_for
from tests.test_grvt_mainnet_api import ROOT,connect
from tests.test_grvt_mainnet_submit import PASSWORD
from app.portfolio.api import create_real_router

@contextmanager
def api_case(directory):
    case,origin,source,emit,settings,unused=service_case(directory,research=True);case.vault.lock()
    app=FastAPI();app.include_router(create_real_router(case.vault.path,lab_path=origin.lab.path,paper_perp_path=origin.paper.path,
        grvt_mainnet_factory=unused.factory,grvt_mainnet_public_post=unused.public,grvt_mainnet_clock_ns=unused.clock,
        grvt_mainnet_waiter=unused.waiter,grvt_mainnet_bot_poll_seconds=.02))
    with TestClient(app,base_url='http://127.0.0.1') as api:
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token'];headers={'X-Orbis-Vault-Token':token}
        yield case,origin,source,emit,settings,api,headers
        api.post('/real/operations/stop',json={'confirmation':'DETENER_Y_BLOQUEAR'})
    case.vault.lock()


def preview(api,headers,source,keep=True):
    revision=connect(api,headers).json()['permission_revision']
    result=api.post(ROOT+'/bots/preview',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,
        'session_id':source['session_id'],'research_validation_id':source['research_validation_id'],
        'entry_cap':'50','exit_cap':'100','total_entry_cap':'50','loss_cap':'10',
        'max_entries':1,'wait_seconds':120,'exit_seconds':120,'recovery_seconds':180,'keep_operating':keep})
    assert result.status_code==200,result.text
    return {**result.json(),'permission_revision':revision,'confirmation':'ACTIVAR_BOT_GRVT_MAINNET'}


def test_bot_review_is_one_use_and_known_id_recovers_start_while_view_is_protected(tmp_path):
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        assert api.get(ROOT+'/bots',params={'sub_account_id':'123456'}).status_code==401
        body=preview(api,headers,source);run=body['review']['run_id']
        assert not case.sent
        result=api.post(ROOT+'/bots/start',headers=headers,json=body)
        assert result.status_code==202,result.text
        assert result.json()['id']==run and result.json()['active']
        assert result.json()['schema']=='orbis.grvt.mainnet.bot.grant.v3'
        assert result.json()['research_binding']==body['review']['research_binding']
        assert api.post(ROOT+'/bots/start',headers=headers,json=body).status_code==409
        protected=api.post('/real/vault/protect-view',headers=headers)
        assert protected.status_code==200 and protected.json()['operating'] is True
        status=api.get('/real/vault/status').json();assert status['view_protected'] and status['operating']['active']
        assert api.get(ROOT+'/bots/'+run,headers=headers).status_code==401
        emit();wait_for(lambda:len(case.sent)==1)
        # La reautenticación recupera la vista; no reinicia ni amplía el trabajo.
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token'];headers={'X-Orbis-Vault-Token':token}
        settings['mark']='96.9'
        def completed():
            result=api.get(ROOT+'/bots/'+run,headers=headers).json()
            item=result.get('item')
            return item if item and not item['active'] else None
        done=wait_for(completed)
        assert done['status']=='ENTRY_LIMIT',done
        assert done['selections'][0]['settlement']['net_realized_pnl_usdt']=='-0.03'
        assert len(case.sent)==1 and len(case.settings['reduction_posts'])==1


def test_session_only_bot_stops_on_protect_view_without_sending(tmp_path):
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source,keep=False);run=body['review']['run_id']
        assert api.post(ROOT+'/bots/start',headers=headers,json=body).status_code==202
        assert api.post('/real/vault/protect-view',headers=headers).json()['operating'] is False
        emit();wait_for(lambda:not api.get('/real/vault/status').json()['unlocked'])
        assert not case.sent


def test_global_stop_revokes_protected_bot_and_explicit_retirement_after_unlock_releases_slot(tmp_path):
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source);run=body['review']['run_id']
        assert api.post(ROOT+'/bots/start',headers=headers,json=body).status_code==202
        assert api.post('/real/vault/protect-view',headers=headers).json()['operating'] is True
        result=api.post('/real/operations/stop',json={'confirmation':'DETENER_Y_BLOQUEAR'})
        assert result.status_code==200,result.text
        assert not api.get('/real/vault/status').json()['unlocked']
        emit();assert not case.sent
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token'];headers={'X-Orbis-Vault-Token':token}
        revision=api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).json()['permission_revision']
        result=api.post(ROOT+'/bots/stop',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,'confirmation':'DETENER_BOT_GRVT_MAINNET'})
        assert result.status_code==200,result.text
        def retired():
            row=api.get(ROOT+'/bots/'+run,headers=headers).json().get('item')
            return row if row and row['terminal'] else None
        assert wait_for(retired)['terminal']['status']=='STOPPED'
        assert not case.sent


def test_changed_review_terms_and_foreign_origin_do_not_activate_a_bot(tmp_path):
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source)
        changed={**body,'review':{**body['review'],'policy':{**body['review']['policy'],'entry_cap':'500'}}}
        assert api.post(ROOT+'/bots/start',headers=headers,json=changed).status_code==409
        assert api.post(ROOT+'/bots/start',headers={**headers,'Origin':'https://foreign.invalid'},json=body).status_code==403
        assert not case.sent
        history=api.get(ROOT+'/bots',headers=headers,params={'sub_account_id':'123456'}).json()
        assert not history['items']


def test_authenticated_group_can_use_verified_ram_snapshot_when_storage_is_busy(tmp_path):
    from contextlib import contextmanager
    from unittest.mock import patch
    from app.portfolio.vault import RealVault
    @contextmanager
    def busy(*args,**kwargs):yield False
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source);run=body['review']['run_id']
        assert api.post(ROOT+'/bots/start',headers=headers,json=body).status_code==202
        emit()
        def watching():
            result=api.get(ROOT+'/bots/'+run,headers=headers).json().get('item')
            return result if result and result['status']=='WATCHING' else None
        item=wait_for(watching);parent=item['parent_client_order_id']
        with patch.object(RealVault,'try_guard_session_generation',busy):
            result=api.get(ROOT+'/groups/'+parent,headers=headers)
            assert result.status_code==200,result.text
            value=result.json();assert value['pending'] is True and value['group']['parent_client_order_id']==parent
            assert value['group']['job']=={'active':True,'status':'WATCHING'}
            assert api.get(ROOT+'/groups/'+parent).status_code==401
            assert api.get(ROOT+'/groups/'+str(int(parent)^8),headers=headers).json()['group'] is None
        assert len(case.sent)==1 and not case.settings['reduction_posts']
