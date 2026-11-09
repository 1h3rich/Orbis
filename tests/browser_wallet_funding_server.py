"""Wallet/MEXC controlados y bóveda temporal; no existe un cliente de envío RPC."""

import os
from datetime import datetime,timezone
from pathlib import Path

from fastapi import FastAPI

from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from app.portfolio.vault import RealVault
from app.portfolio.repository import account_snapshot,verify_paired_transfers
from tests.test_wallet_funding_api import BscFakeMexc,PASSWORD
from tests.test_wallet_funding_credit import snapshot
from tests.test_mexc_deposit_destination import ADDRESS,CONTRACT


class FundingBrowserMexc(BscFakeMexc):
    deposit = None

    def transfers_recent(self,**kwargs):
        now = datetime.now(timezone.utc)
        if self.deposit:
            return {**self.deposit,"observed_at":now.isoformat(),"end_ms":int(now.timestamp()*1000)}
        return {**snapshot(now),"deposits":[]}


app = FastAPI()
vault_path = Path(os.environ["ORBIS_TEST_FUNDING_DIRECTORY"]) / "real.db"
app.include_router(create_real_router(vault_path,mexc_factory=FundingBrowserMexc))
source_id = None


@app.post("/_fixture/linked-wallet")
def fixture_source(body: dict):
    global source_id
    from tests.test_wallet_funding_internal import source_wallet
    vault = RealVault(vault_path); vault.unlock(PASSWORD)
    try:
        source_id,_ = source_wallet(vault,datetime.now(timezone.utc),address=body["address"])
        return {"id":source_id}
    finally:
        vault.lock()


@app.get("/_fixture/source-book")
def fixture_books():
    vault = RealVault(vault_path); vault.unlock(PASSWORD)
    try:
        with vault.session() as db:
            book = account_snapshot(db,source_id)
            return {"cash":str(book.cash["USDT"]),"contribution":str(book.contributions["USDT"]),
                    "pairs":verify_paired_transfers(db)}
    finally:
        vault.lock()


@app.post("/_fixture/deposit")
def fixture_deposit(status: int = 5):
    if FundingBrowserMexc.deposit is None:
        FundingBrowserMexc.deposit = snapshot(datetime.now(timezone.utc),amount="12.345678901234567891",
            tx_hash="0x"+"1".zfill(64),destination=ADDRESS,contract=CONTRACT,status=status)
    else:
        FundingBrowserMexc.deposit["deposits"][0]["status"] = status
    return {"fixture":True}


register_ui(app)
