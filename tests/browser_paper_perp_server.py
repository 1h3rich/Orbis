"""Servidor exclusivo del smoke de navegador: cotizaciones GRVT controladas."""

import os
from pathlib import Path

from fastapi import Body

from app.main import app
from app.derivatives.paper_api import create_paper_perp_router
from app.derivatives.signal_monitor import create_signal_monitor_router


app.router.routes[:] = [
    route for route in app.router.routes
    if not any(getattr(inner, "path", "").startswith("/paper/perp")
               for inner in getattr(getattr(route, "original_router", None), "routes", []))
]

state = {"now": 1_800_001_800_000_000_000, "bid": "99", "ask": "101", "mark": "100"}


class Source:
    known_pairs = ("BTC/USDT:PERP",)

    def _instrument(self, symbol):
        return "BTC_USDT_Perp" if symbol in self.known_pairs else None

    def get_ticker(self, symbol):
        return {"instrument": "BTC_USDT_Perp", "event_time_ns": state["now"],
                "mark_price": state["mark"], "bid": state["bid"], "ask": state["ask"]}

    def get_margin_rules(self, symbol):
        return {"instrument": "BTC_USDT_Perp", "fetched_at_ns": state["now"],
                "max_position_size": "1000", "risk_brackets": [
                    {"tier": 1, "notional_floor": "0", "notional_cap": "600000",
                     "maintenance_margin_rate": "0.01", "initial_margin_rate": "0.02",
                     "max_leverage": 50, "cumulative_maintenance_amount": "0"}]}

    def get_instrument_spec(self, symbol):
        return {"instrument": "BTC_USDT_Perp", "kind": "PERPETUAL", "venues": ["ORDERBOOK"],
                "base_decimals": 3, "tick_size": "0.01", "min_size": "0.01",
                "min_notional": "20", "max_position_size": "1000",
                "fetched_at_ns": state["now"]}

    def get_funding_history(self, symbol, opened_at_ns, end_ns):
        hour_ns = 3_600_000_000_000
        anchor = opened_at_ns // hour_ns * hour_ns
        return [{"instrument": "BTC_USDT_Perp", "funding_time": str(timestamp),
                 "funding_rate": "0.01", "mark_price": "100",
                 "funding_interval_hours": 1}
                for timestamp in range(end_ns // hour_ns * hour_ns, anchor - 1, -hour_ns)]


app.include_router(create_paper_perp_router(
    Path(os.environ["ORBIS_PAPER_PERP_PATH"]), adapter_factory=Source,
    clock_ns=lambda: state["now"]))
app.include_router(create_signal_monitor_router(
    Path(os.environ["ORBIS_PAPER_PERP_PATH"]), Path(os.environ["ORBIS_LAB_PATH"]),
    adapter_factory=Source, clock_ns=lambda: state["now"]))


@app.post("/__test/quote")
def set_quote(body: dict = Body(...)):
    state["now"] += 1_000_000_000
    for key in ("bid", "ask", "mark"):
        if key in body:
            state[key] = str(body[key])
    return dict(state)


@app.post("/__test/advance")
def advance_clock(body: dict = Body(...)):
    hours = body.get("hours")
    seconds = body.get("seconds")
    if type(hours) is int and 1 <= hours <= 24 and seconds is None:
        state["now"] += hours * 3_600_000_000_000
    elif type(seconds) is int and 1 <= seconds <= 60 and hours is None:
        state["now"] += seconds * 1_000_000_000
    else:
        return {"error": "Elige hours 1..24 o seconds 1..60"}
    return dict(state)
