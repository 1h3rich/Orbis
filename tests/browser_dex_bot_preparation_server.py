"""Isolated real API + encrypted LAB/PAPER fixtures; never sends transactions."""
from pathlib import Path
import os
from fastapi import FastAPI
from app.automation.api import create_automation_router
from app.automation.paper_dca import create_paper_dca_router
from app.lab.api import create_lab_router
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from tests.dex_bot_source_fixture import SourceCase,DcaSourceCase

base=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
for name in ('long','dca'):(base/name).mkdir(parents=True,exist_ok=True)
source=SourceCase(base/'long',research=True);dca=DcaSourceCase(base/'dca',research=True)
app=FastAPI()
app.include_router(create_real_router(base/'real.db',spot_paper=source.paper,dca_paper=dca.paper))
app.include_router(create_lab_router(dca.lab.path))
app.include_router(create_automation_router(dca.store.path,dca.lab.path))
app.include_router(create_paper_dca_router(dca.store.path,dca.lab.path,paper=dca.paper))
register_ui(app)


@app.get('/__test/state')
def state():return {'fixture_id':base.name,'dca_session':dca.session['id'],'dca_reserve':dca.validation['id']}
