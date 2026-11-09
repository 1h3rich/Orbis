"""Protected cancellation HTTP flow with signed transactions on our Anvil."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
import json
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.live_execution.dex_gas_cancellation_transport import BnbGasCancellationSender
from app.portfolio.api import create_real_router
from tests.dex_gas_cancellation_fixture import pending_return
from tests.test_dex_deployments import PASSWORD
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
from tests.test_dex_gas_funding import now
from tests.test_wallet_funding_api import prove


@contextmanager
def http_case(directory,monkeypatch,*,before_read=lambda:None,lost=False,arrived=True,original_wins=False):
    with pending_return(directory) as (values,original,original_hash,_):
        vault,account,manager,draft,meta,rpc,_,_=values
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
        manager.revoke();vault.lock();posts=[];controls={'receipts':True}
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def read(method,params):
            if not controls['receipts'] and method=='eth_getTransactionReceipt':return None
            return rpc(method,params)
        @contextmanager
        def reader():before_read();yield read
        def sender(plan):
            actual=BnbGasCancellationSender(plan);send=actual.send
            def post(raw):
                posts.append(1)
                if original_wins:rpc('miner_start',[1]);rpc('evm_mine',[])
                result=send(raw) if arrived else None
                if not original_wins:rpc('miner_start',[1]);rpc('evm_mine',[])
                if lost:raise TimeoutError('controlled lost reply')
                return result
            actual.send=post;return actual
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=reader,dex_cancel_sender_factory=sender))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            base=f'/real/accounts/{account}/dex-gas-cancellations'
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            restored_id=str(uuid4())
            response=client.post(f'/real/accounts/{account}/dex-operator/restore',headers=headers,json={
                'address':meta['owner'],'request_id':restored_id,'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup)})
            assert response.status_code==200,response.text
            body={'address':meta['owner'],'request_id':str(uuid4()),'return_id':original['id'],'draft_id':restored_id,
                'gas_price':'1200000000','bump_percent':10,'max_gas_bnb':'0.0001'}
            yield client,headers,base,body,values,original_hash,posts,controls


def test_cancellation_api_checks_permissions_cost_and_exact_confirmation_and_recovers_after_reopen(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,original_hash,posts,_):
        assert client.get(base).status_code==401 and client.post(base,json=body).status_code==401
        assert client.post(base,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        for extra in ({'destination':body['address']},{'raw_transaction':'0x'},{'gas':22000},{'bump_percent':True}):
            assert client.post(base,headers=headers,json={**body,**extra}).status_code==422
        assert client.post(base,headers=headers,json={**body,'gas_price':'1050000000'}).status_code==409
        assert client.post(base,headers=headers,json={**body,'max_gas_bnb':'0.000001'}).status_code==409
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        review=response.json();url=base+'/'+review['id'];assert review['status']=='PREPARED' and not posts
        assert response.headers['cache-control']=='no-store'
        assert client.post(base,headers=headers,json=body).json()['replayed']
        submit={'address':body['address'],'draft_id':body['draft_id'],'plan_digest':review['plan_digest'],
            'original_hash':original_hash,'confirmation':'CANCELAR_DEVOLUCION_BNB'}
        assert client.post(url+'/submit',headers=headers,json={**submit,'confirmation':'si'}).status_code==422
        assert client.post(url+'/submit',headers=headers,json={**submit,'plan_digest':'ab'*32}).status_code==409
        assert client.post(url+'/submit',headers=headers,json={**submit,'original_hash':'0x'+'ab'*32}).status_code==409
        result=client.post(url+'/submit',headers=headers,json=submit);assert result.status_code==200,result.text
        done=result.json();assert done['winner']=='CANCELLATION' and done['receipt']['gas_fee_wei']=='25200000000000'
        assert done['receipt']['transferred_wei']=='0' and posts==[1]
        assert client.post(url+'/submit',headers=headers,json=submit).json()['replayed_without_post'] and posts==[1]
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        detail=client.get(url,headers=headers);assert detail.status_code==200,detail.text
        assert detail.json()['receipt']==done['receipt'] and detail.headers['cache-control']=='no-store'
        assert client.get(base,headers=headers).json()[0]['winner_id']==review['id']
        assert posts==[1]


def test_cancellation_lost_reply_get_is_passive_and_observation_requires_fresh_wallet_proof(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch,lost=True) as (client,headers,base,body,values,original_hash,posts,controls):
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        review=response.json();url=base+'/'+review['id'];controls['receipts']=False
        submit={'address':body['address'],'draft_id':body['draft_id'],'plan_digest':review['plan_digest'],'confirmation':'CANCELAR_DEVOLUCION_BNB'}
        result=client.post(url+'/submit',headers=headers,json=submit);assert result.status_code==200,result.text
        done=result.json();tx_hash=done['tx_hash']
        assert done['send_result']=='UNKNOWN' and done['status']=='BROADCAST_ATTEMPTED' and posts==[1]
        assert client.post(url+'/cancel',headers=headers,json={'address':body['address'],'confirmation':'RETIRAR_REVISION_CANCELACION_BNB'}).status_code==409
        assert client.post(url+'/abandon',headers=headers,json={'address':body['address'],'confirmation':'DESCARTAR_CANCELACION_BNB_NO_EMITIDA'}).status_code==409
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.get(url,headers=headers).json()['status']=='BROADCAST_ATTEMPTED' and posts==[1]
        assert client.post(url+'/observe',headers=headers,json={'address':body['address'],'tx_hash':tx_hash}).status_code==409
        prove(client,headers,values[1],Account.from_key('0x'+'11'*32));controls['receipts']=True
        assert client.post(url+'/observe',headers=headers,json={'address':body['address'],'tx_hash':'0x'+'ab'*32}).status_code==409
        response=client.post(url+'/observe',headers=headers,json={'address':body['address'],'tx_hash':tx_hash})
        assert response.status_code==200 and response.json()['winner']=='CANCELLATION',response.text
        assert posts==[1]


def test_original_wins_after_cancellation_grant_and_is_never_misreported_as_cancelled(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch,original_wins=True) as (client,headers,base,body,values,original_hash,posts,_):
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        review=response.json();url=base+'/'+review['id']
        response=client.post(url+'/submit',headers=headers,json={'address':body['address'],'draft_id':body['draft_id'],
            'plan_digest':review['plan_digest'],'original_hash':original_hash,'confirmation':'CANCELAR_DEVOLUCION_BNB'})
        assert response.status_code==200,response.text
        done=response.json();assert done['winner']=='ORIGINAL' and int(done['receipt']['transferred_wei'])>0 and done['send_result']=='UNKNOWN'
        assert done['receipt']['gas_fee_wei']=='21000000000000' and posts==[1]
        assert client.post('/real/vault/backup',headers=headers).status_code==200


def test_lock_while_reading_cancellation_nonce_prevents_late_signature_and_post(tmp_path,monkeypatch):
    entered,release=Event(),Event();gate={'enabled':False}
    def before_read():
        if gate['enabled']:entered.set();assert release.wait(10)
    with http_case(tmp_path,monkeypatch,before_read=before_read) as (client,headers,base,body,values,_,posts,_):
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        review=response.json();url=base+'/'+review['id'];gate['enabled']=True
        submit={'address':body['address'],'draft_id':body['draft_id'],'plan_digest':review['plan_digest'],'confirmation':'CANCELAR_DEVOLUCION_BNB'}
        with ThreadPoolExecutor(max_workers=1) as pool:
            job=pool.submit(client.post,url+'/submit',headers=headers,json=submit)
            try:
                assert entered.wait(5);assert client.post('/real/vault/lock',headers=headers).status_code==200
            finally:release.set()
            assert job.result(15).status_code==401
        assert not posts
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.get(url,headers=headers).json()['status']=='PREPARED'
