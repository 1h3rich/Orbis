"""Causal divergence, audit evidence and parity with incoming PAPER candles."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.coverage import higher_timeframe_coverage
from app.lab.engine import (Candle, CURRENT_ENGINE_VERSION, IndicatorCache, LabError,
                            evaluate, run_backtest, supports_engine_version, validate_rule, validate_strategy)
from app.lab.recent_paper_evaluator import RECENT_BARS, evaluate_recent
from app.lab.research import run_research
from app.lab.store import LabStore

START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 3600
BULL = "RSI_BULLISH_DIVERGENCE"
BEAR = "RSI_BEARISH_DIVERGENCE"


def divergence_bars(*, bearish=False):
    closes = [112, 110, 105, 102, 100, 101, 104, 106, 109, 102, 103, 105,
              104, 106, 107, 108, 109, 110, 110, 111, 112, 113]
    bars = []
    for i, close in enumerate(closes):
        high, low = D(121 if i == 8 else 115), D({4: 90, 12: 85}.get(i, 98))
        opened, close = D(close), D(close)
        if bearish:
            high, low, opened, close = 220 - low, 220 - high, 220 - opened, 220 - close
        bars.append(Candle(START + i * STEP, opened, high, low, close, D(1)))
    return bars


def rule(kind=BULL, **overrides):
    return validate_rule({"kind": kind, "period": 3, "pivot_bars": 2, "lookback_bars": 8, **overrides})


def strategy(kind=BULL, **overrides):
    return validate_strategy({"direction": "LONG" if kind == BULL else "SHORT",
                              "initial_capital": "1000", "allocation": "0.5",
                              "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.9",
                              "take_profit": "9", "max_holding_bars": 2, "entry": rule(kind, **overrides)})


@pytest.mark.parametrize(("kind", "bearish", "price", "rsi"), [
    (BULL, False, ["90", "85"], ["0", "75"]),
    (BEAR, True, ["130", "135"], ["100", "25"]),
])
def test_confirmed_divergence_once_and_same_evidence_with_no_future(kind, bearish, price, rsi):
    bars = divergence_bars(bearish=bearish)
    definition, cache = rule(kind), IndicatorCache(bars)
    observations = []
    for i in range(len(bars)):
        full = evaluate(definition, bars, i, cache, base_timeframe="1h")
        prefix = evaluate(definition, bars[:i + 1], i, base_timeframe="1h")
        assert full == prefix
        if full[0]:
            observations.append((i, full[1]))
    assert len(observations) == 1
    i, proof = observations[0]
    assert i == 14
    assert [(pivot["t"], pivot["price"]) for pivot in proof["pivots"]] == [
        (bars[at].t, value) for at, value in zip((4, 12), price)]
    assert [D(pivot["rsi"]).quantize(D("0.000001")) for pivot in proof["pivots"]] == [D(value) for value in rsi]
    assert proof["intervening_pivot_t"] == bars[8].t
    assert proof["confirmed_at"] == bars[14].t
    assert proof["confirmation_close_t"] == bars[15].t
    assert evaluate(definition, bars[:15] + list(reversed(bars[15:])), 14, base_timeframe="1h") == (True, proof)

    sim = run_backtest(bars, strategy(kind))
    assert sim["events"][0]["signal_time"] == bars[14].t
    assert sim["events"][0]["explanation"] == proof
    assert sim["trades"][0]["entry_time"] == bars[15].t
    assert sim["trades"][0]["entry_price"] == str(bars[15].o)
    research = run_research(bars, definition, "1h")
    assert [row["explanation"] for row in research["events"]] == [proof]
    assert not supports_engine_version("lab-1.15", strategy(kind))
    assert supports_engine_version(CURRENT_ENGINE_VERSION, strategy(kind))
    with pytest.raises(LabError, match="incompatible"):
        run_research(bars, definition, "1h", engine_version="research-1.1")


def test_rejects_missing_intermediate_pivot_equal_prices_expired_distance_and_gap():
    bars = divergence_bars()
    without_intermediate = bars.copy()
    without_intermediate[8] = replace(bars[8], h=D(115))
    assert evaluate(rule(), without_intermediate, 14, base_timeframe="1h")[1]["reason"] == "NO_INTERVENING_PIVOT"
    equal = bars.copy()
    equal[12] = replace(bars[12], l=bars[4].l)
    assert evaluate(rule(), equal, 14, base_timeframe="1h")[1]["reason"] == "NO_DIVERGENCE"
    equal_rsi = bars.copy()
    for i, close in ((10, 101), (11, 100), (12, 99)):
        equal_rsi[i] = replace(equal_rsi[i], o=D(close), c=D(close))
    matched, evidence = evaluate(rule(), equal_rsi, 14, base_timeframe="1h")
    assert not matched and evidence["reason"] == "NO_DIVERGENCE"
    assert all(D(pivot["rsi"]) == 0 for pivot in evidence["pivots"])
    assert evaluate(rule(lookback_bars=7), bars, 14, base_timeframe="1h")[1]["reason"] == "NO_PRIOR_PIVOT"
    assert evaluate(rule(period=5), bars, 14, base_timeframe="1h")[1]["reason"] == "RSI_WARMUP"
    with_gap = [bar if i < 6 else replace(bar, t=bar.t + STEP) for i, bar in enumerate(bars)]
    assert evaluate(rule(), with_gap, 14, base_timeframe="1h")[1]["reason"] == "CANDLE_GAP"
    assert run_research(with_gap, rule(), "1h")["summary"]["cases"] == 0


def test_higher_timeframe_signal_only_at_final_lower_close_and_never_across_missing_bucket():
    bars = divergence_bars()
    lower = [replace(bar, t=bar.t + 900 * offset, v=D("0.25"))
             for bar in bars for offset in range(4)]
    condition = rule(timeframe="1h")
    cache = IndicatorCache(lower)
    results = [i for i in range(len(lower))
               if evaluate(condition, lower, i, cache, base_timeframe="15m")[0]]
    assert results == [59]
    match, proof = evaluate(condition, lower, 59, cache, base_timeframe="15m")
    assert match and proof["timeframe"] == "1h" and proof["higher_candle_t"] == bars[14].t
    assert proof["confirmation_close_t"] == lower[59].t + 900
    assert evaluate(condition, lower, 60, cache, base_timeframe="15m")[1]["reason"] == "NO_NEW_HIGHER_CLOSE"
    for missing_index in (25, 58):
        broken = lower[:missing_index] + lower[missing_index + 1:]
        current = next(i for i, bar in enumerate(broken) if bar.t == lower[59].t)
        assert not evaluate(condition, broken, current, base_timeframe="15m")[0]
    assert [row["signal_time"] for row in run_research(lower, condition, "15m")["events"]] == [lower[59].t]
    coverage = higher_timeframe_coverage(lower, strategy(timeframe="1h"), "15m")
    assert coverage["rules"][0]["minimum_bars"] == 9


def test_recent_paper_window_covers_maximum_supported_divergence_parameters():
    radius, period, distance = 10, 300, 200
    latest = 711
    second, first = latest - radius, latest - radius - distance
    rows = [Candle(START + i * STEP, D(100), D(105), D(95), D(100), D(1))
            for i in range(latest + 1)]
    rows[first] = replace(rows[first], o=D(99), l=D(90), c=D(99))
    rows[second] = replace(rows[second], o=D(102), l=D(85), c=D(102))
    rows[first + 100] = replace(rows[first + 100], h=D(110))
    condition = rule(period=period, pivot_bars=radius, lookback_bars=distance)
    expected = evaluate(condition, rows, latest, base_timeframe="1h")
    assert expected[0]
    recent = rows[-RECENT_BARS:]
    assert evaluate_recent(condition, recent, {"last_t": recent[-1].t},
                           engine_version=CURRENT_ENGINE_VERSION, base_timeframe="1h") == expected


def test_paper_uses_same_evidence_after_new_confirmation(tmp_path):
    bars = divergence_bars()
    state = {"now": bars[14].t + 10}

    class Source:
        def get_candles(self, symbol, timeframe="1h", limit=3, since=None, before=None):
            assert symbol == "BTC/USDT" and timeframe == "1h"
            return [bar.json() for bar in bars if bar.t >= since][:limit]

        def get_paper_quote(self, symbol):
            return {"symbol": symbol, "source": "mexc-spot-public",
                    "event_time_ns": state["now"] * 1_000_000_000,
                    "bid": "107", "ask": "108", "last": "107"}

    lab = LabStore(tmp_path / "lab.db")
    dataset = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars[:14], [], verified_public=True)
    version = lab.create_strategy("Divergencia PAPER", strategy())
    paper = SpotStrategyPaper(tmp_path / "paper.db", lab.path,
                              adapter_factory=Source, clock_ns=lambda: state["now"] * 1_000_000_000)
    session = paper.create(version["version_id"], dataset["id"])
    state["now"] = bars[15].t + 1
    observed = paper.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    event = observed["events"][0]
    assert event["entry_match"] and event["entry"] == run_backtest(bars, strategy())["events"][0]["explanation"]
    assert event["action"]["side"] == "BUY"
    assert observed["execution"].endswith("NO_REAL_ORDERS")


@pytest.mark.parametrize("extra", [{"pivot_bars": True}, {"lookback_bars": 2}, {"lookback_bars": 201},
                                   {"period": 301}, {"threshold": "30"}])
def test_invalid_divergence_parameters_rejected(extra):
    with pytest.raises(LabError):
        rule(**extra)
