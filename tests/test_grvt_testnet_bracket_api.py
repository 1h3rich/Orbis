"""HTTP local autenticado → revisión → coordinador nativo; sólo venue ficticio."""

from contextlib import contextmanager
from copy import deepcopy
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import create_real_router
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from tests.test_grvt_testnet_bracket_submit import live_group,PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER

ROOT='/real/exchanges/grvt/testnet/brackets'


@contextmanager
def bracket_session(directory,direction='LONG',*,clock_ns=time.time_ns):
    venue,source,vault,_,cap,_,transport,public,recorded=live_group(directory,direction)
    def factory(key): return GrvtTestnetOrderClient(key,http_post=transport._post)
    app=FastAPI()
    app.include_router(create_real_router(directory/'real.db',lab_path=venue.lab.path,paper_perp_path=venue.paper.path,
        grvt_testnet_factory=factory,grvt_testnet_bracket_public_post=public,grvt_testnet_bracket_clock_ns=clock_ns))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        unlocked=client.post('/real/vault/unlock',json={'passphrase':PASSWORD})
        assert unlocked.status_code==200,unlocked.text
        headers={'X-Orbis-Vault-Token':unlocked.json()['token']}
        yield client,headers,source,recorded
    vault.lock()


def review_body(source):
    return {'api_key':'fake-testnet-key-'+source['sub_account_id'],'sub_account_id':source['sub_account_id'],
        'session_id':source['session_id'],'action_request_id':source['action_request_id'],
        'size':'0.5','limit_price':'100','entry_cap':'100','exit_cap':'100',
        'stop_loss':'95' if source['direction']=='LONG' else '105',
        'take_profit':'110' if source['direction']=='LONG' else '90','ttl_minutes':60,
        'max_spread_bps':25,'max_slippage_bps':25}


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_local_review_sends_one_native_group_and_recovers_it_by_its_three_ids(tmp_path,direction):
    with bracket_session(tmp_path,direction) as (client,headers,source,recorded):
        path=ROOT+'?sub_account_id='+source['sub_account_id']
        assert client.get(path).status_code==401
        assert client.post(ROOT+'/preview',json=review_body(source)).status_code==401
        prepared=client.post(ROOT+'/preview',headers=headers,json=review_body(source))
        assert prepared.status_code==200,prepared.text
        assert prepared.headers['cache-control']=='no-store' and recorded['sends']==[]
        data=prepared.json()
        assert data['can_submit'] is False and data['preview']['automatic_strategy'] is False
        payload={'preview':data['preview'],'preview_mac':data['preview_mac'],
            'api_key':review_body(source)['api_key'],'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER,
            'confirmation':'FIRMAR_ENTRADA_PROTEGIDA_GRVT_TESTNET'}
        sent=client.post(ROOT+'/send',headers=headers,json=payload)
        assert sent.status_code==200,sent.text
        assert sent.json()['group']['status']=='ACKNOWLEDGED' and len(recorded['sends'])==1
        assert client.post(ROOT+'/send',headers=headers,json=payload).status_code==409
        observed=client.post(ROOT+'/'+source['client_order_id']+'/observe',headers=headers,
            json={'api_key':payload['api_key']})
        assert observed.status_code==200,observed.text
        assert [row['status'] for row in observed.json()['group']['last_observation']['orders']]==['FILLED','OPEN','OPEN']
        history=client.get(path,headers=headers)
        assert history.status_code==200 and len(history.json()['groups'])==1
        assert isinstance(history.json()['groups'][0]['created_at_ns'],str)
        for secret in (KEY.hex(),SIGNER,payload['api_key']):
            assert secret not in sent.text and secret not in history.text


