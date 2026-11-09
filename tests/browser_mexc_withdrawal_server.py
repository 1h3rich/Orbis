"""Saldo REAL de prueba cifrado y retiro hacia una wallet ficticia, en puerto aislado."""

from datetime import datetime,timedelta,timezone
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.repository import create_account,append_event
from app.portfolio.vault import RealVault
from app.web.mount import register_ui
from tests.test_mexc_withdrawal_api import WithdrawalClient,WithdrawalView,PASSWORD
from tests.test_wallet_funding import TX_HASH
from tests.test_mexc_withdrawal_receipt import evidence
from tests.test_mexc_deposit_destination import CONTRACT


directory = Path(os.environ["ORBIS_TEST_WITHDRAWAL_DIRECTORY"])
vault = RealVault(directory / "real.db")
vault.unlock(PASSWORD,create=True)
with vault.session() as db:
    account = create_account(db,"MEXC","USDT")
    append_event(db,account.id,str(uuid4()),EventInput("CONTRIBUTION","USDT",
        datetime.now(timezone.utc)-timedelta(days=1),amount=Decimal("100")))
    create_account(db,"Wallet","USDT")
vault.lock()
app = FastAPI()
app.include_router(create_real_router(directory / "real.db",mexc_factory=WithdrawalView,mexc_withdrawal_factory=WithdrawalClient))
register_ui(app)


@app.get("/_fixture/posts")
def posts():
    return {"count":len(WithdrawalClient.posts)}


@app.post("/_fixture/complete")
def complete():
    from decimal import localcontext
    WithdrawalClient.completed = True
    post = WithdrawalClient.posts[0]
    with localcontext() as context:
        context.prec = 300
        net = str(Decimal(post["amount"][0])-Decimal("0.01"))
    return evidence(WithdrawalClient.times[0],received=net,recipient=post["address"][0],chain=TX_HASH,contract=CONTRACT)
