"""Asociación EOA persistente sin custodiar direcciones ni autorizar órdenes."""

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.vault import SCHEMA_VERSION
from app.portfolio.api import create_real_router
from app.portfolio.vault import RealVault


PASSWORD = "wallet link vault password"


def _client(path):
    app = FastAPI()
    app.include_router(create_real_router(path))
    return TestClient(app)


def _proof(client, headers, account_id, wallet, chain="0x1"):
    base = f"/real/accounts/{account_id}/wallet-proof"
    body = {"address": wallet.address, "chain_id": chain}
    challenge = client.post(base + "/start", json=body, headers=headers)
    assert challenge.status_code == 200, challenge.text
    signed = Account.sign_message(encode_defunct(text=challenge.json()["message"]),
                                  wallet.key).signature.hex()
    response = client.post(base + "/confirm", json={
        **body, "nonce": challenge.json()["nonce"], "signature": "0x" + signed,
    }, headers=headers)
    assert response.status_code == 200, response.text


def test_wallet_link_requires_fresh_proof_and_survives_reopen_without_address(tmp_path):
    path = tmp_path / "real.db"
    client = _client(path)
    created = client.post("/real/vault/create", json={"passphrase": PASSWORD}).json()
    headers = {"X-Orbis-Vault-Token": created["token"]}
    a = client.post("/real/accounts", json={"name": "Wallet A"}, headers=headers).json()["id"]
    b = client.post("/real/accounts", json={"name": "Wallet B"}, headers=headers).json()["id"]
    wallet, other = Account.create(), Account.create()
    base = f"/real/accounts/{a}/wallet-links"
    payload = {"address": wallet.address, "chain_id": "0x1",
               "alias": "Principal", "confirmation": "VINCULAR"}
    assert client.post(base, json=payload, headers=headers).status_code == 422
    inspect_path = base + "/inspect"
    assert client.post(inspect_path, json={"address": wallet.address, "chain_id": "0x1"},
                       headers=headers).status_code == 422
    _proof(client, headers, a, wallet)
    initial = client.post(inspect_path, json={"address": wallet.address, "chain_id": "0x1"},
                          headers=headers)
    assert initial.status_code == 200 and initial.json()["status"] == "UNLINKED"
    assert initial.json()["control_verified_this_session"] is True
    assert "address" not in initial.json() and "digest" not in initial.json()
    assert client.post(inspect_path, json={"address": other.address, "chain_id": "0x1"},
                       headers=headers).status_code == 422
    assert client.post(base, json={**payload, "address": other.address}, headers=headers).status_code == 422
    assert client.post(f"/real/accounts/{b}/wallet-links", json=payload, headers=headers).status_code == 422
    invalid = client.post(base, json={**payload, "alias": wallet.address}, headers=headers)
    assert invalid.status_code == 422
    saved = client.post(base, json=payload, headers=headers)
    assert saved.status_code == 200, saved.text
    link_id = saved.json()["id"]
    assert saved.json()["spending_authorized"] is False
    assert saved.json()["balances_reconciled"] is False
    assert "address" not in saved.json() and "digest" not in saved.json()
    current = client.post(inspect_path, json={"address": wallet.address, "chain_id": "0x1"},
                          headers=headers).json()
    assert current["status"] == "CURRENT_ACCOUNT" and current["alias"] == "Principal"
    assert current["spending_authorized"] is False
    assert client.post(base, json=payload, headers=headers).json()["id"] == link_id
    inspector = RealVault(path)
    inspector.unlock(PASSWORD)
    with inspector.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT alias, chain_id, address_digest FROM real_wallet_links WHERE id = ?",
            (link_id,),
        ).one()
        assert row.alias == "Principal" and row.chain_id == "0x1"
        assert len(row.address_digest) == 64
        assert wallet.address.lower() not in str(row).lower()
    inspector.lock()
    assert client.post(f"/real/accounts/{b}/wallet-links/{link_id}/unlink", json={
        "confirmation": "DESVINCULAR"}, headers=headers).status_code == 404
    assert wallet.address.lower().encode() not in path.read_bytes().lower()
    assert other.address.lower().encode() not in path.read_bytes().lower()
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    reopened = _client(path)
    new_token = reopened.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    new_headers = {"X-Orbis-Vault-Token": new_token}
    listed = reopened.get(base, headers=new_headers)
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert [row["id"] for row in listed.json()] == [link_id]
    assert wallet.address.lower() not in str(listed.json()).lower()
    assert reopened.post(base, json=payload, headers=new_headers).status_code == 422
    assert reopened.post(inspect_path, json={"address": wallet.address, "chain_id": "0x1"},
                         headers=new_headers).status_code == 422
    _proof(reopened, new_headers, b, wallet)
    other_link = reopened.post(f"/real/accounts/{b}/wallet-links/inspect", json={
        "address": wallet.address, "chain_id": "0x1"}, headers=new_headers)
    assert other_link.status_code == 200
    assert other_link.json()["status"] == "OTHER_ACCOUNT"
    assert other_link.json()["linked_account_name"] == "Wallet A"
    assert "address" not in other_link.json() and "digest" not in other_link.json()
    assert reopened.post(f"/real/accounts/{b}/wallet-links", json=payload,
                         headers=new_headers).status_code == 409
    assert reopened.post(base + f"/{link_id}/unlink", json={
        "confirmation": "NO"}, headers=new_headers).status_code == 422
    assert reopened.post(base + f"/{link_id}/unlink", json={
        "confirmation": "DESVINCULAR"}, headers=new_headers).status_code == 200
    assert reopened.get(base, headers=new_headers).json() == []
    assert reopened.post(base, json=payload, headers=new_headers).status_code == 422


def test_v18_wallet_link_migration_keeps_portfolio_and_encrypted_preimage(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock(PASSWORD, create=True)
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE real_wallet_links")
        connection.exec_driver_sql("DROP TABLE real_wallet_identity_salt")
        connection.exec_driver_sql("PRAGMA user_version = 18")
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_wallet_links").scalar_one() == 0
    backup = vault.last_migration_backup
    assert backup is not None and backup.is_file()
    assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert RealVault._verify_portfolio_file(backup, PASSWORD, expected_version=18) == (0, 0)
    vault.lock()
