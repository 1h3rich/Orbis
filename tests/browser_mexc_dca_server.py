"""Servidor aislado: DCA probado/PAPER y trabajador REAL con exchange ficticio."""

import os
from pathlib import Path

from app.automation.api import create_automation_router
from app.automation.paper_dca import create_paper_dca_router
from app.lab.api import create_lab_router
from app.marketdata import service as market_service
from app.web.mount import register_ui
from tests.mexc_dca_fixture import DcaScenario, EMA_RULE


average = os.environ.get("ORBIS_TEST_MEXC_DCA_AVERAGE") == "1"
volume = os.environ.get("ORBIS_TEST_MEXC_DCA_VOLUME") == "1"
scenario = DcaScenario(Path(os.environ["ORBIS_TEST_DCA_DIRECTORY"]), average_rule=EMA_RULE if average else None, volume=volume)
app = scenario.make_app()
app.include_router(create_lab_router(scenario.env["lab"].path))
if average or volume:
    app.include_router(create_automation_router(scenario.paper.store.path, scenario.paper.lab.path))
    app.include_router(create_paper_dca_router(scenario.paper.store.path, scenario.paper.lab.path, paper=scenario.paper))
register_ui(app)


def candles(symbol,*,provider,timeframe,limit,since=None,before=None):
    assert provider == "mexc" and symbol == "BTC/USDT"
    step = {"1h":3600,"1d":86400}[timeframe]
    now = int(scenario.clock().timestamp())
    start = now//step*step-12*step if before is None else before-6*step
    rows = [{"t":start+i*step,"o":"65000","h":"66000","l":"49000","c":"57000","v":"10",
             "closed":start+(i+1)*step <= now} for i in range(13 if before is None else 6)]
    return {"available":True,"resolved_provider":"mexc","symbol":symbol,"timeframe":timeframe,
            "candles":rows,"older_pagination_supported":True,"history_exhausted":before is not None}


market_service.candles = candles


@app.post("/__test/observe/{day}")
def observe(day:int):
    assert day in (2,3)
    return scenario.observe(day)


@app.post("/__test/advance/{day}")
def advance(day:int):
    assert day in (2,3)
    scenario.advance(day)
    return {"changed":True}


@app.get("/__test/posts")
def posts():
    return {"count":len(scenario.env["trader"].posts),"workers":[worker.state() for worker in scenario.workers]}
