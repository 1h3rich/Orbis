"""Partición cronológica para evidencia LAB sin etiquetas cruzando la reserva."""

from app.lab.export_split import partition_evidence


DAY = 86400
START = 1735689600


def case(day, *, full=True, followup_days=7):
    stamp = START + day * DAY
    return {"features_at_close": {"signal_time": stamp, "candle": {"c": "100"}},
            "future_labels": {"followup_end_time": stamp + followup_days * DAY,
                              "full_7d_followup": full}}


def test_partition_purges_labels_crossing_cutoff_and_keeps_partial_cases_out():
    candles = [{"t": START + day * DAY} for day in range(60)]
    rows = [case(30), case(40), case(42), case(55, full=False, followup_days=4)]
    split = partition_evidence(candles, rows, "1d")
    assert split["boundary_open_t"] == START + 42 * DAY
    assert split["boundary_index"] == 42
    assert split["horizon_seconds"] == 7 * DAY
    assert split["counts"] == {"development": 1, "purged_boundary": 1,
                               "validation": 1, "incomplete_validation": 1,
                               "incomplete_development": 0}
    assert [row["partition"] for row in rows] == [
        "development", "purged_boundary", "validation", "incomplete_validation",
    ]
    assert split["development_indices"] == [0]
    assert split["validation_indices"] == [2]
    assert rows[0]["features_at_close"] == {"signal_time": START + 30 * DAY, "candle": {"c": "100"}}


def test_close_at_cutoff_is_not_a_training_signal_and_input_is_checked():
    candles = [{"t": START + day * DAY} for day in range(10)]
    rows = [case(6, full=True, followup_days=0), case(7, full=True, followup_days=0)]
    rows.append(case(9, full=False, followup_days=0))
    rows[-1]["future_labels"]["followup_end_time"] = None
    split = partition_evidence(candles, rows, "1d")
    assert split["boundary_open_t"] == START + 7 * DAY
    assert rows[0]["partition"] == "purged_boundary"
    assert rows[1]["partition"] == "validation"
    assert rows[2]["partition"] == "incomplete_validation"


def test_nonoverlap_indices_choose_chronological_complete_windows_without_results():
    candles = [{"t": START + day * DAY} for day in range(60)]
    rows = [case(10), case(11), case(18), case(43), case(44), case(51)]
    split = partition_evidence(candles, rows, "1d")
    assert split["development_indices"] == [0, 1, 2]
    assert split["validation_indices"] == [3, 4, 5]
    assert split["development_nonoverlap_indices"] == [0, 2]
    assert split["validation_nonoverlap_indices"] == [3, 5]

    # El orden del archivo puede cambiar sin que la selección temporal cambie.
    reordered = [rows[index] for index in (2, 1, 0, 5, 4, 3)]
    split_reordered = partition_evidence(candles, reordered, "1d")
    assert [reordered[index]["features_at_close"]["signal_time"]
            for index in split_reordered["development_nonoverlap_indices"]] == [
                START + 10 * DAY, START + 18 * DAY,
            ]
