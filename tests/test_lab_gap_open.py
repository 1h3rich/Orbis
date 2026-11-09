"""La apertura conocida precede a los extremos intravela; runs viejos se repiten."""

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.engine import Candle, run_backtest, validate_strategy
from app.lab.optimization import optimize_stops
from app.lab.store import LabStore
from app.lab.validation import VALIDATION_ENGINE_VERSION, chronological_holdout
from app.lab.verification import verify_backtest_run


START = 1_735_689_600
STEP = 3_600


def scenario(direction: str, opening: str) -> tuple[list[Candle], dict]:
    first, signal = (("100", "110") if direction == "LONG" else ("100", "90"))
    bars = [
        Candle(START, Decimal(first), Decimal(first), Decimal(first), Decimal(first), Decimal(10)),
        Candle(START + STEP, Decimal(signal), Decimal(signal), Decimal(signal), Decimal(signal), Decimal(10)),
        Candle(START + 2 * STEP, Decimal(100), Decimal(100), Decimal(100), Decimal(100), Decimal(10)),
        Candle(START + 3 * STEP, Decimal(opening), Decimal(130), Decimal(70), Decimal(100), Decimal(10)),
    ]
    rule = "CLOSE_ABOVE_SMA" if direction == "LONG" else "CLOSE_BELOW_SMA"
    strategy = validate_strategy({
        "direction": direction, "initial_capital": "1000", "allocation": "1",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
        "take_profit": "0.2", "entry": {"kind": rule, "period": 2},
    })
    return bars, strategy


@pytest.mark.parametrize(("direction", "opening", "target"), [
    ("LONG", "125", "120"),
    ("SHORT", "75", "80"),
])
def test_target_already_crossed_at_open_precedes_intrabar_stop(
        direction, opening, target):
    bars, strategy = scenario(direction, opening)
    current = run_backtest(bars, strategy, engine_version="lab-1.10")
    previous = run_backtest(bars, strategy, engine_version="lab-1.9")
    assert current["trades"][0]["reason"] == "TARGET_OPEN"
    assert Decimal(current["trades"][0]["exit_price"]) == Decimal(target)
    assert current["metrics"]["ambiguous_exits"] == 0
    assert current["metrics"]["exposure_pct"] == "50.0"
    assert previous["trades"][0]["reason"] == "STOP_AND_TARGET_AMBIGUOUS"
    assert previous["metrics"]["ambiguous_exits"] == 1


@pytest.mark.parametrize(("direction", "opening"), [
    ("LONG", "85"),
    ("SHORT", "115"),
])
def test_stop_already_crossed_at_open_uses_open_not_intrabar_guess(direction, opening):
    bars, strategy = scenario(direction, opening)
    current = run_backtest(bars, strategy, engine_version="lab-1.10")
    assert current["trades"][0]["reason"] == "STOP"
    assert current["trades"][0]["exit_price"] == opening
    assert current["metrics"]["ambiguous_exits"] == 0
    assert current["metrics"]["exposure_pct"] == "50.0"


def test_intrabar_exit_counts_exposure_but_opening_exit_does_not():
    bars, strategy = scenario("LONG", "100")
    current = run_backtest(bars, strategy)
    previous = run_backtest(bars, strategy, engine_version="lab-1.14")
    assert current["trades"][0]["reason"] == "STOP_AND_TARGET_AMBIGUOUS"
    assert current["metrics"]["exposure_pct"] == previous["metrics"]["exposure_pct"] == "50.0"
    gap_bars, _ = scenario("LONG", "125")
    old_gap = run_backtest(gap_bars, strategy, engine_version="lab-1.14")
    new_gap = run_backtest(gap_bars, strategy)
    assert old_gap["metrics"]["exposure_pct"] == "50.0"
    assert new_gap["metrics"]["exposure_pct"] == "25.00"
    for section in ("events", "exit_events", "trades", "equity", "drawdown"):
        assert new_gap[section] == old_gap[section]
    assert {key: value for key, value in new_gap["metrics"].items() if key != "exposure_pct"} == {
        key: value for key, value in old_gap["metrics"].items() if key != "exposure_pct"}


def test_old_and_new_gap_runs_replay_exactly_after_reopen(tmp_path):
    bars, strategy = scenario("LONG", "125")
    store = LabStore(tmp_path / "lab.db")
    dataset = store.save_dataset("BTC/USD", "1h", "fixture-gap-open", bars, [])
    version = store.create_strategy("Gap de apertura", strategy)
    saved = {}
    for engine in ("lab-1.9", "lab-1.10", "lab-1.14", "lab-1.15"):
        result = run_backtest(bars, strategy, engine_version=engine)
        saved[engine] = store.save_run(dataset["id"], version["version_id"],
                                       result, engine)
    reopened = LabStore(tmp_path / "lab.db")
    for engine, run in saved.items():
        check = verify_backtest_run(reopened, reopened.run(run["id"]))
        assert check["verified"], (engine, check)


def test_gap_semantics_are_pinned_in_saved_validations_and_optimizations(tmp_path):
    pattern, strategy = scenario("LONG", "125")
    bars = [
        Candle(START + i * STEP, source.o, source.h, source.l, source.c, source.v)
        for i in range(200) for source in (pattern[i % len(pattern)],)
    ]
    path = tmp_path / "lab.db"
    store = LabStore(path)
    dataset = store.save_dataset("BTC/USD", "1h", "fixture-gap-open", bars, [])
    version = store.create_strategy("Gap de apertura", strategy)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    for engine in ("lab-1.9", "lab-1.10"):
        validation = chronological_holdout(bars, strategy, "1h",
                                            engine_version=engine)
        saved_validation = store.save_validation(
            dataset["id"], version["version_id"], dataset["hash"],
            validation, VALIDATION_ENGINE_VERSION, lab_engine_version=engine)
        checked = client.get(f"/lab/validations/{saved_validation['id']}/verify")
        assert checked.status_code == 200 and checked.json()["verified"], checked.text
        optimization = optimize_stops(bars, strategy, "1h", ["0.1"], ["0.2"],
                                      engine_version=engine)
        saved_optimization = store.save_optimization(
            dataset["id"], version["version_id"], dataset["hash"],
            optimization, engine)
        checked = client.get(f"/lab/optimizations/{saved_optimization['id']}/verify")
        assert checked.status_code == 200 and checked.json()["verified"], checked.text
