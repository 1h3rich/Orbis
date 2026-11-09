"""Servidor PAPER DCA de navegador con reloj y mercado falsos, bases temporales."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI

from app.automation.api import create_automation_router
from app.automation.paper_dca import PaperDca, create_paper_dca_router
from app.automation.paper_worker import PaperDcaWorker
from app.automation.store import AutomationStore
from app.lab.api import create_lab_router
from app.lab.engine import validate_candles
from app.lab.store import LabStore
from app.web.mount import register_ui


DAY = 86400
START = 1_735_689_600
average = os.environ.get("ORBIS_TEST_DCA_AVERAGE") == "1"
volume = os.environ.get("ORBIS_TEST_DCA_VOLUME") == "1"
reserve = os.environ.get("ORBIS_TEST_DCA_RESERVE") == "1"
seed_count = 100 if reserve else 4 if average or volume else 3
state = {"now": START + seed_count * DAY + 3600}
if reserve:
    prices = tuple(100 + i % 7 for i in range(seed_count + 2))
elif average:
    prices = (100, 100, 160, 125, 90, 140)
elif os.environ.get("ORBIS_TEST_DCA_SMA") == "1":
    prices = (100, 95, 80, 110, 75, 85)
elif volume:
    prices = (100, 95, 80, 75, 74, 70, 69)
else:
    prices = (100, 95, 80, 70, 65, 60)
volumes = (10, 10, 20, 5, 5, 10, 10) if volume else (10,) * len(prices)
rows = [{"t": START + i * DAY, "o": str(price + 1), "h": str(price + 2),
         "l": str(price - 1), "c": str(price), "v": str(volumes[i])}
        for i, price in enumerate(prices)]


class Source:
    def get_candles(self, symbol, timeframe="1d", limit=3, since=None, before=None):
        return [row.copy() for row in rows if since <= row["t"] < before][:limit]

    def get_paper_quote(self, symbol):
        return {"symbol": symbol, "source": "mexc-spot-public",
                "event_time_ns": (state["now"] - 1) * 1_000_000_000,
                "bid": "70", "ask": "71", "last": "70"}


lab_path = Path(os.environ["ORBIS_LAB_PATH"])
automation_path = Path(os.environ["ORBIS_AUTOMATION_PATH"])
if reserve:
    from app.automation.access import automation_access
    from app.lab.access import lab_access
    lab_access.create(lab_path, "owned dca reserve browser password")
    automation_access.create(automation_path, "owned dca reserve browser password")
lab = LabStore(lab_path)
bars, gaps = validate_candles(rows[:seed_count], "1d", now=state["now"])
lab.save_dataset("BNB/USDT" if volume else "BTC/USDT", "1d", "mexc-spot-public", bars, gaps, verified_public=True)
AutomationStore(automation_path).create_rule("DCA comprobación", {
    "frequency": "daily", "amount": "100", "budget_total": "1000", "monthly_limit": "300",
    "max_exposure": "1", "fee_rate": "0.001", "slippage_rate": "0.002",
    "max_extra_activations": 1, "cooldown_bars": 0,
    "dips": [{"drop_pct": "0.1", "amount": "50"}],
})

paper = PaperDca(automation_path, lab_path, adapter_factory=Source,
                 clock_ns=lambda: state["now"] * 1_000_000_000)
worker = PaperDcaWorker(paper, retry_seconds=0.02, idle_seconds=0.1)


@asynccontextmanager
async def lifespan(_):
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(create_lab_router(lab_path))
app.include_router(create_automation_router(automation_path, lab_path))
app.include_router(create_paper_dca_router(automation_path, lab_path, paper=paper, worker=worker))
register_ui(app)


@app.post("/__test/advance_paper_dca")
def advance(body: dict = Body(...)):
    state["now"] = int(body["now"])
    worker.wake()
    return state
