"""Bóveda aislada con un retiro MEXC con comisión aún no conciliada."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

from app.live_execution.mexc_uid_guard import claim_mexc_uid_in_transaction
from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.mexc_transfers import save_transfer_snapshot
from app.portfolio.models import RealMexcAccountLink
from app.portfolio.repository import append_event, create_account, transfer_asset_between_accounts
from app.portfolio.vault import RealVault
from app.web.mount import register_ui


PATH = Path(os.environ["ORBIS_REAL_VAULT_PATH"])
PASSWORD = "browser fee hypothesis password"
WHEN = datetime.now(timezone.utc) - timedelta(minutes=15)
ACCOUNT_REF = "a" * 64


def seed():
    if PATH.exists():
        return
    vault = RealVault(PATH)
    vault.unlock(PASSWORD, create=True)
    with vault.session() as db:
        mexc = create_account(db, "MEXC comisión", "USDT").id
        wallet = create_account(db, "Wallet prueba", "USDT").id
        append_event(db, mexc, str(uuid4()), EventInput(
            "BUY", "USDT", WHEN - timedelta(days=2), asset="BTC",
            quantity=Decimal("2"), unit_price=Decimal("100")))
        transfer_asset_between_accounts(
            db, mexc, wallet, str(uuid4()), asset="BTC", quote_currency="USDT",
            quantity=Decimal("1"), fee=Decimal("0.1"), occurred_at=WHEN)
        db.add(RealMexcAccountLink(account_ref=ACCOUNT_REF, account_id=mexc,
                                   created_at=WHEN))
        db.commit()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        claim_mexc_uid_in_transaction(db, ACCOUNT_REF, "123456789", now=WHEN)
        db.commit()
        save_transfer_snapshot(db, ACCOUNT_REF, {
            "source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
            "start_ms": int((WHEN - timedelta(days=1)).timestamp() * 1000),
            "end_ms": int((WHEN + timedelta(minutes=1)).timestamp() * 1000),
            "observed_at": (WHEN + timedelta(minutes=1)).isoformat(),
            "deposits": [],
            "withdrawals": [{"direction": "withdrawal", "asset": "BTC", "network": "BTC",
                             "amount": "0.9", "fee": "0.1", "status": 7,
                             "transfer_type": 0, "time_ms": int(WHEN.timestamp() * 1000),
                             "source_identity": "fee-withdrawal-id"}],
        }, view_uid="123456789")
    vault.lock()


seed()
app = FastAPI()
app.include_router(create_real_router(PATH))
register_ui(app)