def test_scoped_emergency_review_closes_the_residual_through_the_same_panel(tmp_path):
    from tests.test_grvt_testnet_bracket_emergency import emergency_case
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']
    app=FastAPI(); logins=[]
    def factory(key):
        logins.append(True)
        return GrvtTestnetOrderClient(key,http_post=case.reader._post,clock_ns=lambda:case.clock[0])
    app.include_router(create_real_router(tmp_path/'real.db',
        grvt_testnet_factory=factory,
        grvt_testnet_bracket_public_post=case.public,grvt_testnet_bracket_clock_ns=lambda:case.clock[0]))
    path=ROOT+'/'+parent
    key={'api_key':'fake-testnet-key-'+case.cap.sub_account_id}
    with TestClient(app,base_url='http://127.0.0.1') as client:
        token=client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers={'X-Orbis-Vault-Token':token}
        reviewed=client.post(path+'/emergency/preview',headers=headers,json=key)
        assert reviewed.status_code==200,reviewed.text
        payload=reviewed.json()
        assert payload['preview']['size']=='0.3' and payload['preview']['side']=='SELL'
        assert not case.controls['sent']
        body={**key,**payload,'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER,
            'confirmation':'FIRMAR_CIERRE_EMERGENCIA_GRVT_TESTNET'}
        body.pop('network'); body.pop('can_submit')
        # La fixture usa fills a 100: revisar ese límite antes de firmar.
        cid=payload['preview']['client_order_id']
        sent=client.post(path+'/emergency/send',headers=headers,json=body)
        assert sent.status_code==200,sent.text
        assert len(case.controls['sent'])==1
        assert client.post(path+'/emergency/send',headers=headers,json=body).status_code==409
        pending=client.get(ROOT,headers=headers,params={'sub_account_id':case.cap.sub_account_id}).json()
        assert pending['groups'][0]['can_emergency'] is False
        baseline=len(logins)
        assert client.post(path+'/emergency/preview',headers=headers,json=key).status_code==409
        assert len(logins)==baseline
        for i in range(2):
            case.clock[0]+=(6 if i else 1)*1_000_000_000
            audited=client.post(path+'/emergencies/'+cid+'/audit',headers=headers,json=key)
            assert audited.status_code==200,audited.text
        reconciled=client.post(path+'/emergencies/'+cid+'/reconcile',headers=headers,
            json={'confirmation':'CONCILIAR_CIERRE_EMERGENCIA_TESTNET'})
        assert reconciled.status_code==200,reconciled.text
        panel=client.get(ROOT,headers=headers,params={'sub_account_id':case.cap.sub_account_id}).json()
        assert panel['stopped'] is True
        assert panel['groups'][0]['emergency_attempts'][0]['reconciliation']['result']['positions']==[]
    case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('fault',['size','source','network','expiry','mac','session'])
def test_review_cannot_be_modified_or_reused_from_another_vault_session(tmp_path,fault):
    with bracket_session(tmp_path) as (client,headers,source,recorded):
        prepared=client.post(ROOT+'/preview',headers=headers,json=review_body(source)).json()
        body={'preview':deepcopy(prepared['preview']),'preview_mac':prepared['preview_mac'],
            'api_key':review_body(source)['api_key'],'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER,
            'confirmation':'FIRMAR_ENTRADA_PROTEGIDA_GRVT_TESTNET'}
        if fault=='size': body['preview']['legs'][0]['size']='0.6'
        if fault=='source': body['preview']['source']['action_request_id']='00000000-0000-0000-0000-000000000000'
        if fault=='network': body['preview']['network']='MAINNET'
        if fault=='expiry': body['preview']['expires_at_ms']+=1000
        if fault=='mac': body['preview_mac']='0'*64
        if fault=='session':
            client.post('/real/vault/lock',headers=headers)
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        calls=list(recorded['private'])+list(recorded['public'])
        response=client.post(ROOT+'/send',headers=headers,json=body)
        assert response.status_code==409,response.text
        assert recorded['sends']==[] and recorded['private']+recorded['public']==calls
        for secret in (KEY.hex(),body['api_key'],SIGNER): assert secret not in response.text


