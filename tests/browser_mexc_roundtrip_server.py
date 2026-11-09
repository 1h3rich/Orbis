"""Puerto/bóveda/exchange exclusivos del ciclo de navegador, sin mainnet."""

import os
from pathlib import Path

from app.automation.spot_strategy_paper import create_spot_strategy_router
from app.automation.spot_paper_access import spot_paper_access
from app.lab.api import create_lab_router
from app.web.mount import register_ui
from tests.mexc_cancel_fixture import CancelableExchangeOrders
from tests.mexc_roundtrip_fixture import Roundtrip
from tests.test_mexc_execution_state import PASSWORD


scenario = Roundtrip(Path(os.environ["ORBIS_TEST_ROUNDTRIP_DIRECTORY"]))
manual = os.environ.get("ORBIS_TEST_MEXC_MANUAL") == "1"
if manual:
    scenario.env["trader"] = CancelableExchangeOrders(scenario.venue, scenario.clock)
funding_closure = os.environ.get("ORBIS_TEST_FUNDING_CLOSURE") == "1"
funding_server_read = funding_closure or any(os.environ.get(flag) == "1" for flag in
    ("ORBIS_TEST_FUNDING_NONCE", "ORBIS_TEST_FUNDING_BNB"))
app = scenario.make_app(manual=manual,funding_nonce=funding_server_read,
                        withdrawal_bnb=os.environ.get("ORBIS_TEST_WITHDRAW_BNB") == "1")
if not manual:
    lab, paper = scenario.env["lab"], scenario.env["paper"]
    spot_paper_access.unlock(paper.path, PASSWORD)
    app.include_router(create_lab_router(lab.path))
    app.include_router(create_spot_strategy_router(paper.path, lab.path, paper=paper))
register_ui(app)


@app.get("/__test/state")
def state():
    return {"manual":manual,"wallet_usdt":str(scenario.wallet_usdt),"wallet_bnb_wei":str(scenario.wallet_bnb_wei),
            "paper_session":scenario.env["session"]["id"],"version":scenario.env["session"]["version_id"],
            "validation":scenario.env["validation"]["id"],
            "exchange_usdt":scenario.venue.quote,"exchange_btc":scenario.venue.base,
            "wallet_sends":len(scenario.wallet_sends),"orders":len(scenario.env["trader"].posts),
            "cancellations":len(getattr(scenario.env["trader"],"cancellations",[])),
            "withdrawals":len(scenario.withdrawals),"workers":[worker.state() for worker in scenario.workers]}


@app.post("/__test/wallet-send")
def wallet_send(transaction: dict):
    return scenario.wallet_send(transaction)


@app.post("/__test/wallet-cancel-funding")
def cancel_funding(evidence: dict):
    assert funding_closure and not scenario.wallet_sends and scenario.funding_rpc is None
    receipt = evidence["receipt"]
    scenario.wallet_bnb_wei -= int(receipt["gasUsed"],16)*int(receipt["effectiveGasPrice"],16)
    scenario.funding_rpc = evidence
    return {"cancelled":True}


@app.post("/__test/leave-order-open")
def leave_order_open(immediate_cancel: bool = False):
    assert manual
    if immediate_cancel:
        scenario.env["trader"].open_next = "cancelled"
    else:
        scenario.env["trader"].open_next = "partial" if os.environ.get("ORBIS_TEST_MEXC_PARTIAL") == "1" else "open"
    return {"configured":True}


@app.post("/__test/finish-open-order")
def finish_open_order(body: dict):
    assert manual and os.environ.get("ORBIS_TEST_MEXC_VIEW_ROTATION") == "1"
    scenario.env["trader"].finish(body["intent_id"])
    return {"finished":True}


@app.post("/__test/credit")
def credit():
    scenario.credit_funding()
    return state()


@app.post("/__test/entry")
def entry():
    return scenario.advance()


@app.post('/__test/arm-clock-shift')
def arm_clock_shift():
    assert os.environ.get('ORBIS_TEST_EXPECT_CLOCK_PAUSE')=='1'
    scenario.clock_shift_armed=True
    return {'armed':True}


@app.post("/__test/target")
def target():
    scenario.target()
    return {"changed":True}


@app.post("/__test/complete")
def complete():
    return scenario.complete_withdrawal()
