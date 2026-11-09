"""Private HTTP order save, stale-result rejection and no resend after lost reply."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.portfolio.models import RealDexExternalOrderProof
from tests.test_dex_external_observation_api import api_case
from tests.test_dex_external_observation_save_api import review
from tests.test_wallet_swap import PASSWORD


def ready(c):
    body=review(c);saved=c.client.post(c.url+'/save',headers=c.headers,json=body)
    assert saved.status_code==200,saved.text
    return c.url.removesuffix('/external-observation')+'/external-order'


def order(c,url):
    result=c.client.post(url+'/preview',headers=c.headers,json=c.body)
    assert result.status_code==200,result.text
    return {**c.body,'preview_digest':result.json()['preview_digest'],'confirmation':'GUARDAR_ORDEN_EXTERNA'}


def test_order_requires_private_source_wallet_and_exact_confirmation_with_no_client_evidence(tmp_path):
    with api_case(tmp_path)as c:
        url=ready(c);before=len(c.reads)
        assert c.client.post(url+'/preview',json=c.body).status_code==401
        assert c.client.get(url).status_code==401
        assert c.client.post(url+'/preview',headers={**c.headers,'Origin':'https://outside.invalid'},json=c.body).status_code==403
        for extra in({'proof':{}},{'observation_id':str(uuid4())}):
            assert c.client.post(url+'/preview',headers=c.headers,json={**c.body,**extra}).status_code==422
        assert c.client.post(url+'/preview',headers=c.headers,json={**c.body,'address':c.meta['operator']}).status_code==409
        assert len(c.reads)==before
        assert c.client.get(url,headers=c.headers).json()['saved']is False
        body=order(c,url)
        assert c.client.post(url+'/save',headers=c.headers,json={**body,'confirmation':'GUARDAR_OBSERVACION_EXTERNA'}).status_code==422
        assert c.client.post(url+'/save',headers=c.headers,json={**body,'preview_digest':'0'*64}).status_code==409
        result=c.client.post(url+'/save',headers=c.headers,json=body)
        assert result.status_code==200,result.text
        assert result.headers['cache-control']=='no-store'
        for key in('applied_to_ledger','resolves_intent','trading_authority'):assert result.json()[key]is False
        for private in(c.meta['owner'],c.meta['operator'],c.hash):assert private.lower()not in result.text.lower()
        assert c.client.get(url.replace(c.account,str(uuid4())),headers=c.headers).status_code==409


def test_real_http_lost_reply_and_reopen_recover_without_wallet_proof_or_rpc(tmp_path):
    with api_case(tmp_path)as c:
        url=ready(c);body=order(c,url)
        for _ in range(3):c.rpc('evm_mine',[])
        result=c.client.post(url+'/save',headers=c.headers,json=body);assert result.status_code==200,result.text
        data=result.json();before=len(c.reads);c.gate['failure']=True
        assert c.client.post(url+'/save',headers=c.headers,json=body).json()==data
        assert len(c.reads)==before
        c.client.post('/real/vault/lock',headers=c.headers)
        token=c.client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token'];c.headers.clear();c.headers.update({'X-Orbis-Vault-Token':token})
        restored=c.client.get(url,headers=c.headers)
        assert restored.status_code==200 and restored.json()==data and len(c.reads)==before
        with c.vault.session()as db:assert len(list(db.scalars(select(RealDexExternalOrderProof))))==1
        assert c.client.get(c.url+'/saved',headers=c.headers).json()['status']=='EFFECTS_PENDING'


def test_rpc_and_crypto_do_not_hold_vault_leases_and_revocation_discards_late_order(tmp_path,monkeypatch):
    from app.portfolio import dex_external_order_api as module
    with api_case(tmp_path)as c:
        url=ready(c);body=order(c,url);c.gate['block']=True
        with ThreadPoolExecutor(max_workers=1)as pool:
            future=pool.submit(c.client.post,url+'/save',headers=c.headers,json=body)
            assert c.entered.wait(5)
            assert c.client.post('/real/vault/lock',headers=c.headers).status_code==200
            c.release.set();response=future.result(timeout=10)
        assert response.status_code==401 and 'certificate_ref'not in response.text
        token=c.client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token'];c.headers.clear();c.headers.update({'X-Orbis-Vault-Token':token})
        from tests.test_dex_external_observation_api import owner
        owner(c);c.gate['block']=False;original=module.verify_external_order
        with ThreadPoolExecutor(max_workers=1)as pool:
            def checked(*args,**kwargs):
                proof=original(*args,**kwargs)
                assert pool.submit(c.client.post,'/real/vault/lock',headers=c.headers).result(timeout=5).status_code==200
                return proof
            monkeypatch.setattr(module,'verify_external_order',checked)
            response=c.client.post(url+'/save',headers=c.headers,json=body)
        assert response.status_code==401


def test_provider_error_is_sanitized_and_wrong_receipt_is_rejected_before_rpc(tmp_path):
    with api_case(tmp_path)as c:
        url=ready(c);before=len(c.reads)
        response=c.client.post(url+'/preview',headers=c.headers,json={**c.body,'tx_hash':'0x'+'ff'*32})
        assert response.status_code==409 and len(c.reads)==before
        c.gate['failure']=True
        response=c.client.post(url+'/preview',headers=c.headers,json=c.body)
        assert response.status_code==503 and 'secret-endpoint-password'not in response.text and 'private-rpc.invalid'not in response.text
        with c.vault.session()as db:assert not list(db.scalars(select(RealDexExternalOrderProof)))
