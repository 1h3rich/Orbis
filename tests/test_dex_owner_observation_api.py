"""RPC observation of signed owner actions, never a backend wallet sender."""
from contextlib import contextmanager
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup,PASSWORD
from tests.test_dex_deployment_receipt import deployment_on_evm
from tests.test_wallet_funding_api import prove


def test_owner_observer_checks_a_finalized_deployment_without_browser_bytecode_or_rpc_payload(tmp_path):
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,evidence=deployment_on_evm(vault,account,now,meta,rpc);vault.lock();calls=[]
        @contextmanager
        def factory():
            def read(method,params):calls.append(method);return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            url=f'/real/accounts/{account}/dex-deployments/{plan["id"]}/observe'
            body={'address':meta['owner'],'tx_hash':evidence['tx_hash']}
            assert client.post(url,json=body).status_code==401
            assert client.post(url,headers=headers,json=body).status_code==409 and not calls
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            result=client.post(url,headers=headers,json=body)
            assert result.status_code==200,result.text
            assert result.headers['cache-control']=='no-store'
            assert result.json()['status']=='FINALIZED' and result.json()['executor_verified']
            assert int(result.json()['receipt']['gas_fee_wei'])>0
            assert all(method not in ('eth_sendTransaction','eth_sendRawTransaction') for method in calls)
            before=len(calls)
            assert client.post(url,headers=headers,json=body).json()['receipt']==result.json()['receipt']
            assert len(calls)==before
            for value in (meta['owner'],meta['operator'],evidence['tx_hash']):assert value.lower() not in result.text.lower()


def test_lost_wallet_hash_can_be_recovered_then_verified_without_binding_a_wrong_pasted_hash(tmp_path):
    from datetime import datetime,timezone
    from tests.test_dex_deployments import prepare
    from app.portfolio import dex_deployments as journal
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        nonce=int(rpc('eth_getTransactionCount',[meta['owner'],'pending']),16)
        plan=prepare(vault,account,now,wallet_address=meta['owner'],nonce=str(nonce))
        with vault.session() as db:
            journal.claim_deployment(db,plan['id'],account_id=account,wallet_address=meta['owner'],plan_digest=plan['plan_digest'],now=now)
        original=rpc('eth_sendTransaction',[plan['transaction']])
        wrong=rpc('eth_sendTransaction',[{'from':meta['owner'],'to':meta['operator'],'value':'0x0','gas':'0x5208','gasPrice':'0x3b9aca00'}])
        vault.lock();calls=[];pending=True
        @contextmanager
        def factory():
            def read(method,params):
                calls.append(method)
                if pending and method=='eth_getTransactionReceipt':return None
                return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            url=f'/real/accounts/{account}/dex-deployments/{plan["id"]}/observe'
            result=client.post(url,headers=headers,json={'address':meta['owner'],'tx_hash':wrong})
            assert result.status_code==200 and result.json()['observation_status']=='PENDING'
            assert result.json()['status']=='WALLET_REQUESTED' and result.json()['receipt'] is None
            pending=False
            result=client.post(url,headers=headers,json={'address':meta['owner'],'tx_hash':wrong})
            assert result.status_code==409
            result=client.post(url,headers=headers,json={'address':meta['owner']})
            assert result.status_code==200,result.text
            assert result.json()['status']=='CODE_OBSERVED' and result.json()['receipt'] is None
            before=len(calls)
            assert client.post(url,headers=headers,json={'address':meta['owner']}).json()['status']=='CODE_OBSERVED'
            assert len(calls)==before
            result=client.post(url,headers=headers,json={'address':meta['owner'],'tx_hash':original})
            assert result.status_code==200,result.text
            assert result.json()['status']=='FINALIZED' and int(result.json()['receipt']['gas_fee_wei'])>0
            before=len(calls)
            assert client.post(url,headers=headers,json={'address':meta['owner'],'tx_hash':wrong}).status_code==409
            assert len(calls)==before
        assert int(rpc('eth_getTransactionCount',[meta['owner'],'pending']),16)==nonce+2


