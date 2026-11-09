"""El motor directo no debe fiarse de velas construidas fuera de la API."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.lab.engine import LabError, run_backtest, validate_candles, validate_strategy
from app.lab.research import run_research


START = 1_735_689_600
STEP = 3_600
STRATEGY = validate_strategy({
    "direction": "LONG", "initial_capital": "1000", "allocation": "0.5",
    "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.05",
    "take_profit": "0.1", "entry": {"kind": "CLOSE_ABOVE_SMA", "period": 2},
})


def candles():
    rows = [{"t": START + index * STEP, "o": str(100 + index),
             "h": str(102 + index), "l": str(99 + index),
             "c": str(101 + index), "v": "10"} for index in range(4)]
    return validate_candles(rows, "1h", now=START + 10 * STEP)[0]


@pytest.mark.parametrize("change", [
    {"t": True}, {"t": START + 1}, {"t": -STEP},
    {"o": Decimal("NaN")}, {"c": Decimal("Infinity")},
    {"l": Decimal("0")}, {"h": Decimal("100")},
    {"v": Decimal("-1")}, {"v": 2.5},
])
@pytest.mark.parametrize("run", [
    lambda bars: run_backtest(bars, STRATEGY, "1h"),
    lambda bars: run_research(bars, STRATEGY["entry"], "1h"),
])
def test_direct_engines_reject_invalid_candle_without_result(change, run):
    bars = candles()
    bars[2] = replace(bars[2], **change)
    with pytest.raises(LabError, match="vela|OHLC|volumen"):
        run(bars)


def test_direct_backtest_keeps_valid_history_deterministic():
    bars = candles()
    assert run_backtest(bars, STRATEGY, "1h") == run_backtest(bars, STRATEGY, "1h")
    assert run_research(bars, STRATEGY["entry"], "1h") == run_research(bars, STRATEGY["entry"], "1h")
