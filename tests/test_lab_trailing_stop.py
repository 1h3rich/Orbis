"""El stop móvil LAB usa sólo extremos conocidos al cerrar velas anteriores."""

from decimal import Decimal

import pytest

from app.lab.engine import (CURRENT_ENGINE_VERSION, Candle, LabError, run_backtest,
                            supports_engine_version, validate_strategy)
from app.lab.store import LabStore
from app.lab.verification import verify_backtest_run


START = 1_735_689_600
STEP = 3_600
D = Decimal


def scenario(direction: str, *, next_open: str | None = None,
             ambiguous: bool = False) -> tuple[list[Candle], dict]:
    long = direction == "LONG"
    opening = next_open or ("110" if long else "85")
    rows = [
        ("100", "100", "100", "100"),
        (("110", "110", "110", "110") if long else ("90", "90", "90", "90")),
        (("100", "120", "95", "110") if long else ("100", "105", "80", "90")),
        ((opening, "160" if ambiguous else "112", str(min(D(opening), D("107"))), "109") if long
         else (opening, "89", "40" if ambiguous else "82", "86")),
    ]
    bars = [Candle(START + i * STEP, *(D(value) for value in row), D(10))
            for i, row in enumerate(rows)]
    rule = "CLOSE_ABOVE_SMA" if long else "CLOSE_BELOW_SMA"
    strategy = validate_strategy({
        "direction": direction, "initial_capital": "1000", "allocation": "1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
        "take_profit": "0.5", "trailing_stop": "0.1",
        "entry": {"kind": rule, "period": 2},
    })
    return bars, strategy


@pytest.mark.parametrize(("direction", "expected"), [("LONG", "108.0"), ("SHORT", "88.0")])
def test_trailing_stop_uses_previous_closed_bar_only(direction, expected):
    bars, strategy = scenario(direction)
    result = run_backtest(bars, strategy)
    trade = result["trades"][0]
    assert trade["entry_time"] == bars[2].t
    assert trade["exit_time"] == bars[3].t
    assert trade["exit_price"] == expected
    assert trade["reason"] == "TRAILING_STOP"
    assert result["metrics"]["ambiguous_exits"] == 0


def test_trailing_stop_gap_uses_open_before_intrabar_range():
    bars, strategy = scenario("LONG", next_open="105")
    result = run_backtest(bars, strategy)
    assert result["trades"][0]["reason"] == "TRAILING_STOP"
    assert result["trades"][0]["exit_price"] == "105"


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_trailing_stop_and_target_same_bar_are_conservatively_ambiguous(direction):
    bars, strategy = scenario(direction, ambiguous=True)
    result = run_backtest(bars, strategy)
    assert result["trades"][0]["reason"] == "TRAILING_STOP_AND_TARGET_AMBIGUOUS"
    assert result["metrics"]["ambiguous_exits"] == 1


def test_trailing_stop_version_and_saved_run_replay(tmp_path):
    bars, strategy = scenario("LONG")
    assert not supports_engine_version("lab-1.13", strategy)
    assert supports_engine_version("lab-1.14", strategy)
    with pytest.raises(LabError, match="Versión de motor"):
        run_backtest(bars, strategy, engine_version="lab-1.13")
    path = tmp_path / "lab.db"
    store = LabStore(path)
    dataset = store.save_dataset("BTC/USD", "1h", "fixture-trailing", bars, [])
    version = store.create_strategy("Stop móvil", strategy)
    saved = store.save_run(dataset["id"], version["version_id"],
                           run_backtest(bars, strategy), CURRENT_ENGINE_VERSION)
    assert verify_backtest_run(LabStore(path), LabStore(path).run(saved["id"]))["verified"]
    previous = store.save_run(dataset["id"], version["version_id"],
                              run_backtest(bars, strategy, engine_version="lab-1.14"), "lab-1.14")
    assert verify_backtest_run(LabStore(path), LabStore(path).run(previous["id"]))["verified"]
    old_strategy = {key: value for key, value in strategy.items() if key != "trailing_stop"}
    assert run_backtest(bars, old_strategy, engine_version="lab-1.13") == run_backtest(
        bars, old_strategy, engine_version="lab-1.14")


@pytest.mark.parametrize("value", ["0", "-0.1", "1", "1.1", "NaN", 0.1])
def test_invalid_trailing_distance_is_rejected(value):
    _, strategy = scenario("LONG")
    with pytest.raises(LabError):
        validate_strategy({**strategy, "trailing_stop": value})
