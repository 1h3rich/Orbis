"""El límite de una salida sólo cambia mediante otra revisión expresa."""

from app.portfolio.vault import SCHEMA_VERSION
import pytest,httpx
from tests.test_grvt_mainnet_api import api_case,connect,ROOT,advancing_waiter,wait_job
from tests.test_grvt_mainnet_recovery import partial_case


def expensive_exit_case(directory,direction='LONG'):
    case=partial_case(directory,direction);original=case.public
    def public(url,body):
        result=original(url,body)
        if url.endswith('/ticker'):
            data=result.json();data['result'].update(best_bid_price='399.99',best_ask_price='400.01')
            return httpx.Response(200,json=data,request=httpx.Request('POST',url))
        return result
    case.public=public;case.settings['reduction_fill_price']='400';return case


@pytest.mark.parametrize('direction,net',[('LONG','91.96'),('SHORT','-88.04')])
def test_reviewed_exit_cap_can_close_the_same_residual_without_changing_original_limits(tmp_path,direction,net):
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    with api_case(tmp_path,lambda path:expensive_exit_case(path,direction),advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        terms={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':1}
        assert api.post(path+'/recover',headers=headers,json={**terms,'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}).status_code==200
        assert wait_job(api,headers,case.parent)['job']['status']=='REVIEW'
        assert not case.settings['reduction_posts']
        assert api.post(path+'/renew-preview',headers=headers,json=terms).status_code==409
        preview=api.post(path+'/renew-preview',headers=headers,json={**terms,'exit_cap':'150'})
        assert preview.status_code==200,preview.text
        assert preview.json()['review']['previous_exit_cap']=='100' and preview.json()['review']['exit_cap']=='150'
        body={**preview.json(),'permission_revision':revision,'confirmation':'RENOVAR_CIERRE_GRVT_MAINNET'}
        assert api.post(path+'/renew',headers=headers,json=body).status_code==200
        group=wait_job(api,headers,case.parent)
        assert group['job']['status']=='COMPLETE' and group['result']['net_realized_pnl_usdt']==net,group
        view=read_mainnet_recovery(case.vault,case.parent)
        assert view['initial_request']['exit_cap']=='100' and view['request']['exit_cap']=='150'
        assert view['request']['reason']=='EXIT_LIMIT_CHANGE'
        assert read_mainnet_bracket(case.vault,case.parent)['claim']['draft']['exit_cap']=='100'
        assert len(case.settings['reduction_posts'])==1 and case.settings['reduction_posts'][0]['orders'][0]['reduce_only'] is True
        case.vault.backup(tmp_path/'reviewed-exit-limit.db')


def test_changed_limit_review_and_stop_cannot_silently_raise_authority(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery,begin_mainnet_recovery
    def pending(directory):
        case=expensive_exit_case(directory);begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=1,
            confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0]);return case
    with api_case(tmp_path,pending,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        prepared=api.post(path+'/renew-preview',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,
            'ttl_seconds':120,'max_attempts':1,'exit_cap':'150'})
        assert prepared.status_code==200,prepared.text
        body={**prepared.json(),'permission_revision':revision,'confirmation':'RENOVAR_CIERRE_GRVT_MAINNET'}
        changed={**body,'review':{**body['review'],'exit_cap':'10000'}}
        assert api.post(path+'/renew',headers=headers,json=changed).status_code==409
        api.post(ROOT+'/stop',headers=headers,json={'sub_account_id':'123456','confirmation':'DETENER_GRVT_MAINNET'})
        assert api.post(path+'/renew',headers=headers,json=body).status_code==409
        assert read_mainnet_recovery(case.vault,case.parent)['request']['exit_cap']=='100' and not case.settings['reduction_posts']


@pytest.mark.parametrize('cap,code',[('10001',409),('0',409),(150,422)])
def test_exit_cap_limits_are_exact_and_never_broaden_the_old_request(tmp_path,cap,code):
    from tests.test_grvt_mainnet_recovery_renewal import expired_case
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    with api_case(tmp_path,expired_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        reply=api.post(path+'/renew-preview',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,
            'ttl_seconds':120,'max_attempts':1,'exit_cap':cap})
        assert reply.status_code==code,reply.text
        assert read_mainnet_recovery(case.vault,case.parent)['request']['exit_cap']=='100'
        assert not case.settings['reduction_posts']


def test_higher_cap_still_cannot_supersede_an_open_ioc(tmp_path):
    from tests.test_grvt_mainnet_recovery_renewal import open_ioc_case
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    with api_case(tmp_path,open_ioc_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        result=api.post(path+'/renew-preview',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,'exit_cap':'150'})
        assert result.status_code==409,result.text
        assert not any(event['type']=='RECOVERY_GRANT' for event in read_mainnet_recovery(case.vault,case.parent)['events'])
        assert len(case.settings['reduction_posts'])==1


def test_v56_migration_preserves_original_exit_cap_and_no_new_authority(tmp_path):
    from tests.test_grvt_mainnet_recovery_renewal import expired_case
    from tests.test_grvt_mainnet_submit import PASSWORD
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    from app.live_execution.grvt_mainnet_session import GrvtMainnetSession
    case=expired_case(tmp_path);before=read_mainnet_recovery(case.vault,case.parent)
    with case.vault.session() as db:db.connection().exec_driver_sql('PRAGMA user_version=56');db.commit()
    case.vault.lock();case.vault.unlock(PASSWORD)
    assert case.vault.last_migration_backup.is_file() and read_mainnet_recovery(case.vault,case.parent)==before
    assert not GrvtMainnetSession(case.vault).job(case.parent)['active']
    with case.vault.session() as db:assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    case.vault.backup(tmp_path/'original-limit-preserved.db');case.vault.lock()
