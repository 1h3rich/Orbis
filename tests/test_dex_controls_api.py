"""Protected wallet → live EVM state → reviewed configure → receipt, no signer."""
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from tests.dex_evm_fixture import evm
from tests.test_wallet_funding_api import prove
from tests.test_dex_policy import policy


def test_private_control_api_reads_fresh_chain_state_and_confirms_configuration_without_starting_a_bot(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        reads = []
        @contextmanager
        def factory():
            def read(method, params):reads.append(method);return rpc(method, params)
            yield read
        app = FastAPI();app.include_router(create_real_router(tmp_path/'real.db', dex_rpc_factory=factory))
        api = TestClient(app);password = 'isolated DEX controls API password'
        token = api.post('/real/vault/create', json={'passphrase': password}).json()['token']
        headers = {'X-Orbis-Vault-Token': token}
        account = api.post('/real/accounts', headers=headers, json={'name': 'Wallet', 'base_currency': 'USDT'}).json()['id']
        wallet = Account.from_key('0x'+'11'*32);prove(api, headers, account, wallet)
        parent = f'/real/accounts/{account}/dex-deployments'
        deployment = api.post(parent, headers=headers, json={'request_id': str(uuid4()), 'address': wallet.address,
            'chain_id': '0x38', 'fee': 500, 'nonce': str(int(rpc('eth_getTransactionCount', [wallet.address, 'pending']), 16)),
            'gas': '3500000', 'gas_price': '1000000000', 'max_gas_bnb': '0.005'}).json()
        url = parent+'/'+deployment['id']
        assert api.post(url+'/claim', headers=headers, json={'address': wallet.address,
            'plan_digest': deployment['plan_digest'], 'confirmation': 'DESPLEGAR_EJECUTOR_DEX'}).json()['claim_granted']
        tx_hash = rpc('eth_sendTransaction', [deployment['transaction']]);receipt = rpc('eth_getTransactionReceipt', [tx_hash])
        block = rpc('eth_getBlockByNumber', ['finalized', False]);executor = receipt['contractAddress']
        assert api.post(url+'/hash', headers=headers, json={'tx_hash': tx_hash}).status_code == 200
        assert api.post(url+'/check', headers=headers, json={'evidence': {'chain_id': '0x38', 'tx_hash': tx_hash,
            'transaction': rpc('eth_getTransactionByHash', [tx_hash]), 'receipt': receipt, 'block': block, 'finalized': block,
            'pool': meta['pool'], 'runtime': rpc('eth_getCode', [executor, block['number']])}}).status_code == 200
        assert api.post('/real/vault/lock', headers=headers).status_code == 200
        headers = {'X-Orbis-Vault-Token': api.post('/real/vault/unlock', json={'passphrase': password}).json()['token']}
        path = f'/real/accounts/{account}/dex-controls'
        body = {'request_id': str(uuid4()), 'deployment_id': deployment['id'], 'address': wallet.address,
            'action': 'CONFIGURE', 'operator': meta['operator'], 'limits': policy(int(datetime.now(timezone.utc).timestamp())),
            'gas': '1000000', 'gas_price': '1000000000', 'max_gas_bnb': '0.002'}
        assert api.get(path).status_code == 401
        assert api.post(path, json=body).status_code == 401
        assert api.post(path, headers={**headers, 'Origin': 'https://outside.invalid'}, json=body).status_code == 403
        assert api.post(path, headers=headers, json=body).status_code == 409 and not reads
        prove(api, headers, account, wallet)
        assert api.post(path, headers=headers, json={**body, 'limits': {**body['limits'], 'max_entries': True}}).status_code == 422
        assert not reads
        assert api.post(path, headers=headers, json={**body, 'limits': {**body['limits'], 'expires_at': 2**64-1}}).status_code == 409
        prepared = api.post(path, headers=headers, json=body)
        assert prepared.status_code == 200, prepared.text
        assert prepared.headers['cache-control'] == 'no-store' and len(reads) > 0
        plan = prepared.json();detail = path+'/'+plan['id']
        assert plan['transaction']['to'] == executor and not plan['automatic_execution']
        claim = {'address': wallet.address, 'operator': meta['operator'], 'plan_digest': plan['plan_digest'], 'confirmation': 'CONFIGURAR_EJECUTOR_DEX'}
        assert api.post(detail+'/claim', headers=headers, json={**claim, 'confirmation': 'DETENER_EJECUTOR_DEX'}).status_code == 409
        granted = api.post(detail+'/claim', headers=headers, json=claim)
        assert granted.status_code == 200 and granted.json()['claim_granted'], granted.text
        before = len(reads)
        assert not api.post(detail+'/claim', headers=headers, json=claim).json()['claim_granted']
        assert len(reads) == before
        assert api.post(detail+'/recover', headers=headers, json={'address': wallet.address}).status_code == 409
        sent = rpc('eth_sendTransaction', [plan['transaction']]);receipt = rpc('eth_getTransactionReceipt', [sent])
        recovered = api.post(detail+'/recover', headers=headers, json={'address': wallet.address})
        assert recovered.status_code == 200 and recovered.json()['nonce_observation']['effect_matches'], recovered.text
        assert recovered.json()['receipt'] is None
        assert api.post(detail+'/hash', headers=headers, json={'tx_hash': sent}).status_code == 200
        block = rpc('eth_getBlockByNumber', ['finalized', False])
        checked = api.post(detail+'/check', headers=headers, json={'evidence': {'chain_id': '0x38', 'tx_hash': sent,
            'transaction': rpc('eth_getTransactionByHash', [sent]), 'receipt': receipt, 'block': block, 'finalized': block,
            'pool': meta['pool'], 'runtime': rpc('eth_getCode', [executor, block['number']])}})
        assert checked.status_code == 200 and checked.json()['receipt']['new_policy_id'] == '1', checked.text
        current = api.post(path+'/state', headers=headers, json={'deployment_id': deployment['id'], 'address': wallet.address})
        assert current.status_code == 200 and current.json()['policy_id'] == '1' and not current.json()['automatic_execution'], current.text
        history = api.get(path, headers=headers)
        assert history.status_code == 200 and not history.json()[0]['automatic_execution']
        for secret in (wallet.address, meta['operator'], executor, sent):assert secret.lower() not in history.text.lower()
        assert api.post('/real/vault/lock', headers=headers).status_code == 200
        assert api.get(path, headers=headers).status_code == 401


def test_slow_public_rpc_does_not_prevent_locking_and_cannot_commit_a_plan_after_session_revocation(tmp_path):
    from tests.test_dex_controls import confirmed_deployment
    from tests.test_dex_deployments import setup, PASSWORD
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = confirmed_deployment(vault, account, now, meta, rpc);vault.lock()
        entered, release = Event(), Event()
        @contextmanager
        def factory():
            def read(method, params):
                if method == 'eth_chainId':
                    entered.set();assert release.wait(15)
                return rpc(method, params)
            yield read
        app = FastAPI();app.include_router(create_real_router(tmp_path/'real.db', dex_rpc_factory=factory))
        api = TestClient(app)
        token = api.post('/real/vault/unlock', json={'passphrase': PASSWORD}).json()['token']
        headers = {'X-Orbis-Vault-Token': token};wallet = Account.from_key('0x'+'11'*32);prove(api, headers, account, wallet)
        path = f'/real/accounts/{account}/dex-controls'
        body = {'request_id': str(uuid4()), 'deployment_id': deployment['id'], 'address': wallet.address,
            'action': 'CONFIGURE', 'operator': meta['operator'], 'limits': policy(int(datetime.now(timezone.utc).timestamp())),
            'gas': '1000000', 'gas_price': '1000000000', 'max_gas_bnb': '0.002'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading = pool.submit(api.post, path, headers=headers, json=body)
            try:
                assert entered.wait(5)
                locking = pool.submit(api.post, '/real/vault/lock', headers=headers)
                assert locking.result(timeout=3).status_code == 200
                assert not release.is_set() and not reading.done()
            finally:release.set()
            assert reading.result(timeout=10).status_code == 401
        token = api.post('/real/vault/unlock', json={'passphrase': PASSWORD}).json()['token']
        headers = {'X-Orbis-Vault-Token': token}
        assert api.get(path, headers=headers).json() == []
        api.post('/real/vault/lock', headers=headers)
