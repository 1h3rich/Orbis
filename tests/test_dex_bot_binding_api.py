"""Private binding with fresh EVM state and EOA proof; no signing or send."""
from contextlib import contextmanager
from datetime import datetime,timezone
from uuid import uuid4
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from tests.dex_bot_source_fixture import SourceCase
from tests.dex_evm_fixture import evm
from tests.test_dex_allowance_journal import ready
from tests.test_dex_deployments import setup,PASSWORD
from tests.test_wallet_funding_api import prove


def test_binding_api_rechecks_wallet_policy_and_replays_after_paper_is_locked(tmp_path):
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory,research=True)
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        deployment=ready(vault,account,now,meta,rpc);vault.lock();reads=[]
        @contextmanager
        def factory():
            def read(method,params):reads.append(method);return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,spot_paper=source.paper,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            base=f'/real/accounts/{account}/dex-bots/bindings'
            body={'request_id':str(uuid4()),'deployment_id':deployment['id'],'address':meta['owner'],'source_kind':'STRATEGY',
                'paper_session_id':source.session['id'],'research_validation_id':source.validation['id'],'entry_usdt':'12','budget_usdt':'60','exposure_usdt':'25',
                'loss_usdt':'5','wbnb_quota':'1','gas_budget_bnb':'0.002','confirmation':'VINCULAR_BOT_DEX'}
            assert client.post(base,json=body).status_code==401
            assert client.get(base).status_code==401
            assert client.post(base,headers=headers,json=body).status_code==409 and not reads
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            assert client.post(base,headers=headers,json={k:v for k,v in body.items() if k!='research_validation_id'}).status_code==409 and not reads
            assert client.post(base,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
            created=client.post(base,headers=headers,json=body)
            assert created.status_code==200,created.text
            assert created.headers['cache-control']=='no-store'
            value=created.json();assert value['status']=='READY' and not value['execution_enabled'] and not value['replayed']
            assert value['research_qualification']['evidence']['research']['artifact_id']==source.validation['id']
            assert reads and all(method!='eth_sendRawTransaction' for method in reads)
            count=len(reads);source.close()
            replay=client.post(base,headers=headers,json=body)
            assert replay.status_code==200 and replay.json()['replayed'],replay.text
            assert len(reads)==count
            assert client.post(base,headers=headers,json={**body,'entry_usdt':'13'}).status_code==409
            assert len(reads)==count
            assert client.get(base,headers=headers).json()[0]['plan_digest']==value['plan_digest']
            detail=client.get(base+'/'+value['id'],headers=headers)
            assert detail.status_code==200 and detail.headers['cache-control']=='no-store'
            stopped=client.post(base+'/'+value['id']+'/stop',headers=headers,json={'confirmation':'RETIRAR_VINCULO_DEX'})
            assert stopped.status_code==200 and stopped.json()['status']=='STOPPED',stopped.text
            assert not stopped.json()['automatic_execution']
            assert client.post('/real/vault/lock',headers=headers).status_code==200
            assert client.get(base,headers=headers).status_code==401
    source.close()


def test_lock_during_policy_rpc_cancels_binding_without_a_partial_gas_grant(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import select,func
    from app.portfolio.models import RealDexOperatorGrant,RealDexBotBinding
    from app.portfolio.vault import RealVault
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory,research=True)
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        deployment=ready(vault,account,now,meta,rpc);vault.lock();started=Event();release=Event()
        @contextmanager
        def factory():
            def read(method,params):
                started.set();assert release.wait(10)
                return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,spot_paper=source.paper,dex_rpc_factory=factory))
        with TestClient(app) as client,ThreadPoolExecutor() as pool:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            body={'request_id':str(uuid4()),'deployment_id':deployment['id'],'address':meta['owner'],'source_kind':'STRATEGY',
                'paper_session_id':source.session['id'],'research_validation_id':source.validation['id'],'entry_usdt':'12','budget_usdt':'60','exposure_usdt':'25',
                'loss_usdt':'5','wbnb_quota':'1','gas_budget_bnb':'0.002','confirmation':'VINCULAR_BOT_DEX'}
            pending=pool.submit(client.post,f'/real/accounts/{account}/dex-bots/bindings',headers=headers,json=body)
            try:
                assert started.wait(5)
                assert client.post('/real/vault/lock',headers=headers).status_code==200
            finally:release.set()
            assert pending.result(timeout=10).status_code==401
        vault=RealVault(vault.path);vault.unlock(PASSWORD)
        with vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexBotBinding))==0
            assert db.scalar(select(func.count()).select_from(RealDexOperatorGrant))==0
        vault.lock()
    source.close()
