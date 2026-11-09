"""Protected native recovery against owned EVMs, with no replacement emission."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
import json
from eth_account import Account
from eth_utils import to_checksum_address
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from tests.test_dex_gas_funding import case
from tests.test_dex_deployments import PASSWORD
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
from tests.test_wallet_funding_api import prove


@contextmanager
def api_case(directory):
    with case(directory) as values:
        vault,account,manager,draft,meta,rpc,_,binding=values
        from datetime import datetime,timezone
        at=datetime.now(timezone.utc)
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=at)
        with manager._copy(draft['id'],account,meta['owner'],at) as (_,key):fixture_key=bytes(key)
        manager.revoke();vault.lock();reads=[];gate={'on':False};entered,release=Event(),Event();posts=[]
        @contextmanager
        def reader():
            if gate['on']:entered.set();assert release.wait(10)
            def read(method,params):reads.append(method);return rpc(method,params)
            yield read
        @contextmanager
        def no_arrival(plan):
            def send(raw):posts.append('original_attempt');raise TimeoutError('Own fixture: no POST reached the node')
            yield SimpleNamespace(send=send)
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=reader,dex_return_sender_factory=no_arrival))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32));draft_id=str(uuid4())
            base=f'/real/accounts/{account}'
            restored=client.post(base+'/dex-operator/restore',headers=headers,json={'address':meta['owner'],'request_id':draft_id,
                'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup)})
            assert restored.status_code==200,restored.text
            yield client,headers,base,draft_id,values,fixture_key,reads,gate,entered,release,posts


def prepare_funding(client,headers,base,draft_id,values):
    _,_,_,_,meta,_,_,binding=values
    response=client.post(base+'/dex-gas-funding',headers=headers,json={'address':meta['owner'],'request_id':str(uuid4()),'binding_id':binding['id'],
        'draft_id':draft_id,'amount_bnb':'0.001','gas':'21000','gas_price':'1000000000','max_gas_bnb':'0.00003'})
    assert response.status_code==200,response.text
    plan=response.json()
    response=client.post(base+'/dex-gas-funding/'+plan['id']+'/claim',headers=headers,json={'address':meta['owner'],
        'plan_digest':plan['plan_digest'],'confirmation':'FINANCIAR_GAS_OPERADORA_DEX'})
    assert response.status_code==200 and response.json()['claim_granted'],response.text
    return plan


def test_native_funding_api_requires_owner_scope_and_records_one_resolution_without_send(tmp_path):
    with api_case(tmp_path) as (client,headers,base,draft_id,values,key,reads,gate,entered,release,posts):
        _,account,_,draft,meta,rpc,_,_=values;plan=prepare_funding(client,headers,base,draft_id,values)
        url=base+'/dex-native-resolutions/GAS_FUNDING/'+plan['id']
        discovery={'address':meta['owner']}
        assert client.post(url+'/discover',json=discovery).status_code==401
        pending=client.post(url+'/discover',headers=headers,json=discovery)
        assert pending.status_code==409 and 'finalizada' in pending.json()['detail']
        tx_hash=rpc('eth_sendTransaction',[{**plan['transaction'],'to':meta['owner'],'value':'0x0'}])
        body={**discovery,'tx_hash':tx_hash}
        reads.clear()
        assert client.post(url+'/resolution-preview',json=body).status_code==401
        assert client.post(url+'/resolution-preview',headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        assert client.post(url+'/resolution-preview',headers=headers,json={**body,'evidence':{}}).status_code==422
        assert client.post(url+'/resolution-preview',headers=headers,json={**body,'address':meta['operator']}).status_code==409 and not reads
        assert client.post(url+'/discover',headers=headers,json=body).status_code==422 and not reads
        found=client.post(url+'/discover',headers=headers,json=discovery);assert found.status_code==200,found.text
        assert found.json()['discovered_tx_hash']==tx_hash and found.headers['cache-control']=='no-store'
        response=client.post(url+'/resolution-preview',headers=headers,json=body);assert response.status_code==200,response.text
        preview=response.json();assert preview['outcome']=='CANCELLED' and response.headers['cache-control']=='no-store'
        assert {k:v for k,v in found.json().items() if k!='discovered_tx_hash'}==preview
        commit={**body,'preview_digest':preview['preview_digest'],'source_state_digest':preview['source_state_digest'],'confirmation':'RESOLVER_REEMPLAZO_BNB'}
        assert client.post(url+'/resolve',headers=headers,json={**commit,'preview_digest':'ab'*32}).status_code==409
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:client.post(url+'/resolve',headers=headers,json=commit),range(2)))
        assert all(item.status_code==200 for item in results),[item.text for item in results]
        assert results[0].json()==results[1].json()
        history=client.get(base+'/dex-gas-funding',headers=headers)
        assert history.json()[0]['resolution']['outcome']=='CANCELLED' and history.json()[0]['status']=='WALLET_REQUESTED'
        assert tx_hash not in history.text and meta['owner'].lower() not in history.text.lower()
        assert client.post(url+'/discover',headers=headers,json=discovery).json()==results[0].json()
        assert not posts and not any('send' in method.lower() for method in reads)
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.post(url+'/resolve',headers=headers,json=commit).status_code==409
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        assert client.post(url+'/resolve',headers=headers,json=commit).json()==results[0].json()


def test_native_return_resolution_needs_no_live_operator_and_lock_during_read_prevents_late_commit(tmp_path):
    with api_case(tmp_path) as (client,headers,base,draft_id,values,key,reads,gate,entered,release,posts):
        _,account,_,draft,meta,rpc,_,binding=values;plan=prepare_funding(client,headers,base,draft_id,values)
        funding_hash=rpc('eth_sendTransaction',[plan['transaction']])
        response=client.post(base+'/dex-gas-funding/'+plan['id']+'/observe',headers=headers,json={'address':meta['owner'],'tx_hash':funding_hash})
        assert response.status_code==200,response.text
        prepared=client.post(base+'/dex-gas-returns',headers=headers,json={'address':meta['owner'],'request_id':str(uuid4()),'binding_id':binding['id'],
            'draft_id':draft_id,'gas_price':'1000000000','max_gas_bnb':'0.000021'}).json()
        item=base+'/dex-gas-returns/'+prepared['id']
        submitted=client.post(item+'/submit',headers=headers,json={'address':meta['owner'],'draft_id':draft_id,'plan_digest':prepared['plan_digest'],'confirmation':'DEVOLVER_GAS_OPERADORA_DEX'})
        assert submitted.status_code==200 and submitted.json()['status']=='BROADCAST_ATTEMPTED',submitted.text
        assert posts==['original_attempt']
        tx={'chainId':56,'nonce':int(prepared['nonce']),'to':to_checksum_address(draft['operator_address']),'value':0,'data':'0x','gas':21000,'gasPrice':10**9}
        signed=Account.sign_transaction(tx,key);tx_hash=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
        url=base+'/dex-native-resolutions/GAS_RETURN/'+prepared['id'];body={'address':meta['owner'],'tx_hash':tx_hash}
        preview=client.post(url+'/resolution-preview',headers=headers,json=body);assert preview.status_code==200,preview.text
        p=preview.json();commit={**body,'preview_digest':p['preview_digest'],'source_state_digest':p['source_state_digest'],'confirmation':'RESOLVER_REEMPLAZO_BNB'}
        gate['on']=True
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(client.post,url+'/resolve',headers=headers,json=commit)
            try:
                assert entered.wait(5)
                assert client.post('/real/vault/lock',headers=headers).status_code==200
            finally:release.set()
            assert pending.result(10).status_code in (401,409)
        gate['on']=False
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert 'resolution' not in client.get(item,headers=headers).json()
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        found=client.post(url+'/discover',headers=headers,json={'address':meta['owner']})
        assert found.status_code==200,found.text
        assert found.json()=={**p,'discovered_tx_hash':tx_hash}
        result=client.post(url+'/resolve',headers=headers,json=commit);assert result.status_code==200,result.text
        assert result.json()['outcome']=='CANCELLED' and client.get(item,headers=headers).json()['resolution']==result.json()
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert posts==['original_attempt'] and not any('send' in method.lower() for method in reads)
