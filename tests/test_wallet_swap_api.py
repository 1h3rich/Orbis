"""API DEX: sesión REAL + prueba EOA, sin credenciales de exchange."""

from uuid import uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from tests.test_wallet_funding_api import prove


def test_swap_requires_same_real_session_proof_and_explicit_single_claim_without_mexc(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db"))
    client = TestClient(app)
    password = "isolated wallet swap API password"
    token = client.post("/real/vault/create", json={"passphrase": password}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", headers=headers, json={"name": "Wallet", "base_currency": "USDT"}).json()["id"]
    wallet = Account.create()
    path = f"/real/accounts/{account}/wallet-swaps"
    body = {"request_id": str(uuid4()), "address": wallet.address, "chain_id": "0x38", "action": "SWAP", "token_in": "USDT",
            "amount": "1", "quote_units": "1000000000000000", "fee": 500, "slippage_bps": 50,
            "nonce": "3", "gas": "200000", "gas_price": "1000000000", "max_gas_bnb": "0.001"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers={**headers, "Origin": "https://outside.invalid"}, json=body).status_code == 403
    assert client.post(path, headers=headers, json=body).status_code == 409
    prove(client, headers, account, wallet)
    assert client.post(path, headers=headers, json={**body, "chain_id": "0x1"}).status_code == 422
    assert client.post(path, headers=headers, json={**body, "calldata": "0x"}).status_code == 422
    prepared = client.post(path, headers=headers, json=body)
    assert prepared.status_code == 200, prepared.text
    assert prepared.headers["cache-control"] == "no-store"
    plan = prepared.json()
    claim = {"address": wallet.address, "plan_digest": plan["plan_digest"], "confirmation": "SOLICITAR_SWAP_WALLET"}
    grant = client.post(path+"/"+plan["id"]+"/claim", headers=headers, json=claim)
    assert grant.status_code == 200 and grant.json()["claim_granted"], grant.text
    assert not client.post(path+"/"+plan["id"]+"/claim", headers=headers, json=claim).json()["claim_granted"]
    result = client.post(path+"/"+plan["id"]+"/hash", headers=headers, json={"tx_hash": "0x"+"aa"*32})
    assert result.status_code == 200 and result.json()["status"] == "HASH_REPORTED"
    history = client.get(path, headers=headers)
    assert history.headers["cache-control"] == "no-store" and wallet.address.lower() not in history.text
    assert "transaction" not in history.text
    snapshot = client.get(f"/real/accounts/{account}/snapshot", headers=headers).json()
    assert snapshot["cash"] == {} and snapshot["positions"] == []
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    assert client.get(path, headers=headers).status_code == 401
