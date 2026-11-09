"""Los motores directos no convierten saltos temporales en horizontes ficticios."""

import pytest

from app.lab.engine import LabError, run_backtest, validate_candles, validate_strategy
from app.lab.research import run_research


START = 1_735_689_600
HOUR = 3_600


def bars(indices):
    rows = [{"t": START + index * HOUR, "o": str(100 + index),
             "h": str(102 + index), "l": str(99 + index),
             "c": str(101 + index), "v": "10"} for index in indices]
    return validate_candles(rows, "1h", now=START + 20 * HOUR)


STRATEGY = validate_strategy({
    "direction": "LONG", "initial_capital": "1000", "allocation": "0.1",
    "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.05",
    "take_profit": "0.1", "entry": {"kind": "CLOSE_ABOVE_SMA", "period": 2},
})
RULE = {"kind": "CLOSE_ABOVE_SMA", "period": 2}


@pytest.mark.parametrize("indices", [(0, 1, 3, 4), (0, 1, 1, 2)])
def test_direct_backtest_rejects_gaps_and_duplicate_times(indices):
    if indices[2] == 1:
        # La ruta pública de importación rechaza duplicados; el motor directo
        # también debe protegerse de objetos Candle suministrados por código.
        continuous, _ = bars((0, 1, 2, 3))
        data = [continuous[0], continuous[1], continuous[1], continuous[2]]
    else:
        data, gaps = bars(indices)
        assert gaps[0]["missing"] == 1
    with pytest.raises(LabError, match="huecos o velas desordenadas"):
        run_backtest(data, STRATEGY, "1h")


def test_direct_research_segments_gap_without_crossing_horizons_or_indicator_warmup():
    data, gaps = bars((0, 1, 2, 4, 5, 6))
    assert gaps[0]["missing"] == 1
    result = run_research(data, RULE, "1h")
    assert result["summary"]["coverage"]["segments"] == 2
    assert result["summary"]["coverage"]["missing_bars"] == 1
    assert [event["signal_time"] for event in result["events"]] == [data[1].t, data[2].t, data[4].t, data[5].t]
    assert result["events"][1]["outcomes_pct"]["3600"] is None
    assert result["events"][2]["outcomes_pct"]["3600"] is not None
    assert result["events"][1]["followup_end_time"] is None
    assert result["summary"]["horizons"]["3600"]["count"] == 2
    with pytest.raises(LabError, match="huecos o velas desordenadas"):
        run_research(data, RULE, "1h", engine_version="research-1.0")


def test_direct_research_still_rejects_duplicate_timestamps():
    data, _ = bars((0, 1, 2, 3))
    with pytest.raises(LabError, match="duplicadas"):
        run_research([data[0], data[1], data[1], data[2]], RULE, "1h")


def test_contiguous_history_keeps_existing_engine_results():
    data, gaps = bars((0, 1, 2, 3, 4))
    assert gaps == []
    assert run_backtest(data, STRATEGY, "1h")["events"]
    assert run_research(data, RULE, "1h")["summary"]["cases"] > 0

