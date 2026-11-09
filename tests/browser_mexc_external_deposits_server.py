"""MEXC externo: proveedor controlado y bóveda desechable para navegador."""

import os
from pathlib import Path

from app.web.mount import register_ui
from tests.mexc_roundtrip_fixture import Roundtrip
from tests.test_mexc_external_deposits import add_credit


scenario = Roundtrip(Path(os.environ["ORBIS_TEST_ROUNDTRIP_DIRECTORY"]))
add_credit(scenario, "40.125")
scenario.venue.quote = "740.125"
app = scenario.make_app(manual=True)
register_ui(app)


@app.get("/__test/state")
def state():
    return {"orders": len(scenario.env["trader"].posts), "wallet_sends": len(scenario.wallet_sends),
            "withdrawals": len(scenario.withdrawals)}
