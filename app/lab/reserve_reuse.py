"""Diagnóstico de reservas OOS compartidas por experimentos LAB guardados.

Sólo compara intervalos de un mismo SHA-256 de dataset. No demuestra que el
usuario no haya mirado previamente los datos ni detecta copias reimportadas.
"""

from __future__ import annotations


def _interval(start: object, end: object) -> tuple[int, int]:
    if type(start) is not int or type(end) is not int or start > end:
        raise ValueError("Intervalo LAB no verificable")
    return start, end


def tested_intervals(result: dict) -> list[tuple[int, int]]:
    """Extrae sólo reservas efectivamente evaluadas, nunca desarrollo."""
    if not isinstance(result, dict):
        raise ValueError("Experimento LAB no verificable")
    method = result.get("method")
    if method in ("CHRONOLOGICAL_HOLDOUT", "GRID_STOP_TARGET_HOLDOUT"):
        if method == "GRID_STOP_TARGET_HOLDOUT" and result.get("out_of_sample") is None:
            return []
        return [_interval(result.get("test_start_t"), result.get("test_end_t"))]
    if method in ("ROLLING_FIXED_VERSION", "WALK_FORWARD_GRID_STOP_TARGET"):
        windows = result.get("windows")
        if not isinstance(windows, list):
            raise ValueError("Ventanas LAB no verificables")
        intervals = []
        for window in windows:
            if not isinstance(window, dict):
                raise ValueError("Ventana LAB no verificable")
            if window.get("out_of_sample") is not None:
                intervals.append(_interval(window.get("test_start_t"), window.get("test_end_t")))
        if method == "WALK_FORWARD_GRID_STOP_TARGET" and "final_holdout" in result:
            final = result["final_holdout"]
            if not isinstance(final, dict):
                raise ValueError("Reserva final LAB no verificable")
            if final.get("out_of_sample") is not None:
                intervals.append(_interval(final.get("start_t"), final.get("end_t")))
        return intervals
    raise ValueError("Método LAB no verificable")


def reserve_reuse(current: dict, peers: list[dict]) -> dict:
    """No llama 'independiente' a la ausencia de otros registros locales."""
    scope = "SAME_DATASET_SHA256_SAVED_RUNS_ONLY"
    try:
        current_intervals = tested_intervals(current)
    except ValueError:
        return {"status": "ARCHIVE_UNVERIFIABLE", "overlapping_run_count": 0,
                "overlapping_runs": [], "scope": scope}
    if not current_intervals:
        return {"status": "NO_RESERVE_EVALUATED", "overlapping_run_count": 0,
                "overlapping_runs": [], "scope": scope}
    overlaps = []
    for peer in peers:
        try:
            intervals = tested_intervals(peer["result"])
        except (KeyError, ValueError):
            return {"status": "ARCHIVE_UNVERIFIABLE", "overlapping_run_count": 0,
                    "overlapping_runs": [], "scope": scope}
        if any(left_start <= right_end and right_start <= left_end
               for left_start, left_end in current_intervals
               for right_start, right_end in intervals):
            overlaps.append({"id": peer["id"], "kind": peer["kind"]})
    overlaps.sort(key=lambda row: (row["kind"], row["id"]))
    return {"status": ("SHARED_WITH_SAVED_EXPERIMENTS" if overlaps
                       else "NO_SHARED_SAVED_RESERVE"),
            "overlapping_run_count": len(overlaps),
            "overlapping_runs": overlaps, "scope": scope}
