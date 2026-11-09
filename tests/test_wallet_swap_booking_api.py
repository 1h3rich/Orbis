"""Contabilizar requiere la sesión REAL, EOA original y confirmación separada."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event, create_account
from app.portfolio.vault import RealVault
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.wallet_swap import claim_swap, report_swap, check_swap
from tests.test_wallet_funding_api import prove
from tests.test_wallet_swap import prepare, evidence, PASSWORD


def setup(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path); vault.unlock(PASSWORD, create=True)
    wallet, now = Account.create(), datetime.now(timezone.utc)
    with vault.session() as db:
        account = create_account(db, "Wallet", "USDT").id
    with vault.session() as db:
        save_wallet_link(db, account, wallet.address, "0x38", "Wallet de prueba"); db.commit()
    for item in [EventInput("CONTRIBUTION", "USDT", now-timedelta(days=1), amount=Decimal("100")),
                 EventInput("CONTRIBUTION", "BNB", now-timedelta(days=1), amount=Decimal("0.01"))]:
        with vault.session() as db:
            append_event(db, account, str(uuid4()), item)
    plan = prepare(vault, account, now, wallet_address=wallet.address)
    with vault.session() as db:
        claim_swap(db, plan["id"], account_id=account, wallet_address=wallet.address, plan_digest=plan["plan_digest"], now=now)
    proof = evidence(plan, now)
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=proof["tx_hash"], now=now)
    with vault.session() as db:
        check_swap(db, plan["id"], account_id=account, evidence=proof, now=now)
    vault.lock()
    app = FastAPI(); app.include_router(create_real_router(path))
    client = TestClient(app)
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    return client, headers, account, wallet, plan


def test_reading_receipt_never_books_and_explicit_authenticated_confirmation_is_idempotent(tmp_path):
    client, headers, account, wallet, plan = setup(tmp_path)
    base = f"/real/accounts/{account}/wallet-swaps/{plan['id']}"
    body = {"address": wallet.address, "gas_event_ids": []}
    assert client.post(base+"/booking-preview", json=body).status_code == 401
    assert client.post(base+"/booking-preview", headers=headers, json=body).status_code == 409
    prove(client, headers, account, wallet)
    proposed = client.post(base+"/booking-preview", headers=headers, json=body)
    assert proposed.status_code == 200 and proposed.headers["cache-control"] == "no-store", proposed.text
    assert not proposed.json()["booked"]
    assert len(client.get(f"/real/accounts/{account}/events", headers=headers).json()) == 2
    command = {**body, "expected_revision": proposed.json()["revision"], "preview_digest": proposed.json()["preview_digest"],
               "confirmation": "REGISTRAR_ACCION_DEX"}
    assert client.post(base+"/book", headers=headers, json={**command, "confirmation": ""}).status_code == 422
    assert client.post(base+"/book", headers={**headers, "Origin": "https://outside.invalid"}, json=command).status_code == 403
    recorded = client.post(base+"/book", headers=headers, json=command)
    assert recorded.status_code == 200 and recorded.json()["booked"], recorded.text
    assert client.post(base+"/book", headers=headers, json=command).json()["events_created"] == 0
    snapshot = client.get(f"/real/accounts/{account}/snapshot", headers=headers).json()
    assert Decimal(snapshot["cash"]["USDT"]) == Decimal("87.654321098765432109")
    assert Decimal(snapshot["cash"]["BNB"]) == Decimal("0.0099")
    assert not snapshot["withdrawals"]
    assert len(client.get(f"/real/accounts/{account}/events", headers=headers).json()) == 4
    client.post("/real/vault/lock", headers=headers)
    assert client.post(base+"/book", headers=headers, json=command).status_code == 401
