"""Cruce real entre EMA rápida y lenta, compartido por LAB y PAPER."""

from decimal import Decimal

import pytest

from app.lab.api import ENGINE_VERSION
from app.lab.engine import (Candle, IndicatorCache, LabError, evaluate,
                            run_backtest, supports_engine_version,
                            validate_rule, validate_strategy)


START = 1735689600
HOUR = 3600
PRICES = (10, 10, 10, 9, 8, 9, 10, 11, 10, 9, 8)


def candles(prices=PRICES):
    return [Candle(START + i * HOUR, Decimal(price), Decimal(price + 1),
                   Decimal(price - 1), Decimal(price), Decimal(1))
            for i, price in enumerate(prices)]


def rule(kind):
    return validate_rule({"kind": kind, "period": 2, "slow_period": 3})


def test_cross_is_single_transition_not_two_similar_candles():
    bars = candles()
    up = rule("EMA_CROSS_ABOVE_EMA")
    down = rule("EMA_CROSS_BELOW_EMA")
    assert [i for i in range(len(bars)) if evaluate(up, bars, i)[0]] == [6]
    assert [i for i in range(len(bars)) if evaluate(down, bars, i)[0]] == [3, 9]
    matched, evidence = evaluate(up, bars, 6)
    assert matched is True
    assert Decimal(evidence["previous_observed"]) <= Decimal(evidence["previous_threshold"])
    assert Decimal(evidence["observed"]) > Decimal(evidence["threshold"])
    assert evaluate(up, bars, 7)[0] is False  # seguir por encima no es otro cruce


def test_cross_is_causal_and_cache_matches_uncached_prefix():
    bars = candles()
    for kind in ("EMA_CROSS_ABOVE_EMA", "EMA_CROSS_BELOW_EMA"):
        selected = rule(kind)
        cache = IndicatorCache(bars)
        for i in range(len(bars)):
            expected = evaluate(selected, bars[:i + 1], i)
            assert evaluate(selected, bars, i, cache) == expected
        revised_future = bars[:7] + candles((500, 600, 700, 800))[0:4]
        for i in range(7):
            assert evaluate(selected, revised_future, i) == evaluate(selected, bars, i)


@pytest.mark.parametrize("slow", [2, 1, 301, True, "26", None])
def test_cross_requires_integer_slow_period_greater_than_fast(slow):
    with pytest.raises(LabError, match="rápida < lenta"):
        validate_rule({"kind": "EMA_CROSS_ABOVE_EMA", "period": 2, "slow_period": slow})


def test_version_and_next_open_backtest_marker():
    selected = rule("EMA_CROSS_ABOVE_EMA")
    definition = validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
        "take_profit": "0.2", "entry": selected,
    })
    assert ENGINE_VERSION == "lab-1.16"
    assert supports_engine_version("lab-1.9", definition)
    assert not supports_engine_version("lab-1.8", definition)
    assert supports_engine_version("lab-1.8", {**definition, "entry": validate_rule({
        "kind": "CLOSE_ABOVE_EMA", "period": 2,
    })})
    result = run_backtest(candles(), definition)
    assert result["events"][0]["signal_time"] == START + 6 * HOUR
    assert result["events"][0]["entry_time"] == START + 7 * HOUR
