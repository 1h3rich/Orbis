"""Servidor aislado para vincular un ingreso observado a un traslado REAL."""

import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

from app.portfolio.api import create_real_router
from app.live_execution.mexc_uid_guard import claim_mexc_uid_in_transaction
from app.portfolio.domain import EventInput
from app.portfolio.mexc_transfers import save_transfer_snapshot
from app.portfolio.models import RealMexcAccountLink
from app.portfolio.repository import (
    append_event, create_account, transfer_cash_between_accounts,
)
from app.portfolio.vault import RealVault
from app.web.mount import register_ui


PATH = Path(os.environ["ORBIS_REAL_VAULT_PATH"])
PASSWORD = "browser transfer match password"
WHEN = datetime.now(timezone.utc) - timedelta(minutes=15)
API_KEY = "browser-transfer-view-key"
SECRET_KEY = "browser-transfer-view-secret"
ACCOUNT_REF = hmac.new(SECRET_KEY.encode(), API_KEY.encode(), hashlib.sha256).hexdigest()
TX_HASH = "0x" + "ab" * 32
WITHDRAW_HASH = "0x" + "cd" * 32
MEXC_DESTINATION = "0x" + "33" * 20
TOKEN_CONTRACT = "0x" + "44" * 20


class FakeMexc:
    def __init__(self, api_key, secret_key):
        if (api_key, secret_key) != (API_KEY, SECRET_KEY):
            raise ValueError("Credencial de prueba inválida")

    def account_reference(self):
        return ACCOUNT_REF

    def account_uid(self):
        return "123456789"

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(), "balances": []}

    def transfers_recent(self, *, days, include_source_ids=False):
        assert days == 7 and include_source_ids is True
        now = datetime.now(timezone.utc)
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
                "start_ms": int((now - timedelta(days=days)).timestamp() * 1000),
                "end_ms": int(now.timestamp() * 1000), "observed_at": now.isoformat(),
                "requests": 2,
                "deposits": [{"direction": "deposit", "asset": "USDT", "network": "ERC20",
                              "amount": "25", "fee": "0", "status": 5,
                              "time_ms": int(WHEN.timestamp() * 1000),
                              "source_identity": TX_HASH,
                              "destination_identity": MEXC_DESTINATION,
                              "token_contract": TOKEN_CONTRACT}],
                "withdrawals": [{"direction": "withdrawal", "asset": "USDT", "network": "ERC20",
                                 "amount": "25.1", "fee": "0.1", "status": 7,
                                 "time_ms": int(WHEN.timestamp() * 1000),
                                 "transfer_type": 0, "source_identity": "test-withdrawal-id",
                                 "chain_identity": WITHDRAW_HASH,
                                 "token_contract": TOKEN_CONTRACT,
                                 "destination_identity": "0x" + "11" * 20}]}


def seed():
    if PATH.exists():
        return
    vault = RealVault(PATH)
    vault.unlock(PASSWORD, create=True)
    with vault.session() as db:
        wallet = create_account(db, "Wallet prueba", "USDT").id
        mexc = create_account(db, "MEXC prueba", "USDT").id
        append_event(db, wallet, str(uuid4()), EventInput(
            "CONTRIBUTION", "USDT", WHEN - timedelta(days=2), amount=Decimal("100")))
        transfer_cash_between_accounts(
            db, wallet, mexc, str(uuid4()), currency="USDT",
            amount=Decimal("25"), occurred_at=WHEN,
        )
        db.add(RealMexcAccountLink(account_ref=ACCOUNT_REF, account_id=mexc,
                                   created_at=WHEN))
        db.commit()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        claim_mexc_uid_in_transaction(db, ACCOUNT_REF, "123456789", now=WHEN)
        db.commit()
        save_transfer_snapshot(db, ACCOUNT_REF, {
            "source": "MEXC_SPOT_PRIVATE_READONLY",
            "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
            "start_ms": int((WHEN - timedelta(days=1)).timestamp() * 1000),
            "end_ms": int((WHEN + timedelta(minutes=1)).timestamp() * 1000),
            "observed_at": (WHEN + timedelta(minutes=1)).isoformat(),
            "deposits": [{"direction": "deposit", "asset": "USDT", "network": "ERC20",
                          "amount": "25", "fee": "0", "status": 5,
                          "time_ms": int(WHEN.timestamp() * 1000),
                          "source_identity": TX_HASH,
                          "destination_identity": MEXC_DESTINATION,
                          "token_contract": TOKEN_CONTRACT}],
            "withdrawals": [{"direction": "withdrawal", "asset": "USDT", "network": "ERC20",
                             "amount": "25.1", "fee": "0.1", "status": 7,
                             "time_ms": int(WHEN.timestamp() * 1000),
                             "transfer_type": 0, "source_identity": "test-withdrawal-id",
                             "chain_identity": WITHDRAW_HASH,
                             "token_contract": TOKEN_CONTRACT,
                             "destination_identity": "0x" + "11" * 20}],
        }, view_uid="123456789")
    vault.lock()


seed()
app = FastAPI()
app.include_router(create_real_router(PATH, mexc_factory=FakeMexc))
register_ui(app)
