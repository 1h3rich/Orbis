"""Prueba local de control EOA; firma ficticia, sin wallet ni red real."""

from eth_account import Account
from eth_account.messages import encode_defunct
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import RealAccess, create_real_router
from app.portfolio.repository import create_account
from app.portfolio.wallet_proof import WalletProofError


def test_wallet_proof_is_one_use_session_scoped_and_not_spending_authority(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={
        "passphrase": "wallet proof test password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account_id = client.post("/real/accounts", json={"name": "Wallet local"},
                             headers=headers).json()["id"]
    wallet = Account.create()
    path = f"/real/accounts/{account_id}/wallet-proof"
    body = {"address": wallet.address, "chain_id": "0x1"}
    assert client.post(path + "/start", json=body).status_code == 401
    assert client.post(path + "/start", json=body, headers={
        **headers, "Origin": "https://other.example"}).status_code == 403
    started = client.post(path + "/start", json=body, headers=headers)
    assert started.status_code == 200
    assert started.headers["cache-control"] == "no-store"
    challenge = started.json()
    assert challenge["purpose"] == "SESSION_EOA_CONTROL_ONLY"
    assert account_id in challenge["message"]
    assert wallet.address.lower() in challenge["message"]
    assert "NO autoriza pagos" in challenge["message"]
    signature = Account.sign_message(encode_defunct(text=challenge["message"]),
                                     wallet.key).signature.hex()
    confirmed = client.post(path + "/confirm", json={
        **body, "nonce": challenge["nonce"], "signature": "0x" + signature,
    }, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {
        "verified": True, "scope": "THIS_UNLOCKED_SESSION_ONLY",
        "wallet_type": "EOA_EIP191", "spending_authorized": False,
        "balances_reconciled": False,
    }
    assert client.post(path + "/confirm", json={
        **body, "nonce": challenge["nonce"], "signature": "0x" + signature,
    }, headers=headers).status_code == 422
    assert wallet.address not in str(client.get("/real/accounts", headers=headers).json())
    assert wallet.address.lower() not in str(client.get("/real/accounts", headers=headers).json()).lower()
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    assert client.post(path + "/confirm", json={
        **body, "nonce": challenge["nonce"], "signature": "0x" + signature,
    }, headers=headers).status_code == 401


def test_wallet_proof_rejects_wrong_account_chain_and_signature(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={
        "passphrase": "wallet proof mismatch passphrase"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    accounts = [client.post("/real/accounts", json={"name": name}, headers=headers).json()["id"]
                for name in ("Wallet A", "Wallet B")]
    wallet, other = Account.create(), Account.create()
    path = f"/real/accounts/{accounts[0]}/wallet-proof"
    body = {"address": wallet.address, "chain_id": "0x1"}
    started = client.post(path + "/start", json=body, headers=headers).json()
    wrong = Account.sign_message(encode_defunct(text=started["message"]),
                                 other.key).signature.hex()
    assert client.post(path + "/confirm", json={
        **body, "nonce": started["nonce"], "signature": "0x" + wrong,
    }, headers=headers).status_code == 422
    signed = Account.sign_message(encode_defunct(text=started["message"]),
                                  wallet.key).signature.hex()
    assert client.post(path + "/confirm", json={
        **body, "nonce": started["nonce"], "signature": "0x" + signed,
    }, headers=headers).status_code == 422  # bad proof consumed the nonce
    next_started = client.post(path + "/start", json=body, headers=headers).json()
    next_signature = Account.sign_message(encode_defunct(text=next_started["message"]),
                                          wallet.key).signature.hex()
    assert client.post(f"/real/accounts/{accounts[1]}/wallet-proof/confirm", json={
        **body, "nonce": next_started["nonce"], "signature": "0x" + next_signature,
    }, headers=headers).status_code == 422
    assert client.post(path + "/start", json={
        "address": wallet.address, "chain_id": "invalid"}, headers=headers).status_code == 422


def test_wallet_proof_expires_replaces_old_nonce_and_is_cleared_on_unlock(tmp_path):
    access = RealAccess(tmp_path / "vault.db")
    token = access.unlock("ephemeral proof passphrase", create=True)
    with access.vault.session() as db:
        account_id = create_account(db, "Principal").id
    wallet = Account.create()
    args = (token, account_id, wallet.address, "0x1", "http://127.0.0.1:8000")
    first = access.wallet_proof_start(*args)
    assert wallet.address.lower() not in repr(access._wallet_challenges).lower()
    second = access.wallet_proof_start(*args)
    assert first["nonce"] not in access._wallet_challenges
    assert second["nonce"] in access._wallet_challenges
    signed = Account.sign_message(encode_defunct(text=second["message"]), wallet.key).signature.hex()
    pending = access._wallet_challenges[second["nonce"]]
    access._wallet_challenges[second["nonce"]] = replace(
        pending, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(WalletProofError, match="firma"):
        access.wallet_proof_confirm(token, account_id, second["nonce"], wallet.address,
                                    "0x1", "0x" + signed)
    third = access.wallet_proof_start(*args)
    signed = Account.sign_message(encode_defunct(text=third["message"]), wallet.key).signature.hex()
    access.wallet_proof_confirm(token, account_id, third["nonce"], wallet.address,
                                "0x1", "0x" + signed)
    assert len(access._wallet_verified) == 1
    access.lock()
    assert access._wallet_verified == set()
    assert access._wallet_challenges == {}
    new_token = access.unlock("ephemeral proof passphrase")
    with pytest.raises(WalletProofError, match="inexistente"):
        access.wallet_proof_confirm(new_token, account_id, third["nonce"], wallet.address,
                                    "0x1", "0x" + signed)
    access.lock()
