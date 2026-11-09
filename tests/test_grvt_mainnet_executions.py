"""Marcas REAL derivadas sólo de fills propios verificados."""

from hashlib import sha256
import json

from app.lab.store import content_hash
from app.portfolio.vault import SCHEMA_VERSION
from tests.test_grvt_mainnet_api import api_case,connect,ROOT,advancing_waiter,wait_job
from tests.test_grvt_mainnet_recovery import partial_case


def test_closed_group_exposes_exact_entry_partial_and_reduction_fills_without_rpc(tmp_path):
    with api_case(tmp_path,partial_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        assert api.post(path+'/recover',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,
            'ttl_seconds':120,'max_attempts':3,'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'}).status_code==200
        assert wait_job(api,headers,case.parent)['job']['status']=='COMPLETE'
        before=list(case.calls)
        response=api.get(path+'/executions-page',headers=headers)
        assert response.status_code==200,response.text
        data=response.json();assert data['environment']=='REAL' and data['network']=='MAINNET' and data['provider']=='grvt'
        assert data['financially_reconciled'] is True and data['net_realized_pnl_usdt']=='1.96'
        fills=data['executions'];assert len(fills)==data['total']==3
        assert {(fill['role'],fill['side'],fill['quantity_base']) for fill in fills}=={('ENTRY','BUY','0.5'),('TP','SELL','0.2'),('REDUCTION','SELL','0.3')}
        assert all(type(fill['time_ns']) is str and type(fill['price']) is str for fill in fills)
        export=path+'/research-export'
        body={'sub_account_id':'123456','revision':data['revision'],'confirmation':'EXPORTAR_JSONL_GRVT_REAL'}
        assert api.post(export,json=body).status_code==401
        assert api.post(export,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        assert api.post(export,headers=headers,json={**body,'sub_account_id':'123457'}).status_code==409
        assert api.post(export,headers=headers,json={**body,'revision':'0'*64}).status_code==409
        assert api.post(export,headers=headers,json={**body,'confirmation':'EXPORTAR_JSONL_TESTNET'}).status_code==422
        response=api.post(export,headers=headers,json=body)
        assert response.status_code==200,response.text
        assert response.headers['cache-control']=='no-store'
        payload=response.json()
        assert payload['sha256']==sha256(payload['jsonl'].encode()).hexdigest()
        records=[json.loads(line) for line in payload['jsonl'].splitlines()]
        assert len(records)==payload['record_count']==3 and json.dumps('123456') not in payload['jsonl']
        assert case.parent not in payload['jsonl']
        for record,fill in zip(records,fills):
            assert record['provenance']=={'kind':'MANUAL'} and record['network']=='MAINNET'
            assert record['entry_kind']=='PROTECTED' and record['execution_authority']=='NONE'
            assert all(record['fill'][key]==fill[key] for key in record['fill'])
            assert not any(json.dumps(fill[key]) in payload['jsonl'] for key in ('fill_id','order_id','client_order_id'))
            assert content_hash({key:value for key,value in record.items() if key!='row_sha256'})==record['row_sha256']
        assert case.calls==before
        assert api.get(path+'/executions-page').status_code==401


def test_active_entry_is_archived_at_actual_fill_price_and_readable_after_stop(tmp_path):
    from tests.test_grvt_mainnet_bot_api import api_case as bot_api_case,preview
    from tests.test_grvt_mainnet_bot_service import wait_for
    with bot_api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source);run=body['review']['run_id']
        assert api.post(ROOT+'/bots/start',headers=headers,json=body).status_code==202;emit()
        def watching():
            row=api.get(ROOT+'/bots/'+run,headers=headers).json().get('item')
            return row if row and row['status']=='WATCHING' else None
        item=wait_for(watching);parent=item['parent_client_order_id']
        revision=api.get(ROOT,headers=headers,params={'sub_account_id':'123456'}).json()['permission_revision']
        assert api.post(ROOT+'/bots/stop',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,'confirmation':'DETENER_BOT_GRVT_MAINNET'}).status_code==200
        wait_for(lambda:not api.get(ROOT+'/bots/'+run,headers=headers).json()['item']['active'])
        before=list(case.calls)
        result=api.get(ROOT+'/groups/'+parent+'/executions-page',headers=headers)
        assert result.status_code==200,result.text
        data=result.json();assert data['total']==1 and data['executions'][0]['price']=='100'
        assert data['financially_reconciled'] is False and data['net_realized_pnl_usdt'] is None
        export=api.post(ROOT+'/groups/'+parent+'/research-export',headers=headers,
            json={'sub_account_id':'123456','revision':data['revision'],'confirmation':'EXPORTAR_JSONL_GRVT_REAL'})
        assert export.status_code==409 and 'conciliada' in export.json()['detail']
        assert data['evidence']=='OWN_VERIFIED_FILLS' and case.calls==before
        # Ya no depende de la clave de firma, de la fuente PAPER o de una conexión privada.
        api.post('/real/vault/lock',headers=headers)
        from tests.test_grvt_mainnet_submit import PASSWORD
        token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        recovered=api.get(ROOT+'/groups/'+parent+'/executions-page',headers={'X-Orbis-Vault-Token':token}).json()
        assert recovered==data


