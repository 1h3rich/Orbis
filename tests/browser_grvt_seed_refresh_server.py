"""Servidor aislado para renovar la semilla GRVT sin red externa."""

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

import app.lab.api as lab_api
from app.derivatives.paper_api import create_paper_perp_router
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.signal_monitor import SignalMonitor, create_signal_monitor_router
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
    known_pairs = ("BTC/USDT:PERP",)

    def _instrument(self, symbol):
        return "BTC_USDT_Perp" if symbol in self.known_pairs else None

    def get_historical_candles(self, symbol, timeframe, start, end, *, now):
        assert symbol == "BTC/USDT:PERP" and timeframe == "1h" and now == NOW
        return [{"t": start + i * STEP, "o": "100", "h": "101", "l": "99",
                 "c": "100", "v": "10"} for i in range((end - start) // STEP)]

    def get_instrument_spec(self, symbol):
        return {"instrument": self._instrument(symbol), "kind": "PERPETUAL",
                "venues": ["ORDERBOOK"], "base_decimals": 3, "tick_size": "0.01",
                "min_size": "0.01", "min_notional": "20", "max_position_size": "1000",
                "fetched_at_ns": NOW * 1_000_000_000}


lab_api.datetime = Clock
lab_api.GrvtAdapter = Source
lab_path = Path(os.environ["ORBIS_LAB_PATH"])
paper_path = Path(os.environ["ORBIS_PAPER_PERP_PATH"])
lab = LabStore(lab_path)
old_rows = [{"t": START + i * STEP, "o": "100", "h": "101", "l": "99",
             "c": "100", "v": "10"} for i in range(8)]
bars, gaps = validate_candles(old_rows, "1h", now=NOW)
lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", bars, gaps,
                 verified_public=True)
lab.create_strategy("Versión GRVT fija", {"direction": "SHORT", "initial_capital": "1000",
    "allocation": "0.1", "fee_rate": "0.001", "slippage_rate": "0",
    "stop_loss": "0.03", "take_profit": "0.08",
    "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}})
paper = PaperPerpStore(paper_path)
paper.create_account("Cuenta de prueba", "10000", "1000", "2000", "2", 2, "0.0005", "5")
monitor = SignalMonitor(paper_path, lab_path, adapter_factory=Source,
                        clock_ns=lambda: NOW * 1_000_000_000)

app = FastAPI()
app.include_router(lab_api.create_lab_router(lab_path))
app.include_router(create_paper_perp_router(paper_path, adapter_factory=Source,
                                            clock_ns=lambda: NOW * 1_000_000_000))
app.include_router(create_signal_monitor_router(paper_path, lab_path, monitor=monitor,
                                                adapter_factory=Source,
                                                clock_ns=lambda: NOW * 1_000_000_000))
register_ui(app)
