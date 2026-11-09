"""Private native funding API reads public RPC but can never broadcast."""
from contextlib import contextmanager
import json
from uuid import uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from tests.test_dex_gas_funding import case,now
from tests.test_dex_deployments import PASSWORD
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
from tests.test_wallet_funding_api import prove


def test_api_funds_the_verified_operator_once_and_recovers_without_its_draft(tmp_path):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
        manager.revoke();vault.lock();reads=[]
        @contextmanager
        def factory():
            def read(method,params):
                assert method.startswith('eth_') and 'send' not in method.lower()
                reads.append(method);return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            base=f'/real/accounts/{account}/dex-gas-funding'
            assert client.get(base).status_code==401
            assert client.get(base,headers=headers).json()==[]
            body={'address':meta['owner'],'binding_id':binding['id'],'draft_id':str(uuid4()),'request_id':str(uuid4()),
                'amount_bnb':'0.001','gas':'21000','gas_price':'1000000000','max_gas_bnb':'0.00003'}
            assert client.post(base,headers=headers,json=body).status_code==409 and not reads
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            assert client.post(base,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
            restored=client.post(f'/real/accounts/{account}/dex-operator/restore',headers=headers,json={
                'address':meta['owner'],'request_id':body['draft_id'],'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup)})
            assert restored.status_code==200,restored.text
            reviewed=client.post(base,headers=headers,json=body)
            assert reviewed.status_code==200,reviewed.text
            assert reviewed.headers['cache-control']=='no-store'
            plan=reviewed.json();url=base+'/'+plan['id']
            granted=client.post(url+'/claim',headers=headers,json={'address':meta['owner'],'plan_digest':plan['plan_digest'],
                'confirmation':'FINANCIAR_GAS_OPERADORA_DEX'})
            assert granted.status_code==200 and granted.json()['claim_granted'],granted.text
            assert not client.post(url+'/claim',headers=headers,json={'address':meta['owner'],'plan_digest':plan['plan_digest'],
                'confirmation':'FINANCIAR_GAS_OPERADORA_DEX'}).json()['claim_granted']
            sent=rpc('eth_sendTransaction',[plan['transaction']])
            assert client.post('/real/vault/lock',headers=headers).status_code==200
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            observed=client.post(url+'/observe',headers=headers,json={'address':meta['owner'],'tx_hash':sent})
            assert observed.status_code==200 and observed.json()['status']=='FINALIZED',observed.text
            count=len(reads)
            assert client.post(url+'/observe',headers=headers,json={'address':meta['owner'],'tx_hash':sent}).json()['status']=='FINALIZED'
            assert len(reads)==count
            assert client.get(base,headers=headers).json()[0]['receipt']['transferred_wei']==str(10**15)


def test_lock_during_funding_rpc_leaves_no_record_and_never_holds_the_vault_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import select,func
    from app.portfolio.vault import RealVault
    from app.portfolio.models import RealDexGasFundingIntent
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
        manager.revoke();vault.lock();entered=Event();release=Event()
        @contextmanager
        def factory():
            def read(method,params):
                entered.set();assert release.wait(10);return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client,ThreadPoolExecutor(max_workers=1) as pool:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32));identifier=str(uuid4())
            response=client.post(f'/real/accounts/{account}/dex-operator/restore',headers=headers,json={
                'address':meta['owner'],'request_id':identifier,'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup)})
            assert response.status_code==200,response.text
            future=pool.submit(client.post,f'/real/accounts/{account}/dex-gas-funding',headers=headers,json={
                'request_id':str(uuid4()),'binding_id':binding['id'],'draft_id':identifier,'address':meta['owner'],
                'amount_bnb':'0.001','gas':'21000','gas_price':'1000000000','max_gas_bnb':'0.00003'})
            try:
                assert entered.wait(5)
                assert client.post('/real/vault/lock',headers=headers).status_code==200
            finally:release.set()
            assert future.result(timeout=10).status_code==401
        reopened=RealVault(vault.path);reopened.unlock(PASSWORD)
        try:
            with reopened.session() as db:assert db.scalar(select(func.count()).select_from(RealDexGasFundingIntent))==0
        finally:reopened.lock()
