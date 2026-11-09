"""MEXC y libro REAL ficticios para probar la sincronización completa."""

from decimal import Decimal

from fastapi import Body
from sqlalchemy import select

from tests.browser_mexc_booking_server import (
    app, PATH, PASSWORD, ACCOUNT_REF, NOW, FakeMexc,
)
from app.portfolio.models import RealAccount, RealMexcAccountLink
from app.portfolio.vault import RealVault


state = {"quote_adjustment": "1"}
original_balances = FakeMexc.balances


def balances(self):
    result = original_balances(self)
    for row in result["balances"]:
        if row["asset"] == "USDT":
            row["free"] = row["total"] = str(Decimal(row["free"]) + Decimal(state["quote_adjustment"]))
    return result


FakeMexc.balances = balances
vault = RealVault(PATH)
vault.unlock(PASSWORD)
with vault.session() as db:
    if db.get(RealMexcAccountLink, ACCOUNT_REF) is None:
        account = db.scalar(select(RealAccount))
        db.add(RealMexcAccountLink(account_ref=ACCOUNT_REF, account_id=account.id, created_at=NOW))
        db.commit()
vault.lock()


@app.post("/__test/lifecycle")
def change(body: dict = Body(...)):
    state["quote_adjustment"] = str(Decimal(body["quote_adjustment"]))
    return state
