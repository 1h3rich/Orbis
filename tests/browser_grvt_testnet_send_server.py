"""Servidor aislado: nunca realiza llamadas a GRVT ni usa datos del usuario."""

import os
from pathlib import Path

from app.web.mount import register_ui
from tests.grvt_testnet_ui_fixture import GrvtTestnetScenario


scenario = GrvtTestnetScenario(Path(os.environ["ORBIS_TEST_GRVT_DIRECTORY"]))
app = scenario.make_app()
register_ui(app)


@app.post("/__test/seed/{direction}")
def seed(direction: str, closing: bool = False):
    return scenario.seed(direction,closing=closing)


@app.get("/__test/state")
def state():
    return {"orders":scenario.orders, "calls":scenario.calls}


@app.post("/__test/lost")
def lose(lost: bool = True):
    scenario.lose_response = lost
    return {"configured":True}
