"""Servidor OCO aislado. SQLCipher/API reales, GRVT exclusivamente sintético."""

import os
from pathlib import Path

_directory = Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
os.environ['ORBIS_DATABASE_URL'] = 'sqlite://'
for _key,_name in (('ORBIS_LAB_PATH','bootstrap-lab.db'),('ORBIS_AUTOMATION_PATH','bootstrap-automation.db'),
    ('ORBIS_PAPER_PERP_PATH','bootstrap-perp.db'),('ORBIS_SPOT_STRATEGY_PATH','bootstrap-spot.db'),
    ('ORBIS_REAL_VAULT_PATH','bootstrap-real.db')):
    os.environ[_key] = str(_directory/_name)

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from tests.grvt_oco_ui_fixture import OcoUiVenue


venue = OcoUiVenue(Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY']))
venue.lose_create_reply = os.environ.get('ORBIS_TEST_OCO_LOST_REPLY') == '1'
app = FastAPI()
app.include_router(create_real_router(venue.directory/'real.db',grvt_testnet_factory=venue.factory,
    grvt_testnet_oco_public_post=venue.public))
register_ui(app)


@app.middleware('http')
async def only_isolated_routes(request,call_next):
    # El arranque visual no consulta proveedores de precios ajenos a esta prueba.
    if not request.url.path.startswith(('/ui','/real','/__test')):
        return JSONResponse({'detail':'Ruta fuera del escenario aislado'},status_code=404)
    return await call_next(request)


@app.post('/__test/trigger')
def trigger():
    venue.trigger()
    return {'triggered':True}


@app.get('/__test/state')
def state():
    return {'bulk_sends':venue.bulk_sends,'cancel_sends':venue.cancel_sends,
        'position':str(venue.position),'balance':str(venue.balance),'calls':venue.calls}
