"""Estructuras HH/HL y LH/LL: giros confirmados, nunca dos velas similares."""

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.api import create_lab_router
from app.lab.engine import (
    CURRENT_ENGINE_VERSION, IndicatorCache, LabError, evaluate, run_backtest,
    supports_engine_version, validate_candles, validate_rule, validate_strategy,
)
from app.lab.store import LabStore


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 3600


def candles(*, ascending=True):
    highs = {3: "105", 9: "110", 15: "115"} if ascending else {
        3: "115", 9: "110", 15: "105"}
    lows = {6: "90", 12: "94", 18: "98"} if ascending else {
        6: "98", 12: "94", 18: "90"}
    rows = []
    for index in range(27):
        base = Decimal("100") + Decimal(index) / 10 * (1 if ascending else -1)
        rows.append({"t": START + index * STEP, "o": str(base), "c": str(base),
                     "h": highs.get(index, str(base + 1)),
                     "l": lows.get(index, str(base - 1)), "v": "10"})
    return validate_candles(rows, "1h", now=START + 30 * STEP)[0]


def rule(kind):
    return validate_rule({"kind": kind, "period": 25, "pivot_bars": 2})


def strategy(kind):
    return validate_strategy({"direction": "LONG" if kind == "STRUCTURE_HH_HL" else "SHORT",
                              "initial_capital": "1000", "allocation": "0.5",
                              "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.9",
                              "take_profit": "9", "max_holding_bars": 2,
                              "entry": {"kind": kind, "period": 25, "pivot_bars": 2}})


@pytest.mark.parametrize("kind,ascending", [
    ("STRUCTURE_HH_HL", True), ("STRUCTURE_LH_LL", False),
])
def test_three_confirmed_highs_and_lows_signal_once_without_future(kind, ascending):
    bars = candles(ascending=ascending)
    definition = rule(kind)
    full_cache = IndicatorCache(bars)
    matches = []
    for index in range(len(bars)):
        full = evaluate(definition, bars, index, full_cache)
        prefix = evaluate(definition, bars[:index + 1], index,
                          IndicatorCache(bars[:index + 1]))
        assert full == prefix
        if full[0]:
            matches.append(index)
    assert matches == [20]
    assert evaluate(definition, bars, 19, full_cache)[0] is False
    assert evaluate(definition, bars, 20, full_cache)[1]["newly_confirmed"] is True
    assert evaluate(definition, bars, 21, full_cache)[0] is False
    changed_future = bars[:21] + list(reversed(bars[21:]))
    assert evaluate(definition, changed_future, 20) == evaluate(definition, bars, 20)


def test_outside_bar_cannot_count_as_both_alternating_turns():
    bars = candles()
    bars[18] = replace(bars[18], h=Decimal("120"))
    matched, detail = evaluate(rule("STRUCTURE_HH_HL"), bars, 20)
    assert matched is False and detail["alternating"] is False
    assert detail["newly_confirmed"] is True


def test_backtest_enters_next_open_persists_and_old_engine_replays(tmp_path):
    bars = candles()
    definition = strategy("STRUCTURE_HH_HL")
    assert CURRENT_ENGINE_VERSION == "lab-1.16"
    assert supports_engine_version("lab-1.11", definition) is False
    assert supports_engine_version("lab-1.11", {**definition,
           "entry": validate_rule({"kind": "CLOSE_ABOVE_EMA", "period": 2})}) is True
    with pytest.raises(LabError, match="Versión de motor"):
        run_backtest(bars, definition, engine_version="lab-1.11")
    result = run_backtest(bars, definition, engine_version=CURRENT_ENGINE_VERSION)
    assert result["events"][0]["signal_time"] == bars[20].t
    assert result["trades"][0]["entry_time"] == bars[21].t
    assert result["trades"][0]["reason"] == "TIME_LIMIT"
    store = LabStore(tmp_path / "lab.db")
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars, [])
    saved = store.create_strategy("Giros confirmados", definition)
    app = FastAPI()
    app.include_router(create_lab_router(store.path))
    client = TestClient(app)
    run = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                         "version_id": saved["version_id"]})
    assert run.status_code == 200, run.text
    assert run.json()["engine_version"] == CURRENT_ENGINE_VERSION
    assert client.get(f"/lab/runs/{run.json()['id']}/verify").json()["status"] == "verified"
    assert LabStore(store.path).run(run.json()["id"])["trades"][0]["entry_time"] == bars[21].t
    bearish = run_backtest(candles(ascending=False), strategy("STRUCTURE_LH_LL"),
                           engine_version=CURRENT_ENGINE_VERSION)
    assert bearish["events"][0]["signal_time"] == bars[20].t
    assert bearish["trades"][0]["entry_time"] == bars[21].t


def test_spot_paper_reads_same_confirmed_structure_on_incoming_candle(tmp_path):
    bars = candles()
    rows = [bar.json() for bar in bars]
    state = {"now": START + 20 * STEP + 10}

    class Source:
        def get_candles(self, symbol, timeframe="1h", limit=3, since=None, before=None):
            assert symbol == "BTC/USDT" and timeframe == "1h"
            return [dict(row) for row in rows if row["t"] >= since][:limit]

        def get_paper_quote(self, symbol):
            return {"symbol": symbol, "source": "mexc-spot-public",
                    "event_time_ns": state["now"] * 1_000_000_000,
                    "bid": "102", "ask": "103", "last": "102"}

    lab = LabStore(tmp_path / "lab.db")
    dataset = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars[:20], [],
                               verified_public=True)
    version = lab.create_strategy("Estructura PAPER", strategy("STRUCTURE_HH_HL"))
    paper = SpotStrategyPaper(tmp_path / "spot-paper.db", lab.path,
                              adapter_factory=Source,
                              clock_ns=lambda: state["now"] * 1_000_000_000)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = START + 21 * STEP + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    event = observed["events"][0]
    assert event["entry_match"] is True
    assert event["entry"]["highs"][-1]["t"] == bars[15].t
    assert event["entry"]["lows"][-1]["t"] == bars[18].t
    assert event["action"]["side"] == "BUY"
    assert observed["execution"].endswith("NO_REAL_ORDERS")


@pytest.mark.parametrize("raw", [
    {"kind": "STRUCTURE_HH_HL", "period": 12, "pivot_bars": 2},
    {"kind": "STRUCTURE_HH_HL", "period": 25, "pivot_bars": True},
    {"kind": "STRUCTURE_LH_LL", "period": 25, "pivot_bars": 11},
    {"kind": "STRUCTURE_LH_LL", "period": 25, "pivot_bars": 2, "future": True},
])
def test_invalid_structure_rules_fail_closed(raw):
    with pytest.raises(LabError):
        validate_rule(raw)
