"""Servidor aislado del recorrido LAB→PAPER de señales, sin red externa."""

from contextlib import asynccontextmanager
from decimal import Decimal
import json

from fastapi import Body

from app.main import app, _lab_path, _paper_perp_path
from app.derivatives.paper_api import create_paper_perp_router
from app.derivatives.paper_math import PerpQuote
from app.derivatives.signal_monitor import create_signal_monitor_router
from app.derivatives.signal_monitor import SignalMonitor
from app.derivatives.signal_worker import SignalWorker
from app.lab.engine import validate_candles
from app.lab.store import LabStore


STEP = 3600
START = 1_735_689_600
state = {"now": START + 5 * STEP + 10}
rows = [{"t": START + i * STEP, "o": str(101-i), "h": str(102-i),
         "l": str(98-i), "c": str(100-i), "v": str(10+i)} for i in range(7)]


class Source:
    known_pairs = ("BTC/USDT:PERP",)

    def _instrument(self, symbol):
        return "BTC_USDT_Perp"

    def get_ticker(self, symbol):
        return {"instrument": "BTC_USDT_Perp", "event_time_ns": state["now"] * 1_000_000_000,
                "mark_price": "100", "bid": "99", "ask": "101"}

    def get_margin_rules(self, symbol):
        return {"instrument": "BTC_USDT_Perp", "fetched_at_ns": state["now"] * 1_000_000_000,
                "max_position_size": "1000", "risk_brackets": [
                    {"tier": 1, "notional_floor": "0", "notional_cap": "600000",
                     "maintenance_margin_rate": "0.01", "initial_margin_rate": "0.02",
                     "max_leverage": 50, "cumulative_maintenance_amount": "0"}]}

    def get_instrument_spec(self, symbol):
        return {"instrument": "BTC_USDT_Perp", "kind": "PERPETUAL", "venues": ["ORDERBOOK"],
                "base_decimals": 3, "tick_size": "0.01", "min_size": "0.01",
                "min_notional": "20", "max_position_size": "1000",
                "fetched_at_ns": state["now"] * 1_000_000_000}

    def get_historical_candles(self, symbol, timeframe, start, end, *, now):
        return [row.copy() for row in rows if start <= row["t"] < end]


app.router.routes[:] = [route for route in app.router.routes
                        if not getattr(route, "path", "").startswith("/paper/perp")
                        and not any(getattr(inner, "path", "").startswith("/paper/perp")
                                    for inner in getattr(getattr(route, "original_router", None), "routes", []))]
app.include_router(create_paper_perp_router(_paper_perp_path, adapter_factory=Source,
                                           clock_ns=lambda: state["now"] * 1_000_000_000))
monitor = SignalMonitor(_paper_perp_path, _lab_path, adapter_factory=Source,
                        clock_ns=lambda: state["now"] * 1_000_000_000)
signal_worker = SignalWorker(monitor, retry_seconds=0.02, idle_seconds=0.1)


class RiskWorkerFixture:
    """El navegador prueba el flujo de velas; el ticker real se prueba aparte."""
    def snapshot(self):
        return {"running": True, "connected": False, "symbols": [],
                "last_frame_ns": None, "last_frame_age_seconds": None,
                "last_error": None, "error_count": 0,
                "mode": "TEST_FIXTURE_NO_EXTERNAL_NETWORK"}

    def wake(self):
        pass


risk_worker = RiskWorkerFixture()


class FundingWorkerFixture:
    def snapshot(self):
        return {"running": True, "last_cycle_ns": state["now"] * 1_000_000_000,
                "heartbeat_age_seconds": 0, "last_error": None, "error_count": 0,
                "mode": "TEST_FIXTURE_NO_EXTERNAL_NETWORK",
                "execution": "PAPER_PUBLIC_FUNDING_ONLY"}

    def wake(self):
        pass


funding_worker = FundingWorkerFixture()
app.include_router(create_signal_monitor_router(_paper_perp_path, _lab_path,
                                                monitor=monitor, worker=signal_worker,
                                                risk_worker=risk_worker,
                                                funding_worker=funding_worker))

@asynccontextmanager
async def test_lifespan(_):
    signal_worker.start()
    try:
        yield
    finally:
        signal_worker.stop()


app.router.lifespan_context = test_lifespan

lab = LabStore(_lab_path)
bars, gaps = validate_candles(rows[:5], "1h", now=state["now"])
lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public", bars, gaps, verified_public=True)
lab.create_strategy("Corto 3 velas", {"direction": "SHORT", "initial_capital": "1000",
    "allocation": "0.1", "fee_rate": "0.001", "slippage_rate": "0",
    "stop_loss": "0.03", "take_profit": "0.08",
    "entry": {"kind": "BEARISH_STREAK", "period": 3}})


@app.post("/__test/tamper_lab_result")
def tamper_lab_result(body: dict = Body(...)):
    with lab.connect() as db:
        row = db.execute("SELECT result_json FROM runs WHERE id=?", (body["run_id"],)).fetchone()
        assert row is not None
        result = json.loads(row["result_json"])
        result["metrics"]["net_profit"] = "999999"
        db.execute("UPDATE runs SET result_json=? WHERE id=?",
                   (json.dumps(result), body["run_id"]))
    return {"changed": True}


@app.post("/__test/advance_signal")
def advance_signal(body: dict = Body(...)):
    state["now"] = int(body["now"])
    signal_worker.wake()
    return dict(state)


@app.post("/__test/advance_signal_continuous")
def advance_signal_continuous(body: dict = Body(...)):
    target = int(body["now"])
    session_id = str(body["session_id"])
    while state["now"] + 10 < target:
        state["now"] += 10
        quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                          Decimal("100"), Decimal("99"), Decimal("101"))
        assert monitor.on_risk_quote(session_id, quote) == "OBSERVED"
    if "bar" in body:
        row = body["bar"]
        rows.append({"t": START + 7 * STEP, "o": str(row["o"]), "h": str(row["h"]),
                     "l": str(row["l"]), "c": str(row["c"]), "v": "17"})
    state["now"] = target
    return monitor.poll(session_id)


@app.post("/__test/extend_signal_seed")
def extend_signal_seed():
    bars, gaps = validate_candles(rows[:6], "1h", now=state["now"])
    return lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public",
                            bars, gaps, verified_public=True)


@app.post("/__test/risk_quote")
def risk_quote(body: dict = Body(...)):
    state["now"] = int(body["now"])
    quote = PerpQuote("BTC_USDT_Perp", state["now"] * 1_000_000_000,
                      Decimal(str(body["mark"])), Decimal(str(body["bid"])),
                      Decimal(str(body["ask"])))
    return {"outcome": monitor.on_risk_quote(str(body["session_id"]), quote)}
