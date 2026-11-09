"""Wallet approval/revocation reaches the actual EVM via the private API."""
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.live_execution.dex_policy import stop_transaction
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup, PASSWORD
from tests.test_dex_allowance_journal import ready
from tests.test_wallet_funding_api import prove


def test_api_approves_finite_executor_authority_then_can_revoke_it_after_stop(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = ready(vault, account, now, meta, rpc);vault.lock()
        @contextmanager
        def factory():yield rpc
        app = FastAPI();app.include_router(create_real_router(tmp_path/'real.db', dex_rpc_factory=factory));api = TestClient(app)
        headers = {'X-Orbis-Vault-Token': api.post('/real/vault/unlock', json={'passphrase': PASSWORD}).json()['token']}
        wallet = Account.from_key('0x'+'11'*32);prove(api, headers, account, wallet)
        path = f'/real/accounts/{account}/dex-controls'
        base = {'deployment_id': deployment['id'], 'address': wallet.address, 'gas': '100000', 'gas_price': '1000000000', 'max_gas_bnb': '0.001'}
        body = {**base, 'request_id': str(uuid4()), 'action': 'APPROVE', 'token': 'USDT', 'allowance_units': '1'}
        for change in ({'token': 'BNB'}, {'allowance_units': 1}, {'spender': meta['router']}, {'operator': meta['operator']}):
            assert api.post(path, headers=headers, json={**body, **change}).status_code == 422
        assert api.post(path, headers=headers, json={**body, 'allowance_units': str(2**256-1)}).status_code == 409
        prepared = api.post(path, headers=headers, json=body)
        assert prepared.status_code == 200, prepared.text
        plan = prepared.json();detail = path+'/'+plan['id']
        claim = {'address': wallet.address, 'plan_digest': plan['plan_digest'], 'confirmation': 'AUTORIZAR_TOKENS_DEX'}
        granted = api.post(detail+'/claim', headers=headers, json=claim)
        assert granted.status_code == 200 and granted.json()['claim_granted'], granted.text
        sent = rpc('eth_sendTransaction', [plan['transaction']]);receipt = rpc('eth_getTransactionReceipt', [sent])
        observed = api.post(detail+'/recover', headers=headers, json={'address': wallet.address})
        assert observed.status_code == 200 and observed.json()['nonce_observation']['effect_matches'], observed.text
        assert api.post(detail+'/hash', headers=headers, json={'tx_hash': sent}).status_code == 200
        block = rpc('eth_getBlockByNumber', ['finalized', False])
        from app.live_execution.dex_artifact import executor_address
        executor = executor_address(wallet.address, int(deployment['nonce']))
        evidence = {'chain_id': '0x38', 'tx_hash': sent, 'transaction': rpc('eth_getTransactionByHash', [sent]), 'receipt': receipt,
            'block': block, 'finalized': block, 'pool': meta['pool'], 'runtime': rpc('eth_getCode', [executor, block['number']])}
        checked = api.post(detail+'/check', headers=headers, json={'evidence': evidence})
        assert checked.status_code == 200 and checked.json()['receipt']['approved_units'] == '1', checked.text
        assert not checked.json()['automatic_execution']
        assert api.post(path, headers=headers, json=body).json()['plan_digest'] == plan['plan_digest']
        assert api.post(path, headers=headers, json={**body, 'request_id': str(uuid4())}).status_code == 409
        nonce = int(rpc('eth_getTransactionCount', [wallet.address, 'pending']), 16)
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [stop_transaction(wallet.address, int(deployment['nonce']),
            nonce=nonce, gas=1000000, gas_price=10**9)])])['status'] == '0x1'
        revoked = api.post(path, headers=headers, json={**base, 'request_id': str(uuid4()), 'action': 'REVOKE', 'token': 'USDT', 'allowance_units': '0'})
        assert revoked.status_code == 200, revoked.text
        r = revoked.json();detail = path+'/'+r['id']
        grant = api.post(detail+'/claim', headers=headers, json={'address': wallet.address, 'plan_digest': r['plan_digest'], 'confirmation': 'REVOCAR_TOKENS_DEX'})
        assert grant.status_code == 200 and grant.json()['claim_granted'], grant.text
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [r['transaction']])])['status'] == '0x1'
        current = api.post(path+'/state', headers=headers, json={'deployment_id': deployment['id'], 'address': wallet.address}).json()
        assert current['allowance_usdt'] == '0' and current['operator_stopped'] and not current['automatic_execution']
        assert api.post('/real/vault/lock', headers=headers).status_code == 200