def test_expired_signal_is_rejected_before_contacting_grvt(tmp_path):
    with bracket_session(tmp_path,clock_ns=lambda:time.time_ns()+21_000_000_000) as (client,headers,source,recorded):
        calls=(list(recorded['public']),list(recorded['private']))
        response=client.post(ROOT+'/preview',headers=headers,json=review_body(source))
        assert response.status_code==409
        assert (recorded['public'],recorded['private'])==calls and not recorded['sends']


def test_every_protected_entry_action_requires_the_local_vault_session(tmp_path):
    with bracket_session(tmp_path) as (client,_,source,recorded):
        calls=(list(recorded['public']),list(recorded['private']))
        parent=source['client_order_id']; cid=str(2**63+701)
        routes=['/preview','/send']+['/'+parent+'/'+name for name in ('observe','audit','cancel','reconcile','abandon','emergency/preview','emergency/send','market-sample','market-comparison')]
        routes+=['/'+parent+'/emergencies/'+cid+'/'+name for name in ('audit','reconcile')]
        for route in routes:
            response=client.post(ROOT+route,json={'api_key':'private-test-value','dedicated_private_key':KEY.hex()})
            assert response.status_code==401,(route,response.text)
            assert KEY.hex() not in response.text and 'private-test-value' not in response.text
        assert (recorded['public'],recorded['private'])==calls


@pytest.mark.parametrize('change',[{'ttl_minutes':'private-test-value'},{'max_spread_bps':True},{'unexpected':'private-test-value'}])
def test_invalid_body_never_echoes_credentials_in_validation_errors(tmp_path,change):
    with bracket_session(tmp_path) as (client,headers,source,recorded):
        calls=(list(recorded['public']),list(recorded['private']))
        response=client.post(ROOT+'/preview',headers=headers,json={**review_body(source),**change})
        assert response.status_code==422
        assert 'private-test-value' not in response.text and 'fake-testnet-key' not in response.text
        assert (recorded['public'],recorded['private'])==calls

def test_market_sample_rejects_unposted_group_cross_origin_and_ambiguous_id_without_login(tmp_path):
    with bracket_session(tmp_path) as (client,headers,source,recorded):
        path=ROOT+'/'+source['client_order_id']+'/market-sample'
        key='private-view-testnet-key'
        calls=list(recorded['private'])
        assert client.post(path,headers=headers,json={'api_key':key}).status_code==409
        assert client.post(path,headers={**headers,'Origin':'https://external.invalid'},
            json={'api_key':key}).status_code==403
        invalid=client.post(path,headers=headers,json={'api_key':key,
            'ioc_client_id':str(2**63+701),'ioc_order_id':'0x123'})
        assert invalid.status_code==422 and key not in invalid.text
        assert recorded['private']==calls and not recorded['sends']