def test_pagination_is_bound_to_one_verified_fill_revision(tmp_path):
    with api_case(tmp_path,partial_case,advancing_waiter) as (case,api,headers):
        revision=connect(api,headers).json()['permission_revision'];path=ROOT+'/groups/'+case.parent
        api.post(path+'/recover',headers=headers,json={'sub_account_id':'123456','permission_revision':revision,'ttl_seconds':120,'max_attempts':3,'confirmation':'RECUPERAR_POSICION_GRVT_MAINNET'})
        assert wait_job(api,headers,case.parent)['result']
        first=api.get(path+'/executions-page?limit=1',headers=headers).json()
        second=api.get(path+'/executions-page',headers=headers,params={'limit':1,'offset':1,'revision':first['revision']}).json()
        assert first['next_offset']==1 and second['next_offset']==2 and first['executions'][0]['fill_id']!=second['executions'][0]['fill_id']
        assert api.get(path+'/executions-page',headers=headers,params={'revision':'0'*64}).status_code==409
        assert api.get(path+'/executions-page?offset=4',headers=headers).status_code==409


def test_entry_archive_rejects_recomputed_hash_over_changed_quantity(tmp_path):
    import pytest,json
    from tests.test_grvt_mainnet_bot_exit import exit_case
    from app.live_execution.grvt_mainnet_bot_exit import review_mainnet_bot_exit
    from app.live_execution.grvt_mainnet_entry_fills import save_entry_fills
    from app.live_execution.grvt_mainnet_journal import encode
    from app.portfolio.models import GrvtMainnetEntryFills
    from app.portfolio.vault import VaultError
    case,origin,grant,parent,public,settings=exit_case(tmp_path)
    proof=review_mainnet_bot_exit(case.vault,grant['id'],parent,client_factory=case.factory,public_post=public,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    save_entry_fills(case.vault,proof,require_permission=lambda:None)
    with case.vault.session() as db:
        row=db.get(GrvtMainnetEntryFills,parent);value=json.loads(row.payload_json);value['notional']='5000'
        row.payload_json,row.payload_digest=encode(value);db.commit()
    with pytest.raises(VaultError):case.vault.backup(tmp_path/'damaged-entry.db')
    case.vault.lock()


def test_v55_migration_creates_no_execution_from_a_signed_order(tmp_path):
    from tests.test_grvt_mainnet_recovery_renewal import expired_case
    from tests.test_grvt_mainnet_submit import PASSWORD
    from app.live_execution.grvt_mainnet_executions import read_executions
    case=expired_case(tmp_path)
    with case.vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_mainnet_entry_fills');db.connection().exec_driver_sql('PRAGMA user_version=55');db.commit()
    case.vault.lock();case.vault.unlock(PASSWORD)
    with case.vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
        assert db.connection().exec_driver_sql('SELECT count(*) FROM grvt_mainnet_entry_fills').scalar_one()==0
        value=read_executions(db,case.parent)
        assert value['total']==0 and value['evidence']=='NO_VERIFIED_FILLS'
    case.vault.backup(tmp_path/'migrated-execution-view.db');case.vault.lock()
