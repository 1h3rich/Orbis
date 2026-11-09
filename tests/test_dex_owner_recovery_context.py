"""A stale recovered-code anchor must reject a different signed fork receipt atomically."""
from contextlib import contextmanager
from datetime import datetime,timezone
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from app.portfolio import dex_deployments as journal
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup,prepare,PASSWORD
from tests.test_wallet_funding_api import prove


def test_contradictory_recovery_receipt_does_not_bind_an_original_hash(tmp_path):
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        nonce=int(rpc('eth_getTransactionCount',[meta['owner'],'pending']),16)
        plan=prepare(vault,account,now,wallet_address=meta['owner'],nonce=str(nonce))
        with vault.session() as db:journal.claim_deployment(db,plan['id'],account_id=account,wallet_address=meta['owner'],plan_digest=plan['plan_digest'],now=now)
        snapshot=rpc('evm_snapshot',[])
        original=rpc('eth_sendTransaction',[plan['transaction']]);receipt=rpc('eth_getTransactionReceipt',[original])
        block=rpc('eth_getBlockByNumber',['finalized',False]);executor=receipt['contractAddress']
        evidence={'chain_id':'0x38','block':block,'pool':meta['pool'],'runtime':rpc('eth_getCode',[executor,block['number']]),
            'owner_nonce':rpc('eth_getTransactionCount',[meta['owner'],block['number']]),'contract_address':executor}
        with vault.session() as db:journal.recover_deployment(db,plan['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=datetime.now(timezone.utc))
        assert rpc('evm_revert',[snapshot]);rpc('evm_mine',[])
        altered={**plan['transaction'],'gasPrice':hex(900000000)}
        fork_hash=rpc('eth_sendTransaction',[altered]);vault.lock()
        @contextmanager
        def factory():yield rpc
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            prefix=f'/real/accounts/{account}/dex-deployments'
            result=client.post(f'{prefix}/{plan["id"]}/observe',headers=headers,json={'address':meta['owner'],'tx_hash':fork_hash})
            assert result.status_code==409,result.text
            state=client.get(prefix,headers=headers).json()[0]
            assert state['status']=='CODE_OBSERVED',state
            assert client.post(f'{prefix}/{plan["id"]}/hash',headers=headers,json={'tx_hash':original}).status_code==200


def test_control_recovery_rejects_a_conflicting_anchor_without_persisting_the_pasted_hash(tmp_path):
    from app.live_execution.dex_state import read_executor_state
    from app.portfolio import dex_controls as controls
    from tests.test_dex_controls import prepared_control
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        deployment,plan,snapshot,args=prepared_control(vault,account,now,meta,rpc,action='STOP')
        with vault.session() as db:controls.claim_control(db,plan['id'],**args)
        before=rpc('evm_snapshot',[])
        original=rpc('eth_sendTransaction',[plan['transaction']])
        at=datetime.now(timezone.utc)
        observed=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=at,block_tag='finalized')
        with vault.session() as db:controls.recover_control(db,plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=observed,now=at)
        assert rpc('evm_revert',[before]);rpc('evm_mine',[])
        changed=rpc('eth_sendTransaction',[{**plan['transaction'],'gasPrice':hex(900000000)}]);vault.lock()
        @contextmanager
        def factory():yield rpc
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            prefix=f'/real/accounts/{account}/dex-controls'
            result=client.post(f'{prefix}/{plan["id"]}/observe',headers=headers,json={'address':meta['owner'],'tx_hash':changed})
            assert result.status_code==409,result.text
            state=client.get(prefix,headers=headers).json()[0]
            assert state['status']=='NONCE_OBSERVED' and state['receipt'] is None
            assert client.post(f'{prefix}/{plan["id"]}/hash',headers=headers,json={'tx_hash':original}).status_code==200
