"""Actual private HTTP observation with signed owned receipts and SQLCipher."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.api import create_real_router
from app.portfolio import wallet_swap as swaps
from app.portfolio.models import RealDexCallResolution
from tests.test_dex_external_observation import external_case
from tests.test_economic_receipt_source import counts
from tests.test_wallet_swap import PASSWORD
from tests.test_wallet_funding_api import prove


@contextmanager
def api_case(directory,mode='TRANSFER'):
    with external_case(directory,mode)as original:
        original.vault.lock();holder={};reads=[];gate={'block':False,'failure':False};entered=Event();release=Event()
        @contextmanager
        def reader():
            if gate['block']:entered.set();assert release.wait(10)
            if gate['failure']:raise RuntimeError('https://private-rpc.invalid/secret-endpoint-password')
            def rpc(method,params):
                assert 'send'not in method.lower()and 'sign'not in method.lower(),method
                reads.append(method);return original.rpc(method,params)
            yield rpc
        start=datetime.now(timezone.utc)
        def supervisor(v,c):
            holder['vault']=v
            return DexBotSupervisor(v,c,clock=lambda:original.now+(datetime.now(timezone.utc)-start))
        app=FastAPI();app.include_router(create_real_router(original.vault.path,dex_rpc_factory=reader,dex_bot_supervisor_factory=supervisor))
        with TestClient(app)as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            info={**vars(original),'vault':holder['vault'],'client':client,'headers':headers,'reads':reads,
                'gate':gate,'entered':entered,'release':release,
                'url':f'/real/accounts/{original.account}/dex-call-resolutions/WALLET_SWAP/{original.source.id}/external-observation',
                'body':{'address':original.meta['owner'],'tx_hash':original.hash}}
            try:yield SimpleNamespace(**info)
            finally:release.set();client.post('/real/vault/lock',headers=headers)


def owner(case):
    prove(case.client,case.headers,case.account,Account.from_key('0x'+'11'*32))


def test_observation_requires_exact_eoa_context_and_never_accepts_client_evidence_or_authority(tmp_path):
    with api_case(tmp_path)as case:
        assert case.client.post(case.url,json=case.body).status_code==401
        assert case.client.post(case.url,headers=case.headers,json=case.body).status_code==409
        assert not case.reads
        owner(case)
        for extra in ({'evidence':case.evidence},{'confirmation':'RESOLVE'},{'accept_changes':True}):
            assert case.client.post(case.url,headers=case.headers,json={**case.body,**extra}).status_code==422
        assert case.client.post(case.url,headers={**case.headers,'Origin':'https://outside.invalid'},json=case.body).status_code==403
        assert case.client.post(case.url,headers=case.headers,json={**case.body,'address':case.meta['operator']}).status_code==409
        assert not case.reads
        before=counts(case.vault)
        search=case.client.post(case.url.removesuffix('/external-observation')+'/discover',headers=case.headers,
            json={'address':case.meta['owner']})
        assert search.status_code==200,search.text
        assert search.json()=={'external_review_required':True,'discovered_tx_hash':case.hash}
        response=case.client.post(case.url,headers=case.headers,json=case.body)
        assert response.status_code==200,response.text
        assert response.headers['cache-control']=='no-store'
        data=response.json();assert data['observed']and not data['saved']
        assert all(data[field]is False for field in ('resolves_intent','moves_funds','trading_authority'))
        assert data['observation']['transfers'][0]['units']=='5000000000000000'
        for private in (case.meta['owner'],case.meta['operator'],case.hash):assert private.lower()not in response.text.lower()
        assert counts(case.vault)==before
        with case.vault.session()as db:
            assert db.scalar(select(func.count()).select_from(RealDexCallResolution))==0
            row,_,_=swaps.swap_record(db,case.plan['id'])
            assert (row.payload_json,row.state_json,row.updated_at)==case.before


def test_preview_digest_is_stable_when_finalized_head_and_observation_time_advance(tmp_path):
    with api_case(tmp_path)as case:
        owner(case)
        first=case.client.post(case.url,headers=case.headers,json=case.body)
        assert first.status_code==200,first.text
        for _ in range(3):case.rpc('evm_mine',[])
        later=case.client.post(case.url,headers=case.headers,json=case.body)
        assert later.status_code==200,later.text
        assert later.json()['preview_digest']==first.json()['preview_digest']
        assert later.json()['observation']==first.json()['observation']
        assert all('send'not in method.lower()and 'sign'not in method.lower()for method in case.reads)


@pytest.mark.parametrize('change',['LOCK','SOURCE'])
def test_rpc_has_no_private_lease_and_stale_observation_cannot_escape(tmp_path,change):
    with api_case(tmp_path)as case:
        owner(case);case.gate['block']=True
        with ThreadPoolExecutor(max_workers=1)as pool:
            future=pool.submit(case.client.post,case.url,headers=case.headers,json=case.body)
            assert case.entered.wait(5)
            if change=='LOCK':
                assert case.client.post('/real/vault/lock',headers=case.headers).status_code==200
            else:
                with case.vault.session()as db:
                    swaps.report_swap(db,case.plan['id'],account_id=case.account,tx_hash=case.hash,now=case.now)
            case.release.set();response=future.result(timeout=10)
        assert response.status_code==(401 if change=='LOCK'else 409),response.text
        assert 'observation_ref'not in response.text


def test_detached_crypto_does_not_hold_access_or_sql_and_lock_wins_before_return(tmp_path,monkeypatch):
    from app.portfolio import dex_external_observation_api as module
    with api_case(tmp_path)as case:
        owner(case);original=module.verify_external_call
        with ThreadPoolExecutor(max_workers=1)as pool:
            def checked(*args,**kwargs):
                result=original(*args,**kwargs)
                assert pool.submit(case.client.post,'/real/vault/lock',headers=case.headers).result(timeout=5).status_code==200
                return result
            monkeypatch.setattr(module,'verify_external_call',checked)
            response=case.client.post(case.url,headers=case.headers,json=case.body)
        assert response.status_code==401,response.text
        assert not case.vault.unlocked


def test_provider_error_is_sanitized_and_unfinalized_hash_stays_pending(tmp_path):
    with api_case(tmp_path)as case:
        owner(case);case.gate['failure']=True
        response=case.client.post(case.url,headers=case.headers,json=case.body)
        assert response.status_code==503 and 'secret-endpoint-password'not in response.text and 'private-rpc.invalid'not in response.text
        case.gate['failure']=False
        response=case.client.post(case.url,headers=case.headers,json={**case.body,'tx_hash':'0x'+'44'*32})
        assert response.status_code==409 and 'finalizado'in response.text
        with case.vault.session()as db:assert swaps.swap_record(db,case.plan['id'])[0].state_json==case.before[1]


@pytest.mark.parametrize('mode',['KNOWN','CANCEL','REVERTED'])
def test_external_endpoint_does_not_reclassify_supported_existing_outcomes(tmp_path,mode):
    with api_case(tmp_path,mode)as case:
        owner(case)
        response=case.client.post(case.url,headers=case.headers,json=case.body)
        assert response.status_code==409,response.text
        assert 'observation_ref'not in response.text
        assert counts(case.vault)==(0,0,0,0)
        if mode=='KNOWN':
            base=case.url.removesuffix('/external-observation');body={**case.body,'accept_changes':True}
            preview=case.client.post(base+'/resolution-preview',headers=case.headers,json=body)
            assert preview.status_code==200,preview.text
            saved=case.client.post(base+'/resolve',headers=case.headers,json={**body,
                'preview_digest':preview.json()['preview_digest'],'source_state_digest':preview.json()['source_state_digest'],
                'confirmation':'RESOLVER_LLAMADA_DEX'})
            assert saved.status_code==200,saved.text
            before=len(case.reads)
            assert case.client.post(case.url,headers=case.headers,json=case.body).status_code==409
            assert len(case.reads)==before


def test_passive_observation_does_not_extend_real_session_but_manual_action_does(tmp_path,monkeypatch):
    from app.portfolio.api import RealAccess
    with api_case(tmp_path)as case:
        owner(case);touched=[];original=RealAccess._touch
        def touch(access):touched.append(True);original(access)
        monkeypatch.setattr(RealAccess,'_touch',touch)
        response=case.client.post(case.url,headers={**case.headers,'X-Orbis-Passive':'1'},json=case.body)
        assert response.status_code==200,response.text
        assert not touched
        response=case.client.post(case.url,headers=case.headers,json=case.body)
        assert response.status_code==200,response.text
        assert touched==[True]


def test_lock_wins_over_a_provider_failure_without_exposing_endpoint(tmp_path):
    with api_case(tmp_path)as case:
        owner(case);case.gate.update(block=True,failure=True)
        with ThreadPoolExecutor(max_workers=1)as pool:
            future=pool.submit(case.client.post,case.url,headers=case.headers,json=case.body)
            assert case.entered.wait(5)
            assert case.client.post('/real/vault/lock',headers=case.headers).status_code==200
            case.release.set();response=future.result(timeout=10)
        assert response.status_code==401,response.text
        assert 'private-rpc.invalid'not in response.text and 'secret-endpoint-password'not in response.text
