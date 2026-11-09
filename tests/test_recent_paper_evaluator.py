"""Paridad de reglas base con una ventana de 512 velas tras 10000 observaciones."""

from decimal import Decimal

import pytest

from app.lab.engine import (CURRENT_ENGINE_VERSION, Candle, IndicatorCache,
                            LabError, evaluate, validate_strategy)
from app.lab.paper_indicator_state import build_paper_indicator_state
from app.lab.recent_paper_evaluator import (RECENT_BARS, evaluate_recent,
                                            recent_supported)


def history(count: int) -> list[Candle]:
    bars = []
    for index in range(count):
        close = Decimal(100) + Decimal((index * 17) % 43) / 10 + Decimal(index) / 1000
        open_price = close + (Decimal(1) if index % 4 else Decimal(-1))
        high = max(close, open_price) + Decimal(2 + index % 3)
        low = min(close, open_price) - Decimal(2 + index % 5)
        bars.append(Candle(index * 3600, open_price, high, low, close,
                           Decimal(10 + (index * 7) % 19)))
    return bars


RULES = [
    {"kind": "RSI_LT", "period": 14, "threshold": "40"},
    {"kind": "RSI_GT", "period": 14, "threshold": "60"},
    {"kind": "CLOSE_ABOVE_EMA", "period": 200},
    {"kind": "CLOSE_BELOW_EMA", "period": 50},
    {"kind": "EMA_CROSS_ABOVE_EMA", "period": 20, "slow_period": 50},
    {"kind": "EMA_CROSS_BELOW_EMA", "period": 12, "slow_period": 26},
    {"kind": "CLOSE_ABOVE_SMA", "period": 300},
    {"kind": "CLOSE_BELOW_SMA", "period": 20},
    {"kind": "CLOSE_ABOVE_BB_UPPER", "period": 20, "multiplier": "2"},
    {"kind": "CLOSE_BELOW_BB_LOWER", "period": 20, "multiplier": "2"},
    {"kind": "MACD_CROSS_ABOVE_SIGNAL", "period": 12, "slow_period": 26, "signal_period": 9},
    {"kind": "MACD_CROSS_BELOW_SIGNAL", "period": 5, "slow_period": 35, "signal_period": 5},
    {"kind": "VOLUME_ABOVE_AVG", "period": 40, "factor": "1.4"},
    {"kind": "ATR_PCT_GT", "period": 14, "threshold": "2"},
    {"kind": "ATR_PCT_LT", "period": 20, "threshold": "8"},
    {"kind": "RETEST_ABOVE_RANGE", "period": 30, "retest_bars": 7, "tolerance_pct": "0.01"},
    {"kind": "RETEST_BELOW_RANGE", "period": 30, "retest_bars": 7, "tolerance_pct": "0.01"},
    {"kind": "BEARISH_STREAK", "period": 4},
    {"kind": "BULLISH_STREAK", "period": 4},
    {"kind": "VOLUME_RISING_STREAK", "period": 4},
    {"kind": "VOLUME_ASCENDING_BARS", "period": 4},
    {"kind": "STRUCTURE_HH_HL", "period": 90, "pivot_bars": 3},
    {"kind": "STRUCTURE_LH_LL", "period": 90, "pivot_bars": 3},
]


@pytest.mark.parametrize("end", [512, 513, 1000, 5001, 10050])
def test_every_base_rule_matches_full_history_at_window_boundaries(end):
    bars = history(end)
    definition = validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
        "fee_rate": "0.001", "slippage_rate": "0", "stop_loss": "0.03",
        "take_profit": "0.08", "entry": {"op": "AND", "children": RULES[:8]},
        "exit": {"op": "OR", "children": RULES[8:16]}})
    # Seguir también el resto de condiciones al construir la instantánea.
    tracked = {**definition, "entry": {"op": "AND", "children": RULES[:8]},
               "exit": {"op": "OR", "children": RULES[8:]}}
    snapshot = build_paper_indicator_state(tracked, [bar.json() for bar in bars], 80)
    recent = bars[-RECENT_BARS:]
    full_cache = IndicatorCache(bars)
    for rule in RULES:
        expected = evaluate(rule, bars, len(bars) - 1, full_cache, base_timeframe="1h")
        actual = evaluate_recent(rule, recent, snapshot,
                                 engine_version=CURRENT_ENGINE_VERSION, base_timeframe="1h")
        assert actual == expected, rule["kind"]


def test_recent_evaluator_rejects_legacy_and_higher_timeframe_rules():
    base = {"entry": {"kind": "RSI_LT", "period": 14, "threshold": "40"}}
    assert recent_supported(base, CURRENT_ENGINE_VERSION)
    assert not recent_supported(base, "lab-1.15")
    multi = {"entry": {**base["entry"], "timeframe": "4h"}}
    assert not recent_supported(multi, CURRENT_ENGINE_VERSION)
    bars = history(RECENT_BARS)
    snapshot = build_paper_indicator_state(base, [bar.json() for bar in bars], 80)
    with pytest.raises(LabError, match="legado"):
        evaluate_recent(base["entry"], bars, snapshot,
                        engine_version="lab-1.15", base_timeframe="1h")
    with pytest.raises(LabError, match="multitemporal"):
        evaluate_recent(multi["entry"], bars, snapshot,
                        engine_version=CURRENT_ENGINE_VERSION, base_timeframe="1h")
