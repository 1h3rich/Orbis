"""Servidor aislado para recorrer LAB → Spot PAPER sin red ni credenciales."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI

from app.automation.spot_strategy_paper import SpotStrategyPaper, create_spot_strategy_router
from app.automation.spot_strategy_worker import SpotStrategyWorker
from app.lab.api import create_lab_router
from app.lab.engine import validate_candles
from app.lab.store import LabStore
from app.web.mount import register_ui


STEP = 3600
START = 1_735_689_600
state = {"now": START + 3 * STEP + 10, "bid": "99", "ask": "100"}
rows = [{"t": START + i * STEP, "o": str(c + 1), "h": str(c + 2),
         "l": str(c - 1), "c": str(c), "v": str(10 + i)}
        for i, c in enumerate((100, 99, 98, 97, 110))]


class Source:
    known_pairs = ("BTC/USDT",)

    def get_candles(self, symbol, timeframe="1h", limit=3, since=None, before=None):
        return [row.copy() for row in rows if row["t"] >= since][:limit]

    def get_paper_quote(self, symbol):
        return {"symbol": symbol, "source": "mexc-spot-public",
                "event_time_ns": state["now"] * 1_000_000_000,
                "bid": state["bid"], "ask": state["ask"], "last": state["bid"]}


lab_path = Path(os.environ["ORBIS_LAB_PATH"])
paper_path = Path(os.environ["ORBIS_SPOT_STRATEGY_PATH"])
lab = LabStore(lab_path)
reserve_rows = [{"t": START-(120-i)*STEP, "o": "101", "h": "102", "l": "98", "c": "100", "v": "20"}
                for i in range(120)]
reserve_bars, reserve_gaps = validate_candles(reserve_rows, "1h", now=state["now"])
reserve_data = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public",
                                reserve_bars, reserve_gaps, verified_public=True)
bars, gaps = validate_candles(rows[:3], "1h", now=state["now"])
lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars, gaps, verified_public=True)
lab.create_strategy("Tres velas LONG", {"direction": "LONG", "initial_capital": "1000",
    "allocation": "0.1", "fee_rate": "0.001", "slippage_rate": "0",
    "stop_loss": "0.03", "take_profit": "0.08",
    "entry": {"op": "AND", "children": [
        {"kind": "BEARISH_STREAK", "period": 3},
        {"kind": "VOLUME_RISING_STREAK", "period": 3},
    ]}})
paper = SpotStrategyPaper(paper_path, lab_path, adapter_factory=Source,
                          clock_ns=lambda: state["now"] * 1_000_000_000)
worker = SpotStrategyWorker(paper, retry_seconds=0.02, idle_seconds=0.1)


@asynccontextmanager
async def lifespan(_):
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(create_lab_router(lab_path))
app.include_router(create_spot_strategy_router(paper_path, lab_path, paper=paper, worker=worker))
register_ui(app)


@app.get("/__test/spot_reserve_dataset")
def spot_reserve_dataset():
    return {"id": reserve_data["id"]}


@app.post("/__test/advance_spot")
def advance(body: dict = Body(...)):
    target = int(body["now"])
    if body.get("continuous"):
        # El reloj de prueba avanza sin ocultar una hora sin observación.
        # Mantiene la cotización anterior hasta el instante objetivo.
        while state["now"] + 25 < target:
            state["now"] += 25
            for item in paper.active_sessions():
                if item["state"]["units"] != "0":
                    paper.poll(item["id"])
    state["now"] = target
    state["bid"] = str(body.get("bid", state["bid"]))
    state["ask"] = str(body.get("ask", state["ask"]))
    worker.wake()
    return state
