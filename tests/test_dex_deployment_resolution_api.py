"""Protected replacement review uses only configured RPC, outside session locks."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from tests.dex_evm_fixture import evm
from tests.test_wallet_funding_api import prove


def test_private_resolution_requires_original_scope_review_and_records_once(tmp_path):
    with evm(tmp_path/'evm') as (meta,rpc):
        reads=[]
        @contextmanager
        def factory():
            def read(method,params):reads.append(method);return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(tmp_path/'real.db',dex_rpc_factory=factory))
        with TestClient(app) as client:
            password='own replacement API vault password'
            token=client.post('/real/vault/create',json={'passphrase':password}).json()['token'];headers={'X-Orbis-Vault-Token':token}
            account=client.post('/real/accounts',headers=headers,json={'name':'Wallet','base_currency':'USDT'}).json()['id']
            owner=Account.from_key('0x'+'11'*32);prove(client,headers,account,owner)
            base=f'/real/accounts/{account}/dex-deployments'
            plan=client.post(base,headers=headers,json={'request_id':str(uuid4()),'address':owner.address,'chain_id':'0x38',
                'fee':500,'nonce':str(int(rpc('eth_getTransactionCount',[owner.address,'pending']),16)),
                'gas':'3500000','gas_price':'1000000000','max_gas_bnb':'0.005'}).json()
            item=base+'/'+plan['id'];claim={'address':owner.address,'plan_digest':plan['plan_digest'],'confirmation':'DESPLEGAR_EJECUTOR_DEX'}
            assert client.post(item+'/claim',headers=headers,json=claim).status_code==200
            assert client.post(item+'/discover',json={'address':owner.address}).status_code==401
            pending=client.post(item+'/discover',headers=headers,json={'address':owner.address})
            assert pending.status_code==409 and 'finalizada' in pending.json()['detail']
            reads.clear()
            tx_hash=rpc('eth_sendTransaction',[{'from':owner.address,'to':owner.address,'nonce':hex(int(plan['nonce'])),'value':'0x0','data':'0x','gas':'0x5208','gasPrice':'0x3b9aca00'}])
            body={'address':owner.address,'tx_hash':tx_hash}
            assert client.post(item+'/resolution-preview',json=body).status_code==401
            assert client.post(item+'/resolution-preview',headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
            assert client.post(item+'/resolution-preview',headers=headers,json={**body,'evidence':{}}).status_code==422
            wrong={**body,'address':meta['operator']}
            assert client.post(item+'/resolution-preview',headers=headers,json=wrong).status_code==409 and not reads
            reviewed=client.post(item+'/resolution-preview',headers=headers,json=body);assert reviewed.status_code==200,reviewed.text
            assert reviewed.headers['cache-control']=='no-store' and reviewed.json()['outcome']=='CANCELLED'
            p=reviewed.json();commit={**body,'preview_digest':p['preview_digest'],'source_state_digest':p['source_state_digest'],'confirmation':'RESOLVER_REEMPLAZO_DEX'}
            found=client.post(item+'/discover',headers=headers,json={'address':owner.address})
            assert found.status_code==200 and found.json()=={**p,'discovered_tx_hash':tx_hash},found.text
            assert found.headers['cache-control']=='no-store'
            assert client.post(item+'/resolve',headers=headers,json={**commit,'preview_digest':'ab'*32}).status_code==409
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:client.post(item+'/resolve',headers=headers,json=commit),range(2)))
            assert all(result.status_code==200 for result in results),[result.text for result in results]
            history=client.get(base,headers=headers);assert history.json()[0]['resolution']['outcome']=='CANCELLED'
            assert history.json()[0]['status']=='WALLET_REQUESTED' and not history.json()[0]['executor_verified']
            assert owner.address.lower() not in history.text.lower() and tx_hash.lower() not in history.text.lower()
            assert not any('send' in method.lower() for method in reads)
            assert client.post('/real/vault/backup',headers=headers).status_code==200
            assert client.post('/real/vault/lock',headers=headers).status_code==200
            assert client.post(item+'/resolve',headers=headers,json=commit).status_code==401
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':password}).json()['token']}
            assert client.post(item+'/resolve',headers=headers,json=commit).status_code==409
            prove(client,headers,account,owner)
            assert client.post(item+'/resolve',headers=headers,json=commit).status_code==200


def test_lock_during_resolution_rpc_read_never_commits_a_late_resolution(tmp_path):
    entered,release=Event(),Event();gate={'on':False}
    with evm(tmp_path/'evm') as (meta,rpc):
        @contextmanager
        def factory():
            if gate['on']:entered.set();assert release.wait(10)
            yield rpc
        app=FastAPI();app.include_router(create_real_router(tmp_path/'real.db',dex_rpc_factory=factory))
        with TestClient(app) as client:
            password='own blocked-read replacement vault password'
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/create',json={'passphrase':password}).json()['token']}
            account=client.post('/real/accounts',headers=headers,json={'name':'Wallet'}).json()['id'];owner=Account.from_key('0x'+'11'*32);prove(client,headers,account,owner)
            base=f'/real/accounts/{account}/dex-deployments'
            plan=client.post(base,headers=headers,json={'request_id':str(uuid4()),'address':owner.address,'chain_id':'0x38','fee':500,'nonce':str(int(rpc('eth_getTransactionCount',[owner.address,'pending']),16)),
                'gas':'3500000','gas_price':'1000000000','max_gas_bnb':'0.005'}).json()
            item=base+'/'+plan['id'];assert client.post(item+'/claim',headers=headers,json={'address':owner.address,'plan_digest':plan['plan_digest'],'confirmation':'DESPLEGAR_EJECUTOR_DEX'}).status_code==200
            tx_hash=rpc('eth_sendTransaction',[{'from':owner.address,'to':owner.address,'nonce':hex(int(plan['nonce'])),'gas':'0x5208','gasPrice':'0x3b9aca00','value':'0x0','data':'0x'}])
            body={'address':owner.address,'tx_hash':tx_hash};preview=client.post(item+'/resolution-preview',headers=headers,json=body).json()
            commit={**body,'preview_digest':preview['preview_digest'],'source_state_digest':preview['source_state_digest'],'confirmation':'RESOLVER_REEMPLAZO_DEX'}
            gate['on']=True
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(client.post,item+'/resolve',headers=headers,json=commit)
                try:
                    assert entered.wait(5)
                    assert client.post('/real/vault/lock',headers=headers).status_code==200
                finally:release.set()
                assert pending.result(10).status_code in (401,409)
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':password}).json()['token']}
            assert 'resolution' not in client.get(base,headers=headers).json()[0]
