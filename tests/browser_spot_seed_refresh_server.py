"""Servidor de navegador: actualización pública controlada de la semilla Spot."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

import app.lab.api as lab_api
from app.automation.spot_strategy_paper import SpotStrategyPaper, create_spot_strategy_router
from app.automation.spot_strategy_worker import SpotStrategyWorker
from app.lab.engine import validate_candles
from app.lab.store import LabStore
from app.web.mount import register_ui


STEP = 3600
START = 1_735_689_600
NOW = START + 2000 * STEP + 10


class Clock:
    @staticmethod
    def now(_zone):
        return datetime.fromtimestamp(NOW, timezone.utc)


class Source:
    known_pairs = ("BTC/USDT",)

    def get_candles(self, symbol, timeframe="1h", limit=1000, since=None, before=None):
        assert symbol == "BTC/USDT" and timeframe == "1h"
        return [{"t": since + i * STEP, "o": "100", "h": "101", "l": "99",
                 "c": "100", "v": "10"} for i in range(limit)]

    def get_paper_quote(self, symbol):
        return {"symbol": symbol, "source": "mexc-spot-public",
                "event_time_ns": NOW * 1_000_000_000,
                "bid": "99", "ask": "101", "last": "100"}


lab_api.datetime = Clock
lab_api.MexcAdapter = Source
lab_path = Path(os.environ["ORBIS_LAB_PATH"])
paper_path = Path(os.environ["ORBIS_SPOT_STRATEGY_PATH"])
lab = LabStore(lab_path)
old_rows = [{"t": START + i * STEP, "o": "100", "h": "101", "l": "99",
             "c": "100", "v": "10"} for i in range(8)]
bars, gaps = validate_candles(old_rows, "1h", now=NOW)
lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars, gaps, verified_public=True)
lab.create_strategy("Versión fija", {"direction": "LONG", "initial_capital": "1000",
    "allocation": "0.1", "fee_rate": "0.001", "slippage_rate": "0",
    "stop_loss": "0.03", "take_profit": "0.08",
    "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}})
paper = SpotStrategyPaper(paper_path, lab_path, adapter_factory=Source,
                          clock_ns=lambda: NOW * 1_000_000_000)
worker = SpotStrategyWorker(paper, retry_seconds=0.02, idle_seconds=0.1)


@asynccontextmanager
async def lifespan(_):
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(lab_api.create_lab_router(lab_path))
app.include_router(create_spot_strategy_router(paper_path, lab_path, paper=paper, worker=worker))
register_ui(app)
