"""Servidor aislado para órdenes desde UI; MEXC y credenciales son ficticios."""

from dataclasses import replace
import hashlib
import hmac
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Body, FastAPI

from app.lab.api import create_lab_router
from app.lab.store import LabStore
from app.live_execution.journal import register_spot_policy
from app.portfolio.api import create_real_router
from app.portfolio.repository import create_account
from app.portfolio.vault import RealVault
from app.web.mount import register_ui
from tests import test_mexc_execution_state as fixtures
from tests.test_mexc_execution import ExchangeOrders
from tests.test_mexc_execution_api import RealtimeClock, Probe
from tests.test_mexc_policy_drafts_api import definition


directory = Path(os.environ["ORBIS_TEST_EXECUTION_DIRECTORY"])
fixtures.Clock = RealtimeClock
fixtures.ACCOUNT = hmac.new(b"fixture-secret", b"fixture-key", hashlib.sha256).hexdigest()
existing = os.environ.get("ORBIS_TEST_MEXC_EXISTING") == "1"
lab_path = directory / "lab.db"
if existing:
    vault = RealVault(directory / "real.db")
    vault.unlock(fixtures.PASSWORD, create=True)
    with vault.session() as db:
        create_account(db, "MEXC existente", "USDT")
    clock = RealtimeClock()
    venue = fixtures.Venue(clock)
    venue.base = "0.002"
    venue.other = [fixtures.ETH_BALANCE.copy()]
else:
    vault, policy, _, venue, clock = fixtures.setup(directory)
    lab = LabStore(lab_path)
    version_id = lab.create_strategy("Órdenes manuales de prueba", definition())["version_id"]
    draft = replace(policy, policy_id=str(uuid4()), strategy_version_id=version_id,
                    enabled=False, reconciliation_anchor_id=None, allow_risk_reducing_exits=False)
    register_spot_policy(vault, draft, now=clock())
vault.lock()
trader = ExchangeOrders(venue, clock)
fault = {"next_test":False}


def after_test():
    if fault["next_test"]:
        fault["next_test"] = False
        venue.quote = "701"


trader.after_test = after_test
app = FastAPI()
app.include_router(create_real_router(
    directory / "real.db", lab_path=lab_path if not existing else None, mexc_factory=lambda *_: venue,
    mexc_trade_factory=Probe, mexc_live_factory=lambda *_: trader,
    mexc_quote_get=venue.quote_get,
    mexc_rules_fetch=lambda symbol, now: venue.rules_fetch(symbol, now)))
if not existing:
    app.include_router(create_lab_router(lab_path))
register_ui(app)


@app.get("/__test/posts")
def posts():
    return {"count":len(trader.posts)}


@app.post("/__test/execution-fault")
def change(body: dict = Body(...)):
    fault["next_test"] = bool(body.get("next_test", False))
    if body.get("restore_quote"):
        venue.quote = "700"
    if body.get("move_quote"):
        assert existing
        venue.quote = "701"
    return fault
