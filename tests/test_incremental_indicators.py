"""Paridad exacta y recuperación de EMA, MACD y ATR incrementales."""

import json
from decimal import Decimal

import pytest

from app.lab.engine import Candle, IndicatorCache, LabError
from app.lab.incremental_indicators import AtrState, EmaState, MacdState


def bars(count: int) -> list[Candle]:
    result = []
    for index in range(count):
        close = Decimal(100) + Decimal((index * 29) % 37) / 10 + Decimal(index) / 1000
        result.append(Candle(index * 3600, close - 1, close + 2, close - 3,
                             close, Decimal(10 + index % 9)))
    return result


@pytest.mark.parametrize("precision", [28, 80])
def test_ema_incremental_matches_causal_engine_before_and_after_restore(precision):
    candles = bars(10_050)
    reference = IndicatorCache(candles, precision=precision)
    state = EmaState(200, precision)
    for index, bar in enumerate(candles):
        assert state.push(bar.c) == reference.value("EMA", 200, index)
        if index == 5_004:
            state = EmaState.restore(json.loads(json.dumps(state.snapshot())))
        if index > 0:
            assert state.previous == reference.value("EMA", 200, index - 1)


@pytest.mark.parametrize("precision", [28, 80])
def test_macd_incremental_matches_causal_engine_before_and_after_restore(precision):
    candles = bars(10_050)
    reference = IndicatorCache(candles, precision=precision)
    state = MacdState.create(12, 26, 9, precision=precision)
    for index, bar in enumerate(candles):
        assert state.push(bar.c) == reference.macd(12, 26, 9, index)
        if index == 5_004:
            state = MacdState.restore(json.loads(json.dumps(state.snapshot())))
        if index > 0:
            assert state.previous_line == reference.macd(12, 26, 9, index - 1)[0]
            assert state.previous_signal == reference.macd(12, 26, 9, index - 1)[1]


@pytest.mark.parametrize("precision", [28, 80])
def test_atr_incremental_matches_causal_engine_before_and_after_restore(precision):
    candles = bars(10_050)
    reference = IndicatorCache(candles, precision=precision)
    state = AtrState(14, precision)
    for index, bar in enumerate(candles):
        assert state.push(bar) == reference.value("ATR", 14, index)
        if index == 5_004:
            state = AtrState.restore(json.loads(json.dumps(state.snapshot())))


def test_incremental_snapshots_reject_nonfinite_and_incoherent_values():
    state = EmaState(3)
    snapshot = state.snapshot()
    snapshot["seed_sum"] = "NaN"
    with pytest.raises(LabError, match="no finito"):
        EmaState.restore(snapshot)
    snapshot = AtrState(3).snapshot()
    snapshot["count"] = 3
    with pytest.raises(LabError, match="incoherente"):
        AtrState.restore(snapshot)
