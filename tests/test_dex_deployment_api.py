"""Deployment journal API requires REAL access and proof of the chosen wallet."""
from uuid import uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from tests.test_wallet_funding_api import prove
from tests.dex_evm_fixture import evm


def test_deployment_api_is_private_and_single_use_without_granting_trading_or_booking_money(tmp_path):
    app = FastAPI(); app.include_router(create_real_router(tmp_path/'real.db'))
    client = TestClient(app)
    password = 'isolated DEX deployment API password'
    token = client.post('/real/vault/create', json={'passphrase': password}).json()['token']
    headers = {'X-Orbis-Vault-Token': token}
    account = client.post('/real/accounts', headers=headers, json={'name': 'Wallet', 'base_currency': 'USDT'}).json()['id']
    wallet = Account.create(); path = f'/real/accounts/{account}/dex-deployments'
    body = {'request_id': str(uuid4()), 'address': wallet.address, 'chain_id': '0x38', 'fee': 500, 'nonce': '3',
        'gas': '3500000', 'gas_price': '1000000000', 'max_gas_bnb': '0.005'}
    assert client.get(path).status_code == 401
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers={**headers, 'Origin': 'https://outside.invalid'}, json=body).status_code == 403
    assert client.post(path, headers=headers, json=body).status_code == 409
    prove(client, headers, account, wallet)
    for invalid in [{'chain_id': '0x1'}, {'fee': True}, {'fee': 200}, {'calldata': '0x'}, {'gas': 3500000}]:
        assert client.post(path, headers=headers, json={**body, **invalid}).status_code == 422
    prepared = client.post(path, headers=headers, json=body)
    assert prepared.status_code == 200, prepared.text
    assert prepared.headers['cache-control'] == 'no-store'
    plan = prepared.json(); detail = path+'/'+plan['id']
    assert plan['transaction']['data'].startswith('0x') and not plan['executor_verified']
    claim = {'address': wallet.address, 'plan_digest': plan['plan_digest'], 'confirmation': 'DESPLEGAR_EJECUTOR_DEX'}
    assert client.post(detail+'/claim', headers=headers, json={**claim, 'confirmation': 'yes'}).status_code == 422
    granted = client.post(detail+'/claim', headers=headers, json=claim)
    assert granted.status_code == 200 and granted.json()['claim_granted'], granted.text
    assert not client.post(detail+'/claim', headers=headers, json=claim).json()['claim_granted']
    report = client.post(detail+'/hash', headers=headers, json={'tx_hash': '0x'+'ab'*32})
    assert report.status_code == 200 and not report.json()['executor_verified']
    assert client.post(detail+'/check', headers=headers, json={'evidence': {}}).status_code == 409
    assert client.post(detail+'/recover', headers=headers, json={'address': wallet.address, 'evidence': {}}).status_code == 409
    history = client.get(path, headers=headers)
    assert history.status_code == 200 and history.headers['cache-control'] == 'no-store'
    assert wallet.address.lower() not in history.text.lower() and 'transaction"' not in history.text
    assert not history.json()[0]['automatic_execution']
    snapshot = client.get(f'/real/accounts/{account}/snapshot', headers=headers).json()
    assert snapshot['cash'] == {} and snapshot['positions'] == []
    assert client.post('/real/vault/lock', headers=headers).status_code == 200
    assert client.get(path, headers=headers).status_code == 401


def test_api_confirms_a_genuine_evm_creation_then_recovers_the_same_private_history_after_unlock(tmp_path):
    app = FastAPI(); app.include_router(create_real_router(tmp_path/'real.db'))
    client = TestClient(app); password = 'isolated signed deployment API password'
    token = client.post('/real/vault/create', json={'passphrase': password}).json()['token']
    headers = {'X-Orbis-Vault-Token': token}
    account = client.post('/real/accounts', headers=headers, json={'name': 'Wallet', 'base_currency': 'USDT'}).json()['id']
    with evm(tmp_path/'evm') as (meta, rpc):
        wallet = Account.from_key('0x'+'11'*32)  # Known, unfunded outside the isolated Ganache fixture.
        assert wallet.address == meta['owner']
        prove(client, headers, account, wallet)
        path = f'/real/accounts/{account}/dex-deployments'
        nonce = str(int(rpc('eth_getTransactionCount', [wallet.address, 'pending']), 16))
        prepared = client.post(path, headers=headers, json={'request_id': str(uuid4()), 'address': wallet.address,
            'chain_id': '0x38', 'fee': 500, 'nonce': nonce, 'gas': '3500000', 'gas_price': '1000000000', 'max_gas_bnb': '0.005'})
        assert prepared.status_code == 200, prepared.text
        plan = prepared.json(); detail = path+'/'+plan['id']
        claimed = client.post(detail+'/claim', headers=headers, json={'address': wallet.address,
            'plan_digest': plan['plan_digest'], 'confirmation': 'DESPLEGAR_EJECUTOR_DEX'})
        assert claimed.status_code == 200 and claimed.json()['claim_granted']
        tx_hash = rpc('eth_sendTransaction', [plan['transaction']])
        receipt = rpc('eth_getTransactionReceipt', [tx_hash]); block = rpc('eth_getBlockByNumber', ['finalized', False])
        code = rpc('eth_getCode', [receipt['contractAddress'], block['number']])
        recovered = client.post(detail+'/recover', headers=headers, json={'address': wallet.address, 'evidence': {
            'chain_id': '0x38', 'block': block, 'pool': meta['pool'], 'runtime': code, 'contract_address': receipt['contractAddress'],
            'owner_nonce': rpc('eth_getTransactionCount', [wallet.address, block['number']])}})
        assert recovered.status_code == 200 and recovered.json()['executor_verified'], recovered.text
        assert recovered.json()['receipt'] is None
        assert client.post(detail+'/hash', headers=headers, json={'tx_hash': tx_hash}).status_code == 200
        checked = client.post(detail+'/check', headers=headers, json={'evidence': {
            'chain_id': '0x38', 'tx_hash': tx_hash, 'transaction': rpc('eth_getTransactionByHash', [tx_hash]),
            'receipt': receipt, 'block': block, 'finalized': block, 'pool': meta['pool'], 'runtime': code}})
        assert checked.status_code == 200 and checked.json()['status'] == 'FINALIZED', checked.text
        assert not checked.json()['automatic_execution']
        assert client.post('/real/vault/lock', headers=headers).status_code == 200
        unlocked = client.post('/real/vault/unlock', json={'passphrase': password})
        assert unlocked.status_code == 200, unlocked.text
        history = client.get(path, headers={'X-Orbis-Vault-Token': unlocked.json()['token']})
        assert history.status_code == 200 and history.json()[0] == checked.json()
        for private in (wallet.address, tx_hash, receipt['contractAddress']):assert private.lower() not in history.text.lower()
