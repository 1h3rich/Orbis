"""Servidor aislado del circuito GRVT; nunca llama al proveedor real."""

import os
from pathlib import Path
from decimal import Decimal

from app.web.mount import register_ui
from tests.grvt_testnet_loop_fixture import GrvtTestnetLoop


venue = GrvtTestnetLoop(Path(os.environ["ORBIS_TEST_GRVT_DIRECTORY"]))
venue.funding_on_close = Decimal(os.environ.get("ORBIS_TEST_GRVT_FUNDING","0"))
app = venue.make_app()
register_ui(app)
opening = None


@app.post("/__test/seed")
def seed():
    global opening
    assert opening is None
    opening = venue.seed("LONG")
    return {**opening,"price":str(venue.price),"funding":str(venue.funding_on_close)}


@app.post("/__test/close")
def close():
    assert opening is not None
    return {**venue.close_source(opening),"price":str(venue.price)}


@app.get("/__test/state")
def state():
    return {"orders":venue.orders,"balance":str(venue.balance),"position":str(venue.position),"calls":venue.calls}
