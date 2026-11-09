"""El trabajo manual vigente prevalece sobre una caché de bot ya detenido."""
from contextlib import contextmanager
from threading import Event
from unittest.mock import patch
from tests.test_grvt_mainnet_bot_api import api_case,preview
from tests.test_grvt_mainnet_bot_service import wait_for
from tests.test_grvt_mainnet_api import ROOT,connect
from app.portfolio.vault import RealVault


def test_pending_storage_reports_the_active_manual_recovery_in_the_cached_group(tmp_path):
    entered=Event();release=Event()
    @contextmanager
    def busy(*args,**kwargs):yield False
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source);run=body['review']['run_id'];api.post(ROOT+'/bots/start',headers=headers,json=body);emit()
        def watching():
            row=api.get(ROOT+'/bots/'+run,headers=headers).json().get('item')
            return row if row and row['status']=='WATCHING' else None
        item=wait_for(watching);parent=item['parent_client_order_id']
        revision=api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).json()['permission_revision']
        api.post(ROOT+'/bots/stop',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,'confirmation':'DETENER_BOT_GRVT_MAINNET'})
        wait_for(lambda:not api.get(ROOT+'/bots/'+run,headers=headers).json()['item']['active'])
        revision=connect(api,headers).json()['permission_revision']
        def hold_public_metadata(url,body):
            if url.endswith('/instrument'):entered.set();assert release.wait(10),'metadata barrier expired'
        case.settings['public_hook']=hold_public_metadata
        try:
            started=api.post(ROOT+'/groups/'+parent+'/recover',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,
                'ttl_seconds':120,'max_attempts':1,'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'})
            assert started.status_code==200,started.text
            assert entered.wait(10),'manual recovery never reached its public request'
            with patch.object(RealVault,'try_guard_session_generation',busy):
                response=api.get(ROOT+'/groups/'+parent,headers=headers);assert response.status_code==200,response.text
                value=response.json();assert value['pending'] and value['job']['active']
                assert value['group']['job']==value['job']
        finally:release.set();case.settings['public_hook']=None
        def closed():
            value=api.get(ROOT+'/groups/'+parent,headers=headers).json();return value.get('group',{}).get('result')
        assert wait_for(closed)['net_realized_pnl_usdt']=='-0.03'
        wait_for(lambda:api.get(ROOT+'/groups/'+parent,headers=headers).json().get('group',{}).get('job',{}).get('status')=='COMPLETE')
        with patch.object(RealVault,'try_guard_session_generation',busy):
            value=api.get(ROOT+'/groups/'+parent,headers=headers).json()
            assert value['group']['job']==value['job']=={'active':False,'status':'COMPLETE'}
        assert len(case.settings['reduction_posts'])==1
