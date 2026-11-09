"""Un cierre vencido sólo puede continuar con otra autorización explícita."""

from app.portfolio.vault import SCHEMA_VERSION
from tests.test_grvt_mainnet_api import api_case,connect,ROOT,advancing_waiter,wait_job
from tests.test_grvt_mainnet_recovery import partial_case


def expired_case(directory):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery
    case=partial_case(directory)
    case.original_recovery=begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=30,max_attempts=1,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    case.clock[0]+=31_000_000_000
    return case


def test_expired_close_needs_a_one_use_review_and_keeps_the_original_request(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    with api_case(tmp_path,expired_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        old={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':1,
            'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}
        assert api.post(path+'/recover',headers=headers,json=old).status_code==409
        preview=api.post(path+'/renew-preview',headers=headers,json={key:value for key,value in old.items() if key!='confirmation'})
        assert preview.status_code==200,preview.text
        body={**preview.json(),'permission_revision':revision,'confirmation':'RENOVAR_CIERRE_GRVT_MAINNET'}
        assert not case.settings['reduction_posts']
        result=api.post(path+'/renew',headers=headers,json=body)
        assert result.status_code==200,result.text
        assert api.post(path+'/renew',headers=headers,json=body).status_code==409
        group=wait_job(api,headers,case.parent)
        assert group['job']['status']=='COMPLETE' and group['result']['net_realized_pnl_usdt']=='1.96',group
        assert len(case.settings['reduction_posts'])==1
        view=read_mainnet_recovery(case.vault,case.parent)
        assert view['initial_request']==case.original_recovery['request']
        assert view['request']['review_id']==body['review']['review_id']
        case.vault.backup(tmp_path/'renewed-close.db')


def test_exhausted_partial_ioc_renews_only_remaining_size_and_preserves_both_attempts(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    with api_case(tmp_path,partial_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        case.settings['reduction_fill_size']='0.1'
        start={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':1}
        assert api.post(path+'/recover',headers=headers,json={**start,'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}).status_code==200
        assert wait_job(api,headers,case.parent)['job']['status']=='ATTEMPT_LIMIT'
        before=read_mainnet_recovery(case.vault,case.parent)
        assert len(before['reductions'])==1
        preview=api.post(path+'/renew-preview',headers=headers,json=start)
        assert preview.status_code==200,preview.text
        case.settings['reduction_fill_size']=None
        assert api.post(path+'/renew',headers=headers,json={**preview.json(),'permission_revision':revision,'confirmation':'RENOVAR_CIERRE_GRVT_MAINNET'}).status_code==200
        group=wait_job(api,headers,case.parent)
        assert group['job']['status']=='COMPLETE' and group['result']['net_realized_pnl_usdt']=='1.95',group
        assert [post['orders'][0]['legs'][0]['size'] for post in case.settings['reduction_posts']]==['0.3','0.2']
        after=read_mainnet_recovery(case.vault,case.parent)
        assert after['initial_request']==before['request'] and len(after['reductions'])==2
        assert after['request']['first_attempt']==2 and after['request']['max_attempts']==2
        assert after['reductions'][0]['claim']==before['reductions'][0]['claim']
        case.vault.backup(tmp_path/'two-grants-two-attempts.db')


def open_ioc_case(directory):
    from tests.test_grvt_mainnet_recovery import prepared_case,send_case
    case=prepared_case(directory,attempts=1);case.settings['reduction_fill_size']='0.1'
    view=send_case(case);cid=view['reductions'][0]['claim']['client_order_id']
    case.settings['orders'][cid]['state']['status']='OPEN'
    case.clock[0]+=121_000_000_000
    return case


def test_pending_earlier_ioc_blocks_a_new_grant_even_after_expiry(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    with api_case(tmp_path,open_ioc_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        result=api.post(path+'/renew-preview',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':1})
        assert result.status_code==409,result.text
        view=read_mainnet_recovery(case.vault,case.parent)
        assert not any(event['type']=='RECOVERY_GRANT' for event in view['events']) and len(case.settings['reduction_posts'])==1


def test_changed_renewal_terms_or_stop_never_start_a_new_attempt(tmp_path):
    with api_case(tmp_path,expired_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        prepared=api.post(path+'/renew-preview',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':1}).json()
        body={**prepared,'permission_revision':revision,'confirmation':'RENOVAR_CIERRE_GRVT_MAINNET'}
        changed={**body,'review':{**body['review'],'ttl_seconds':300}}
        assert api.post(path+'/renew',headers=headers,json=changed).status_code==409
        assert api.post(path+'/renew',headers={**headers,'Origin':'https://foreign.invalid'},json=body).status_code==403
        assert api.post(ROOT+'/stop',headers=headers,json={'sub_account_id':'123456','confirmation':'DETENER_GRVT_MAINNET'}).status_code==200
        assert api.post(path+'/renew',headers=headers,json=body).status_code==409
        assert not case.settings['reduction_posts']


def test_renewal_cannot_reuse_snapshots_from_the_previous_grant(tmp_path):
    import pytest
    from tests.test_grvt_mainnet_recovery import prepared_case,send_case
    from app.live_execution.grvt_mainnet_recovery import audit_recovery_position,read_mainnet_recovery
    from app.live_execution.grvt_mainnet_recovery_grants import renew_mainnet_recovery
    from app.live_execution.grvt_mainnet_journal import encode
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case=prepared_case(tmp_path,attempts=1);case.settings['reduction_fill_size']='0.1';send_case(case)
    for _ in range(2):
        case.clock[0]+=6_000_000_000
        audit_recovery_position(case.vault,case.parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    view=read_mainnet_recovery(case.vault,case.parent)
    renew_mainnet_recovery(case.vault,case.parent,expected_request_digest=encode(view['request'])[1],expected_event_count=len(view['events']),
        ttl_seconds=120,max_attempts=1,review_id='synthetic-new-review-id',require_permission=lambda:None,now_ns=case.clock[0])
    with pytest.raises(GrvtMainnetError,match='dos auditorías'):send_case(case)
    assert len(case.settings['reduction_posts'])==1;case.vault.backup(tmp_path/'new-grant-no-old-snapshots.db');case.vault.lock()


def test_backup_rejects_renewed_exit_cap_even_with_an_updated_event_hash(tmp_path):
    import json,pytest
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    from app.live_execution.grvt_mainnet_recovery_grants import renew_mainnet_recovery
    from app.live_execution.grvt_mainnet_journal import encode
    from app.portfolio.models import GrvtMainnetRecovery,GrvtMainnetRecoveryEvent
    from app.portfolio.vault import VaultError
    case=expired_case(tmp_path);view=read_mainnet_recovery(case.vault,case.parent)
    renew_mainnet_recovery(case.vault,case.parent,expected_request_digest=encode(view['request'])[1],expected_event_count=len(view['events']),
        ttl_seconds=120,max_attempts=1,review_id='synthetic-review-for-backup',require_permission=lambda:None,now_ns=case.clock[0])
    with case.vault.session() as db:
        header=db.get(GrvtMainnetRecovery,case.parent);event=db.get(GrvtMainnetRecoveryEvent,(case.parent,1))
        value=json.loads(event.payload_json);value['details']['exit_cap']='1000'
        event.payload_json,event.payload_digest=encode(value);header.head_digest=event.payload_digest;db.commit()
    with pytest.raises(VaultError):case.vault.backup(tmp_path/'forged-renewal.db')
    case.vault.lock()


def test_v54_migration_keeps_old_request_without_renewing_authority(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    from app.live_execution.grvt_mainnet_session import GrvtMainnetSession
    from tests.test_grvt_mainnet_submit import PASSWORD
    case=expired_case(tmp_path);before=read_mainnet_recovery(case.vault,case.parent)
    with case.vault.session() as db:db.connection().exec_driver_sql('PRAGMA user_version=54');db.commit()
    case.vault.lock();case.vault.unlock(PASSWORD)
    assert case.vault.last_migration_backup.is_file() and read_mainnet_recovery(case.vault,case.parent)==before
    assert not GrvtMainnetSession(case.vault).job(case.parent)['active']
    with case.vault.session() as db:assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    case.vault.backup(tmp_path/'v55-migrated-recovery.db');case.vault.lock()
