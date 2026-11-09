"""Salida por permanencia LAB sin alterar la semántica de motores anteriores."""

from datetime import datetime, timezone

import pytest

from app.lab.engine import LabError, run_backtest, supports_engine_version, validate_candles, validate_strategy


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
STEP = 3600


def definition(**extra):
    return {"direction": "LONG", "initial_capital": "1000", "allocation": "1",
            "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.9",
            "take_profit": "9", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2},
            **extra}


def candles():
    rows = [{"t": START + i * STEP, "o": str(10 + i), "h": str(11 + i),
             "l": str(9 + i), "c": str(10 + i), "v": "10"} for i in range(8)]
    return validate_candles(rows, "1h", now=START + 9 * STEP)[0]


def test_time_limit_exits_at_open_after_complete_holding_bars():
    strategy = validate_strategy(definition(max_holding_bars=2))
    result = run_backtest(candles(), strategy, engine_version="lab-1.11")
    first = result["trades"][0]
    assert first["entry_time"] == START + 2 * STEP
    assert first["exit_time"] == START + 4 * STEP
    assert first["exit_price"] == "14"
    assert first["reason"] == "TIME_LIMIT"
    assert first["duration_seconds"] == 2 * STEP
    assert any("permanencia" in item for item in result["assumptions"])
    assert supports_engine_version("lab-1.10", strategy) is False
    with pytest.raises(LabError, match="Versión de motor"):
        run_backtest(candles(), strategy, engine_version="lab-1.10")


def test_time_limit_opening_bar_does_not_inflate_new_exposure_metric():
    strategy = validate_strategy(definition(max_holding_bars=2))
    previous = run_backtest(candles(), strategy, engine_version="lab-1.14")
    current = run_backtest(candles(), strategy)
    assert previous["trades"] == current["trades"]
    assert float(current["metrics"]["exposure_pct"]) < float(previous["metrics"]["exposure_pct"])


def test_old_engine_retains_gap_semantics_without_time_limit():
    strategy = validate_strategy(definition())
    old = run_backtest(candles(), strategy, engine_version="lab-1.10")
    new = run_backtest(candles(), strategy, engine_version="lab-1.11")
    assert old == new


@pytest.mark.parametrize("value", [True, 0, -1, 10001, "2", 1.5])
def test_time_limit_rejects_invalid_values(value):
    with pytest.raises(LabError, match="permanencia"):
        validate_strategy(definition(max_holding_bars=value))
