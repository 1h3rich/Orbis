"""Frecuencia de señales LAB/PAPER sin mezclar rendimiento ni evidencia dudosa."""

from app.lab.signal_frequency import signal_frequency


def test_signal_frequency_uses_evaluated_bars_and_exact_decimal():
    reference = {"verification": {"verified": True}}
    saved = {"events": [{}] * 3, "equity": [{}] * 7}
    result = signal_frequency(reference, saved, {"verified": True},
                              observed_candles=3, entry_signals=1)
    assert result["historical"] == {
        "signals": 3, "evaluated_candles": 7,
        "per_100_candles": "42.857142857142857142857142857142857142857142857142857142857142857142857142857143",
    }
    assert result["paper"] == {
        "signals": 1, "evaluated_candles": 3,
        "per_100_candles": "33.333333333333333333333333333333333333333333333333333333333333333333333333333333",
    }
    assert result["paper_small_sample"] is True
    assert result["historical_small_sample"] is True
    assert result["directly_comparable"] is False
    assert "delta" not in result and "return_pct" not in result


def test_signal_frequency_hides_unverified_and_zero_denominators():
    reference = {"verification": {"verified": False}}
    saved = {"events": [{}], "equity": [{}]}
    result = signal_frequency(reference, saved, {"verified": False},
                              observed_candles=1, entry_signals=1)
    assert result["historical"] is None and result["paper"] is None
    assert result["paper_small_sample"] is False
    assert result["historical_small_sample"] is False
    assert signal_frequency(None, None, {"verified": True},
                            observed_candles=0, entry_signals=0)["paper"] is None
    assert signal_frequency(None, None, {"verified": True},
                            observed_candles=1, entry_signals=2)["paper"] is None


def test_sparse_signals_warn_even_with_many_evaluated_candles():
    reference = {"verification": {"verified": True}}
    saved = {"events": [{}], "equity": [{}] * 1000}
    result = signal_frequency(reference, saved, {"verified": True},
                              observed_candles=1000, entry_signals=1)
    assert result["historical"]["signals"] == 1
    assert result["paper"]["signals"] == 1
    assert result["historical_small_sample"] is True
    assert result["paper_small_sample"] is True

    sufficient = signal_frequency(reference, {"events": [{}] * 30, "equity": [{}] * 1000},
                                  {"verified": True}, observed_candles=1000, entry_signals=30)
    assert sufficient["historical_small_sample"] is False
    assert sufficient["paper_small_sample"] is False

    no_signals = signal_frequency(reference, {"events": [], "equity": [{}] * 1000},
                                  {"verified": True}, observed_candles=1000, entry_signals=0)
    assert no_signals["historical_small_sample"] is True
    assert no_signals["paper_small_sample"] is True


def test_consecutive_signals_are_described_as_one_burst_not_independent_cases():
    reference = {"verification": {"verified": True}}
    times = [1_000 + 60 * index for index in range(30)]
    saved = {"events": [{"signal_time": value} for value in times],
             "equity": [{}] * 1000}
    result = signal_frequency(reference, saved, {"verified": True},
                              observed_candles=1000, entry_signals=30,
                              paper_signal_times=list(reversed(times)), step_seconds=60)
    assert result["historical"]["signal_bursts"] == 1
    assert result["paper"]["signal_bursts"] == 1
    assert result["historical_clustered"] is True
    assert result["paper_clustered"] is True
    assert result["historical_small_sample"] is False
    assert result["paper_small_sample"] is False
    assert result["directly_comparable"] is False


def test_spaced_signals_are_distinct_bursts_but_not_statistical_independence():
    reference = {"verification": {"verified": True}}
    times = [1_000 + 120 * index for index in range(30)]
    saved = {"events": [{"signal_time": value} for value in times],
             "equity": [{}] * 1000}
    result = signal_frequency(reference, saved, {"verified": True},
                              observed_candles=1000, entry_signals=30,
                              paper_signal_times=times, step_seconds=60)
    assert result["historical"]["signal_bursts"] == 30
    assert result["paper"]["signal_bursts"] == 30
    assert result["historical_clustered"] is False
    assert result["paper_clustered"] is False
    assert result["directly_comparable"] is False


def test_invalid_or_unverified_timestamps_never_create_unsupported_bursts():
    reference = {"verification": {"verified": True}}
    saved = {"events": [{"signal_time": 1_000}] * 30,
             "equity": [{}] * 1000}
    result = signal_frequency(reference, saved, {"verified": True},
                              observed_candles=1000, entry_signals=30,
                              paper_signal_times=[1_000] * 30, step_seconds=60)
    assert "signal_bursts" not in result["historical"]
    assert "signal_bursts" not in result["paper"]
    assert result["historical_clustered"] is False
    assert result["paper_clustered"] is False

    unverified = signal_frequency(reference, saved, {"verified": False},
                                  observed_candles=1000, entry_signals=30,
                                  paper_signal_times=[1_000 + i * 60 for i in range(30)],
                                  step_seconds=60)
    assert unverified["paper"] is None
    assert unverified["paper_clustered"] is False


def test_frequency_coverage_uses_evaluated_candles_not_signal_times():
    reference = {"verification": {"verified": True}}
    saved = {"events": [{"signal_time": 160}],
             "equity": [{"t": 100}, {"t": 160}, {"t": 220}]}
    result = signal_frequency(reference, saved, {"verified": True},
                              observed_candles=2, entry_signals=1,
                              paper_signal_times=[520], paper_observed_times=[400, 520],
                              step_seconds=60)
    assert result["historical"]["first_candle_t"] == 100
    assert result["historical"]["last_candle_t"] == 220
    assert result["paper"]["first_candle_t"] == 400
    assert result["paper"]["last_candle_t"] == 520
    assert result["historical"]["signal_bursts"] == 1


def test_invalid_coverage_is_omitted_without_hiding_verified_rate():
    reference = {"verification": {"verified": True}}
    saved = {"events": [], "equity": [{"t": 100}, {"t": 100}]}
    result = signal_frequency(reference, saved, {"verified": True},
                              observed_candles=2, entry_signals=0,
                              paper_observed_times=[100], step_seconds=60)
    assert result["historical"]["evaluated_candles"] == 2
    assert "first_candle_t" not in result["historical"]
    assert "last_candle_t" not in result["paper"]
    assert result["paper"]["evaluated_candles"] == 2

    unverified = signal_frequency(reference, saved, {"verified": False},
                                  observed_candles=2, entry_signals=0,
                                  paper_observed_times=[100, 160], step_seconds=60)
    assert unverified["paper"] is None
