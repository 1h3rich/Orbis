"""Frecuencia descriptiva de señales por vela evaluada, nunca rendimiento."""

from __future__ import annotations

from decimal import Decimal, localcontext


def _rate(signals: int, candles: int) -> dict | None:
    if (type(signals) is not int or type(candles) is not int
            or candles <= 0 or not 0 <= signals <= candles):
        return None
    with localcontext() as context:
        context.prec = 80
        rate = Decimal(signals) * Decimal(100) / Decimal(candles)
    return {"signals": signals, "evaluated_candles": candles,
            "per_100_candles": str(rate)}


def _bursts(times: list[int] | None, *, signals: int, step_seconds: int | None) -> int | None:
    """Cuenta sólo rachas de señales en velas consecutivas, no casos independientes."""
    if (not isinstance(times, list) or type(step_seconds) is not int or step_seconds <= 0
            or len(times) != signals or any(type(value) is not int for value in times)):
        return None
    ordered = sorted(times)
    if any(later <= earlier for earlier, later in zip(ordered, ordered[1:])):
        return None
    return sum(previous is None or current - previous != step_seconds
               for previous, current in zip([None] + ordered[:-1], ordered))


def _coverage(times: list[int] | None, *, candles: int) -> tuple[int, int] | None:
    """Publica sólo el intervalo de velas realmente observadas y ordenadas."""
    if (not isinstance(times, list) or len(times) != candles
            or not times or any(type(value) is not int or value < 0 for value in times)
            or any(later <= earlier for earlier, later in zip(times, times[1:]))):
        return None
    return times[0], times[-1]


def signal_frequency(reference: dict | None, saved_run: dict | None,
                     replay: dict, *, observed_candles: int,
                     entry_signals: int, paper_signal_times: list[int] | None = None,
                     paper_observed_times: list[int] | None = None,
                     step_seconds: int | None = None) -> dict:
    """Sólo publica tasas para evidencia LAB verificada y PAPER reproducida.

    Los periodos son distintos; no calcula delta, ratio ni significación.
    `equity` tiene una fila por vela realmente evaluada por el motor LAB, de
    modo que excluye cualquier calentamiento anterior a `entry_start_index`.
    """
    historical = (_rate(len(saved_run["events"]), len(saved_run["equity"]))
                  if reference and reference["verification"]["verified"] and saved_run
                  else None)
    paper = (_rate(entry_signals, observed_candles) if replay.get("verified") else None)
    if historical is not None:
        coverage = _coverage([row.get("t") if isinstance(row, dict) else None
                              for row in saved_run["equity"]],
                             candles=historical["evaluated_candles"])
        if coverage is not None:
            historical["first_candle_t"], historical["last_candle_t"] = coverage
        event_times = [row.get("signal_time") if isinstance(row, dict) else None
                       for row in saved_run["events"]]
        bursts = _bursts(event_times, signals=historical["signals"], step_seconds=step_seconds)
        if bursts is not None:
            historical["signal_bursts"] = bursts
    if paper is not None:
        coverage = _coverage(paper_observed_times, candles=paper["evaluated_candles"])
        if coverage is not None:
            paper["first_candle_t"], paper["last_candle_t"] = coverage
        bursts = _bursts(paper_signal_times, signals=paper["signals"],
                         step_seconds=step_seconds)
        if bursts is not None:
            paper["signal_bursts"] = bursts
    return {"unit": "signals_per_100_evaluated_candles",
            "historical": historical, "paper": paper,
            "historical_small_sample": historical is not None and
            (historical["evaluated_candles"] < 30 or historical["signals"] < 30),
            "paper_small_sample": paper is not None and
            (paper["evaluated_candles"] < 30 or paper["signals"] < 30),
            "historical_clustered": historical is not None and historical["signals"] >= 30 and
            historical.get("signal_bursts", 30) < 30,
            "paper_clustered": paper is not None and paper["signals"] >= 30 and
            paper.get("signal_bursts", 30) < 30,
            "directly_comparable": False,
            "reason": "Periodos y regímenes distintos; las rachas consecutivas no son muestras independientes ni miden rentabilidad"}