def test_owner_controls_observe_exact_policy_and_offer_only_finite_allowance_caps(tmp_path):
    from datetime import datetime,timezone
    from tests.test_dex_controls import prepared_control
    from app.portfolio import dex_controls as journal
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        deployment,plan,snapshot,args=prepared_control(vault,account,now,meta,rpc)
        with vault.session() as db:journal.claim_control(db,plan['id'],**args)
        original=rpc('eth_sendTransaction',[plan['transaction']]);vault.lock()
        @contextmanager
        def factory():yield rpc
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            prefix=f'/real/accounts/{account}/dex-controls'
            checked=client.post(f'{prefix}/{plan["id"]}/observe',headers=headers,json={'address':meta['owner'],'tx_hash':original})
            assert checked.status_code==200,checked.text
            assert checked.json()['status']=='FINALIZED' and checked.json()['receipt']['new_policy_id']=='1'
            state=client.post(prefix+'/state',headers=headers,json={'address':meta['owner'],'deployment_id':deployment['id']})
            assert state.status_code==200,state.text
            value=state.json()
            assert value['executor_address']==snapshot.executor_address
            assert value['allowance_caps']=={'USDT':plan['limits']['buy_budget'],'WBNB':plan['limits']['max_wbnb_acquired']}
            assert value['allowance_reasons']=={} and not value['automatic_execution']
            # Revocation and inspection must still work after STOP; no invented zero cap.
            from app.live_execution.dex_policy import stop_transaction
            nonce=int(rpc('eth_getTransactionCount',[meta['owner'],'pending']),16)
            tx=stop_transaction(meta['owner'],int(deployment['nonce']),nonce=nonce,gas=100000,gas_price=1000000000)
            rpc('eth_sendTransaction',[tx])
            stopped=client.post(prefix+'/state',headers=headers,json={'address':meta['owner'],'deployment_id':deployment['id']})
            assert stopped.status_code==200,stopped.text
            assert stopped.json()['operator_stopped']
            assert stopped.json()['allowance_caps']=={'USDT':None,'WBNB':None}
            assert set(stopped.json()['allowance_reasons'])=={'USDT','WBNB'}


def test_owner_observation_rejects_cross_origin_and_records_reverted_creation_gas(tmp_path):
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,evidence=deployment_on_evm(vault,account,now,meta,rpc,fee=3000);vault.lock();calls=[]
        @contextmanager
        def factory():
            def read(method,params):calls.append(method);return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            url=f'/real/accounts/{account}/dex-deployments/{plan["id"]}/observe'
            body={'address':meta['owner'],'tx_hash':evidence['tx_hash']}
            assert client.post(url,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
            assert not calls
            result=client.post(url,headers=headers,json=body)
            assert result.status_code==200,result.text
            assert result.json()['status']=='REVERTED' and not result.json()['executor_verified']
            assert int(result.json()['receipt']['gas_fee_wei'])>0


def test_owner_observer_does_not_hold_real_lock_or_commit_after_wallet_session_revocation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    vault,account,now=setup(tmp_path);entered=Event();release=Event()
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,evidence=deployment_on_evm(vault,account,now,meta,rpc);vault.lock()
        @contextmanager
        def factory():
            def read(method,params):
                if method=='eth_chainId':entered.set();assert release.wait(10)
                return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            url=f'/real/accounts/{account}/dex-deployments/{plan["id"]}/observe'
            with ThreadPoolExecutor(max_workers=1) as workers:
                future=workers.submit(client.post,url,headers=headers,json={'address':meta['owner'],'tx_hash':evidence['tx_hash']})
                try:
                    assert entered.wait(5)
                    assert client.post('/real/vault/lock',headers=headers).status_code==200
                finally:release.set()
                assert future.result(timeout=10).status_code==401
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            history=client.get(f'/real/accounts/{account}/dex-deployments',headers=headers).json()
            assert history[0]['status']=='HASH_REPORTED' and history[0]['receipt'] is None


def test_unfinalized_or_incoherent_rpc_receipt_does_not_finalize_or_poison_the_journal(tmp_path):
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,evidence=deployment_on_evm(vault,account,now,meta,rpc);vault.lock();mode='unfinalized'
        @contextmanager
        def factory():
            def read(method,params):
                value=rpc(method,params)
                if mode=='unfinalized' and method=='eth_getBlockByNumber' and params[0]=='finalized':
                    return {**value,'number':hex(int(evidence['receipt']['blockNumber'],16)-1)}
                if mode=='wrong_code' and method=='eth_getCode':return '0x00'
                if mode=='changed_block' and method=='eth_getBlockByNumber' and params[0]!='finalized':
                    return {**value,'hash':'0x'+'ab'*32}
                return value
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            url=f'/real/accounts/{account}/dex-deployments/{plan["id"]}/observe'
            body={'address':meta['owner'],'tx_hash':evidence['tx_hash']}
            pending=client.post(url,headers=headers,json=body)
            assert pending.status_code==200 and pending.json()['observation_status']=='PENDING'
            assert pending.json()['status']=='HASH_REPORTED' and pending.json()['receipt'] is None
            for mode in ('wrong_code','changed_block'):
                assert client.post(url,headers=headers,json=body).status_code==409
                history=client.get(f'/real/accounts/{account}/dex-deployments',headers=headers).json()
                assert history[0]['status']=='HASH_REPORTED' and history[0]['receipt'] is None
            mode='valid'
            checked=client.post(url,headers=headers,json=body)
            assert checked.status_code==200 and checked.json()['status']=='FINALIZED'
