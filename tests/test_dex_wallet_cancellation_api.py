"""Private HTTP cancellation grants; wallet sends occur only in own EVM client."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from tests.dex_wallet_cancellation_fixture import pending_funding
from tests.test_dex_deployments import PASSWORD
from tests.test_wallet_funding_api import prove


@contextmanager
def http_case(directory,monkeypatch,*,before_read=lambda:None):
    with pending_funding(directory) as (values,source,tx_hash):
        vault,account,manager,draft,meta,rpc,_,_=values
        manager.revoke();vault.lock();reads=[]
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        @contextmanager
        def reader():
            before_read()
            def read(method,params):reads.append(method);return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=reader))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            body={'address':meta['owner'],'recipient':draft['operator_address'],'request_id':str(uuid4()),'funding_id':source['id'],
                'original_hash':tx_hash,'gas_price':'1200000000','bump_percent':10,'max_gas_bnb':'0.0001','accept_unknown_fee':False}
            yield client,headers,f'/real/accounts/{account}/dex-wallet-cancellations',body,values,source,reads


def submit_body(body,record):
    return {'address':body['address'],'recipient':body['recipient'],'original_hash':body['original_hash'],
        'plan_digest':record['plan_digest'],'confirmation':'CANCELAR_FINANCIACION_BNB_EN_WALLET'}


def test_owner_only_api_grants_once_and_never_sends_then_records_actual_wallet_receipt(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,source,reads):
        _,_,_,_,meta,rpc,_,_=values
        assert client.get(base).status_code==401 and client.post(base,json=body).status_code==401
        assert client.post(base,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        assert client.post(base,headers=headers,json={**body,'private_key':'not-accepted'}).status_code==422
        assert client.post(base,headers=headers,json={**body,'bump_percent':True}).status_code==422
        assert client.post(base,headers=headers,json={**body,'recipient':meta['owner']}).status_code==409
        assert not reads
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        record=response.json();url=base+'/'+record['id'];assert record['status']=='PREPARED' and response.headers['cache-control']=='no-store'
        assert client.post(base,headers=headers,json=body).json()['replayed']
        claim=submit_body(body,record)
        assert client.post(url+'/claim',headers=headers,json={**claim,'confirmation':'si'}).status_code==422
        assert client.post(url+'/claim',headers=headers,json={**claim,'plan_digest':'ab'*32}).status_code==409
        response=client.post(url+'/claim',headers=headers,json=claim);assert response.status_code==200,response.text
        granted=response.json();tx=granted['transaction']
        assert granted['claim_granted'] and tx['from']==tx['to']==meta['owner'].lower() and tx['value']=='0x0'
        assert tx['nonce']==hex(int(source['nonce'])) and tx['gas']=='0x5208' and tx['data']=='0x'
        replay=client.post(url+'/claim',headers=headers,json=claim).json();assert not replay['claim_granted'] and 'transaction' not in replay
        # No draft restored or server private key. The test wallet signs only
        # after the grant, independently from the HTTP backend.
        cancel_hash=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        response=client.post(url+'/observe',headers=headers,json={'address':body['address'],'tx_hash':cancel_hash})
        assert response.status_code==200,response.text
        done=response.json();assert done['winner']=='CANCELLATION' and done['receipt']['gas_fee_wei']=='25200000000000'
        assert client.post(url+'/reconcile',json={'address':body['address']}).status_code==401
        assert client.post(url+'/reconcile',headers=headers,json={'address':body['address']}).json()['winner']=='CANCELLATION'
        assert client.get(base,headers=headers).json()[0]['winner_id']==record['id']
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert not {'eth_sendRawTransaction','eth_sendTransaction'}.intersection(reads)


def test_unknown_fee_requires_explicit_acceptance_and_reopening_never_grants_again(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,_,reads):
        _,account,_,_,_,_,_,_=values;body={**body,'original_hash':None}
        assert client.post(base,headers=headers,json=body).status_code==409
        body['accept_unknown_fee']=True
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        record=response.json();url=base+'/'+record['id'];claim=submit_body(body,record)
        assert record['original_fee_unknown']
        assert client.post(url+'/claim',headers=headers,json=claim).json()['claim_granted']
        assert client.post(url+'/reconcile',headers=headers,json={'address':body['address']}).status_code==409
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        detail=client.get(url,headers=headers);assert detail.status_code==200 and detail.json()['status']=='WALLET_REQUESTED'
        assert client.post(url+'/claim',headers=headers,json=claim).status_code==409
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        replay=client.post(url+'/claim',headers=headers,json=claim).json();assert not replay['claim_granted'] and 'transaction' not in replay
        assert client.post(url+'/cancel',headers=headers,json={'address':body['address'],'confirmation':'RETIRAR_REVISION_CANCELACION_WALLET_BNB'}).status_code==409
        assert not {'eth_sendRawTransaction','eth_sendTransaction'}.intersection(reads)


def test_explicit_fee_change_resolution_can_link_wallet_result_without_its_raw_hash_after_reopen(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,source,reads):
        _,_,_,_,_,rpc,_,_=values
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        record=response.json();url=base+'/'+record['id'];assert record['original_hash_required']
        granted=client.post(url+'/claim',headers=headers,json=submit_body(body,record)).json()
        tx={**granted['transaction'],'gasPrice':hex(1500000000)}
        tx_hash=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        changed={'address':body['address'],'tx_hash':tx_hash,'accept_changes':True}
        endpoint=base.rsplit('/dex-wallet-cancellations',1)[0]+'/dex-native-resolutions/GAS_FUNDING/'+source['id']
        response=client.post(endpoint+'/resolution-preview',headers=headers,json=changed);assert response.status_code==200,response.text
        preview=response.json()
        response=client.post(endpoint+'/resolve',headers=headers,json={**changed,'preview_digest':preview['preview_digest'],
            'source_state_digest':preview['source_state_digest'],'confirmation':'RESOLVER_REEMPLAZO_BNB'})
        assert response.status_code==200,response.text
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        prove(client,headers,values[1],Account.from_key('0x'+'11'*32))
        done=client.post(url+'/reconcile',headers=headers,json={'address':body['address']});assert done.status_code==200,done.text
        assert done.json()['winner']=='EXTERNAL' and done.json()['receipt']['gas_fee_wei']=='31500000000000'
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert not {'eth_sendRawTransaction','eth_sendTransaction'}.intersection(reads)


def test_original_mines_after_review_so_claim_fails_and_observation_keeps_actual_funding(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,source,reads):
        _,_,_,_,_,rpc,_,_=values
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        record=response.json();url=base+'/'+record['id'];rpc('miner_start',[1]);rpc('evm_mine',[])
        assert client.post(url+'/claim',headers=headers,json=submit_body(body,record)).status_code==409
        assert client.get(url,headers=headers).json()['status']=='PREPARED'
        response=client.post(url+'/observe',headers=headers,json={'address':body['address'],'tx_hash':body['original_hash']})
        assert response.status_code==200,response.text
        done=response.json();assert done['winner']=='ORIGINAL' and done['receipt']['transferred_wei']==source['amount_wei']
        assert not {'eth_sendRawTransaction','eth_sendTransaction'}.intersection(reads)


def test_lock_during_claim_rpc_prevents_late_durable_wallet_grant(tmp_path,monkeypatch):
    entered,release=Event(),Event();gate={'enabled':False}
    def before_read():
        if gate['enabled']:entered.set();assert release.wait(10)
    with http_case(tmp_path,monkeypatch,before_read=before_read) as (client,headers,base,body,values,_,reads):
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        record=response.json();url=base+'/'+record['id'];gate['enabled']=True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,url+'/claim',headers=headers,json=submit_body(body,record))
            try:
                assert entered.wait(5);assert client.post('/real/vault/lock',headers=headers).status_code==200
            finally:release.set()
            assert future.result(15).status_code==401
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.get(url,headers=headers).json()['status']=='PREPARED'
        assert not {'eth_sendRawTransaction','eth_sendTransaction'}.intersection(reads)