def test_market_samples_are_idempotent_scoped_private_and_backed_up(tmp_path):
    from uuid import uuid4

    from app.portfolio.models import GrvtTestnetMarketSample
    from app.portfolio.vault import RealVault, VaultError
    from tests.grvt_bracket_ui_fixture import BracketUiVenue

    venue=BracketUiVenue(tmp_path)
    source=venue.source
    path=ROOT+'/'+source['client_order_id']
    other=ROOT+'/'+str(2**63+9001)
    key=review_body(source)['api_key']
    identifier=str(uuid4())
    payload={'api_key':key,'archive_id':identifier}
    calls=[]
    def factory(api_key):
        calls.append(True)
        return venue.factory(api_key)
    app=FastAPI()
    app.include_router(create_real_router(tmp_path/'real.db',lab_path=venue.origin.lab.path,
        paper_perp_path=venue.origin.paper.path,grvt_testnet_factory=factory,
        grvt_testnet_bracket_public_post=venue.public,grvt_testnet_bracket_clock_ns=venue.clock_ns))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        token=client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers={'X-Orbis-Vault-Token':token}
        prepared=client.post(ROOT+'/preview',headers=headers,json=review_body(source)).json()
        sent=client.post(ROOT+'/send',headers=headers,json={'preview':prepared['preview'],'preview_mac':prepared['preview_mac'],
            'api_key':key,'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER,
            'confirmation':'FIRMAR_ENTRADA_PROTEGIDA_GRVT_TESTNET'})
        assert sent.is_success,sent.text
        read=path+'/market-samples'
        assert client.get(read).status_code==401
        assert client.get(read,headers={**headers,'Origin':'https://external.invalid'}).status_code==403
        assert client.get(read,headers=headers).json()['samples']==[]
        legacy=client.post(path+'/market-sample',headers=headers,json={'api_key':key})
        assert legacy.is_success and client.get(read,headers=headers).json()['samples']==[]
        before=client.post(path+'/market-sample',headers=headers,json=payload)
        assert before.is_success,before.text
        request_count=len(calls)
        repeated=client.post(path+'/market-sample',headers=headers,json=payload)
        assert repeated.is_success and repeated.json()==before.json() and len(calls)==request_count
        assert client.post(path+'/market-sample',headers=headers,json={**payload,'ioc_order_id':'0xother'}).status_code==409
        assert client.post(other+'/market-sample',headers=headers,json=payload).status_code==409
        assert client.get(other+'/market-samples/'+identifier,headers=headers).status_code==409
        assert len(calls)==request_count and venue.bulk_sends==1 and venue.close_sends==0
        result=client.get(read,headers=headers)
        assert result.headers['cache-control']=='no-store'
        assert result.json()=={'network':'TESTNET','authorization':False,'samples':[{'id':identifier,
            'observed_at_ns':before.json()['observed_at_ns'],'remaining_size':'0.5','has_ioc':False}]}
        assert all(secret not in before.text and secret not in result.text for secret in (key,KEY.hex(),SIGNER))
        assert client.post('/real/vault/lock',headers=headers).is_success
        assert client.get(read+'/'+identifier,headers=headers).status_code==401
        new=client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        recovered=client.get(read+'/'+identifier,headers={'X-Orbis-Vault-Token':new})
        assert recovered.is_success and recovered.json()==before.json() and len(calls)==request_count

    vault=RealVault(tmp_path/'real.db')
    vault.unlock(PASSWORD)
    backup=vault.backup(tmp_path/'market-archive.db')
    assert backup.read_bytes()[:16]!=b'SQLite format 3\x00'
    copy=RealVault(backup)
    copy.unlock(PASSWORD)
    try:
        with copy.session() as db:
            row=db.get(GrvtTestnetMarketSample,identifier)
            assert row is not None and key not in row.payload_json and SIGNER not in row.payload_json
            row.payload_json+=' '
            db.commit()
        with pytest.raises(VaultError,match='invalid GRVT Testnet market samples'):
            copy.backup(tmp_path/'corrupt-archive.db')
    finally:
        copy.lock()
        vault.lock()


def test_v81_migration_preserves_portfolio_and_creates_an_empty_market_archive(tmp_path):
    from app.portfolio.repository import create_account
    from app.portfolio.vault import RealVault

    vault=RealVault(tmp_path/'real.db')
    vault.unlock(PASSWORD,create=True)
    with vault.session() as db:
        account_id=create_account(db,'Prueba','USDT').id
    with vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_market_samples')
        db.connection().exec_driver_sql('PRAGMA user_version=81')
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert RealVault._verify_portfolio_file(vault.last_migration_backup,PASSWORD,expected_version=81)==(1,0)
    with vault.session() as db:
        connection=db.connection()
        assert connection.exec_driver_sql('PRAGMA user_version').scalar_one()==82
        assert connection.exec_driver_sql('SELECT count(*) FROM grvt_testnet_market_samples').scalar_one()==0
        assert connection.exec_driver_sql('SELECT id FROM real_accounts').scalar_one()==account_id
    vault.lock()
