"""Protected generated-key lifecycle; only encrypted backup leaves the server."""
from uuid import uuid4
import json
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from tests.test_dex_deployments import setup,PASSWORD
from tests.test_wallet_funding_api import prove
from tests.test_dex_operator_drafts import BACKUP_PASSWORD


def test_operator_preparation_requires_eoa_proof_and_survives_only_as_an_encrypted_backup(tmp_path):
    vault,account,now=setup(tmp_path);vault.lock()
    app=FastAPI();app.include_router(create_real_router(vault.path))
    owner=Account.from_key('0x'+'11'*32)
    with TestClient(app) as client:
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        base=f'/real/accounts/{account}/dex-operator'
        body={'request_id':str(uuid4()),'address':owner.address}
        assert client.post(base+'/draft',json=body).status_code==401
        assert client.post(base+'/draft',headers=headers,json=body).status_code==409
        prove(client,headers,account,owner)
        assert client.post(base+'/draft',headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        result=client.post(base+'/draft',headers=headers,json=body)
        assert result.status_code==200,result.text
        assert result.headers['cache-control']=='no-store'
        created=result.json();assert created['exists'] and not created['automatic_execution']
        assert client.post(base+'/draft',headers=headers,json=body).json()['operator_address']==created['operator_address']
        args={'draft_id':created['id'],'address':owner.address,'password':BACKUP_PASSWORD}
        malformed=client.post(base+'/backup',headers=headers,json={**args,'password':[BACKUP_PASSWORD]})
        assert malformed.status_code==422 and BACKUP_PASSWORD not in malformed.text
        response=client.post(base+'/backup',headers=headers,json=args)
        assert response.status_code==200,response.text
        backup=response.json()
        verified=client.post(base+'/verify-backup',headers=headers,json={**args,'backup_json':json.dumps(backup)})
        assert verified.status_code==200 and verified.json()['backup_verified'],verified.text
        client.post('/real/vault/lock',headers=headers)
        assert client.post(base+'/status',headers=headers,json={'address':owner.address}).status_code==401
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        prove(client,headers,account,owner)
        assert not client.post(base+'/status',headers=headers,json={'address':owner.address}).json()['exists']
        recovered=client.post(base+'/restore',headers=headers,json={'request_id':str(uuid4()),'address':owner.address,
            'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup)})
        assert recovered.status_code==200,recovered.text
        assert recovered.json()['operator_address']==created['operator_address'] and recovered.json()['backup_verified']
        assert not recovered.json()['automatic_execution']


def test_lock_is_immediate_during_key_encryption_and_a_revoked_request_cannot_export(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    vault,account,now=setup(tmp_path);vault.lock();owner=Account.from_key('0x'+'11'*32)
    app=FastAPI();app.include_router(create_real_router(vault.path));entered=Event();release=Event()
    with TestClient(app) as client:
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        prove(client,headers,account,owner);base=f'/real/accounts/{account}/dex-operator'
        state=client.post(base+'/draft',headers=headers,json={'request_id':str(uuid4()),'address':owner.address}).json()
        original=Account.encrypt
        def delayed(*a,**k):entered.set();assert release.wait(5);return original(*a,**k)
        monkeypatch.setattr(Account,'encrypt',delayed)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future=workers.submit(client.post,base+'/backup',headers=headers,json={'draft_id':state['id'],'address':owner.address,'password':BACKUP_PASSWORD})
            try:
                assert entered.wait(3)
                assert client.post('/real/vault/lock',headers=headers).status_code==200
            finally:release.set()
            result=future.result(timeout=5)
            assert result.status_code==401,result.text
            assert BACKUP_PASSWORD not in result.text and 'ciphertext' not in result.text
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        prove(client,headers,account,owner)
        assert not client.post(base+'/status',headers=headers,json={'address':owner.address}).json()['exists']
