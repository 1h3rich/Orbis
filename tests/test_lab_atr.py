"""ATR causal y reproducible para reglas LAB y señales PAPER."""

from decimal import Decimal, localcontext

import pytest

from app.lab.engine import (Candle, IndicatorCache, LabError, atr, evaluate,
                            run_backtest, validate_candles, validate_strategy)
from app.lab.store import LabStore
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.signal_monitor import SignalMonitor


def bars():
    # True range inicial: 4, 6, 5, 9 (incluidos saltos respecto al cierre previo).
    values = [
        (10, 12, 8, 10), (13, 16, 12, 15), (16, 18, 13, 17),
        (21, 26, 18, 22), (20, 23, 17, 18), (18, 21, 14, 16),
    ]
    return [Candle(1_735_689_600 + i * 3600, *map(Decimal, row), Decimal("10"))
            for i, row in enumerate(values)]


def strategy(rule):
    return validate_strategy({"direction": "LONG", "initial_capital": "1000",
                              "allocation": "0.1", "fee_rate": "0", "slippage_rate": "0",
                              "stop_loss": "0.05", "take_profit": "0.1", "entry": rule})


def test_wilder_atr_percentage_uses_only_closed_prefix():
    candles = bars()
    cache = IndicatorCache(candles)
    assert atr(candles, 1, 3) is None
    assert cache.value("ATR", 3, 1) is None
    assert atr(candles, 2, 3) == Decimal("5")
    assert cache.value("ATR", 3, 2) == Decimal("5")
    with localcontext() as ctx:
        ctx.prec = 80
        expected = Decimal(19) / Decimal(3)
        expected_pct = expected / candles[3].c * 100
    assert atr(candles, 3, 3) == expected
    assert cache.value("ATR", 3, 3) == atr(candles, 3, 3)
    rule = strategy({"kind": "ATR_PCT_GT", "period": 3, "threshold": "25"})["entry"]
    matched, explanation = evaluate(rule, candles, 3, cache)
    assert matched is True
    assert explanation["observed"] == str(expected_pct)
    assert explanation["threshold"] == "25"
    changed = candles.copy()
    changed[4] = Candle(candles[4].t, Decimal("1"), Decimal("100"), Decimal("1"), Decimal("50"), Decimal("10"))
    assert evaluate(rule, changed, 3) == evaluate(rule, candles, 3)


def test_atr_rule_lower_threshold_and_backtest_are_deterministic():
    candles = bars()
    condition = {"op": "AND", "children": [
        {"kind": "ATR_PCT_GT", "period": 3, "threshold": "20"},
        {"kind": "ATR_PCT_LT", "period": 3, "threshold": "40"},
    ]}
    definition = strategy(condition)
    result = run_backtest(candles, definition)
    assert result == run_backtest(candles, definition)
    assert result["events"]
    assert result["events"][0]["signal_time"] == candles[2].t
    with localcontext() as ctx:
        ctx.prec = 12
        observed = evaluate(definition["entry"], candles, 4)
    with localcontext() as ctx:
        ctx.prec = 50
        assert evaluate(definition["entry"], candles, 4) == observed


@pytest.mark.parametrize("rule", [
    {"kind": "ATR_PCT_GT", "period": 14, "threshold": "0"},
    {"kind": "ATR_PCT_LT", "period": 14, "threshold": "1001"},
    {"kind": "ATR_PCT_GT", "period": 1, "threshold": "2"},
    {"kind": "ATR_PCT_LT", "period": 14, "threshold": "NaN"},
])
def test_atr_rejects_invalid_parameters(rule):
    with pytest.raises(LabError):
        strategy(rule)


def test_atr_strategy_is_evaluated_by_same_paper_signal_engine(tmp_path):
    step = 3600
    start = 1_735_689_600
    source_rows = [{"t": start + index * step, "o": str(bar.o), "h": str(bar.h),
                    "l": str(bar.l), "c": str(bar.c), "v": str(bar.v)}
                   for index, bar in enumerate(bars())]
    state = {"now": start + 5 * step + 10}

    class Source:
        def get_historical_candles(self, symbol, timeframe, first, end, *, now):
            assert symbol == "BTC/USDT:PERP" and timeframe == "1h"
            return [row for row in source_rows if first <= row["t"] < end]

    paper = PaperPerpStore(tmp_path / "paper.db")
    account = paper.create_account("ATR PAPER", "1000", "500", "800", "5", 2, "0", "0")
    lab = LabStore(tmp_path / "lab.db")
    seed, gaps = validate_candles(source_rows[:5], "1h", now=state["now"])
    assert not gaps
    dataset = lab.save_dataset("BTC/USDT:PERP", "1h", "grvt-perp-trade-public",
                               seed, [], verified_public=True)
    version = lab.create_strategy("ATR causal", strategy({"kind": "ATR_PCT_GT", "period": 3,
                                                           "threshold": "10"}))
    monitor = SignalMonitor(paper.path, lab.path, adapter_factory=Source,
                            clock_ns=lambda: state["now"] * 1_000_000_000)
    session = monitor.create(account["id"], version["version_id"], dataset["id"])
    state["now"] = start + 6 * step + 1
    observed = monitor.poll(session["id"])
    assert observed["poll_result"] == "OBSERVED"
    event = observed["observations"][0]
    assert event["entry_match"] is True
    assert event["evidence"]["entry"]["kind"] == "ATR_PCT_GT"
    assert event["evidence"]["entry"]["observed"] is not None
    assert paper.account(account["id"])["open_positions"] == 0
