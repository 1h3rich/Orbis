"""Servidor DCA PAPER aislado: velas y ticker ficticios, ninguna red MEXC."""

import os
from pathlib import Path

from fastapi import FastAPI

from app.automation.api import create_automation_router
from app.automation.paper_dca import DAY, PaperDca, create_paper_dca_router
from app.automation.store import AutomationStore
from app.lab.api import create_lab_router
from app.lab.engine import validate_candles
from app.lab.store import LabStore
from app.web.mount import register_ui


lab_path = Path(os.environ["ORBIS_LAB_PATH"])
automation_path = Path(os.environ["ORBIS_AUTOMATION_PATH"])
start = 1_735_689_600
now = start + 4 * DAY + 1
prices = (100, 95, 80, 70)
bars = [{"t": start + index * DAY, "o": str(price + 1),
         "h": str(price + 2), "l": str(price - 1), "c": str(price), "v": str(10 + index)}
        for index, price in enumerate(prices)]


class FakeMexc:
    known_pairs = frozenset(("BTC/USDT",))

    def get_candles(self, symbol, timeframe="1d", limit=3, since=None, before=None):
        return [bar for bar in bars if since <= bar["t"] < before][:limit]

    def get_paper_quote(self, symbol):
        return {"symbol": symbol, "source": "mexc-spot-public",
                "event_time_ns": (now - 1) * 1_000_000_000,
                "bid": "70", "ask": "71", "last": "70"}


lab = LabStore(lab_path)
store = AutomationStore(automation_path)
seed, gaps = validate_candles(bars[:3], "1d", now=start + 3 * DAY + 3600)
dataset = lab.save_dataset("BTC/USDT", "1d", "mexc-spot-public", seed, gaps,
                           verified_public=True)
version = store.create_rule("DCA PAPER gráfico", {
    "frequency": "daily", "reference_mode": "initial", "amount": "100",
    "budget_total": "1000", "monthly_limit": "300", "max_exposure": "1",
    "fee_rate": "0.001", "slippage_rate": "0.002",
    "max_extra_activations": 2, "cooldown_bars": 0,
    "dips": [{"drop_pct": "0.1", "amount": "50"},
             {"drop_pct": "0.2", "amount": "100"}],
})
clock = {"now": start + 3 * DAY + 3600}
paper = PaperDca(automation_path, lab_path, adapter_factory=FakeMexc,
                 clock_ns=lambda: clock["now"] * 1_000_000_000)
session = paper.create(version["version_id"], dataset["id"])
clock["now"] = now
assert paper.poll(session["id"])["poll_result"] == "OBSERVED"

app = FastAPI()
app.include_router(create_lab_router(lab_path))
app.include_router(create_automation_router(automation_path, lab_path))
app.include_router(create_paper_dca_router(automation_path, lab_path, paper=paper))
register_ui(app)
