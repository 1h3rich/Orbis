"""Análisis retrospectivo de huecos PAPER, sin simular fills ni operar."""

from __future__ import annotations

from app.lab.engine import IndicatorCache, INTERVALS, LabError, evaluate, paper_precision_for_engine, validate_candles


MAX_DOWNLOAD_BARS = 5000


def gap_download_range(session: dict, now: int) -> tuple[int, int]:
    step = INTERVALS[session["timeframe"]]
    last_t = session["last_t"]
    if session["status"] != "GAP":
        raise LabError("La sesión no está detenida por un hueco PAPER")
    end = now // step * step
    missed = (end - last_t - step) // step
    if missed < 1:
        raise LabError("Aún no hay velas cerradas posteriores a la última observación")
    if missed + 3 > MAX_DOWNLOAD_BARS:
        raise LabError("El hueco supera la cobertura verificable; no se presenta una reconstrucción parcial")
    return last_t - 2 * step, end


def analyze_paper_gap(session: dict, definition: dict, history: list[dict],
                      downloaded: list[dict], *, now: int, source: str) -> dict:
    """Sólo calcula señales sobre velas cerradas y conserva la frontera observada."""
    step = INTERVALS[session["timeframe"]]
    last_t = session["last_t"]
    if session["status"] != "GAP" or len(history) < 3 or history[-1]["t"] != last_t:
        raise LabError("La sesión no tiene un hueco PAPER verificable")
    _, end = gap_download_range(session, now)
    missed = (end - last_t - step) // step
    overlap, gaps = validate_candles(downloaded, session["timeframe"], now=now)
    if (gaps or len(overlap) != missed + 3 or overlap[0].t != last_t - 2 * step
            or overlap[-1].t != end - step
            or [bar.json() for bar in overlap[:3]] != history[-3:]):
        raise LabError("Histórico revisado o incompleto; no se reconstruyen señales PAPER")
    bars, gaps = validate_candles(history + [bar.json() for bar in overlap[3:]],
                                  session["timeframe"], now=now)
    if gaps:
        raise LabError("El hueco contiene velas discontinuas")
    cache = IndicatorCache(bars, precision=paper_precision_for_engine(session["engine_version"]))
    signals = []
    for index in range(len(history), len(bars)):
        entry, _ = evaluate(definition["entry"], bars, index, cache,
                            base_timeframe=session["timeframe"])
        exit_match = (evaluate(definition["exit"], bars, index, cache,
                               base_timeframe=session["timeframe"])[0]
                      if "exit" in definition else None)
        if entry or exit_match:
            signals.append({"candle_t": bars[index].t, "close": str(bars[index].c),
                            "entry_match": entry, "exit_match": exit_match})
    return {"session_id": session["id"], "status": "RECONSTRUCTED_NO_EXECUTION",
            "source": source, "from_t": last_t + step, "through_t": end - step,
            "candles_reviewed": missed, "signals": signals,
            "entry_signals": sum(row["entry_match"] for row in signals),
            "exit_signals": sum(bool(row["exit_match"]) for row in signals),
            "paper_state_unchanged": True,
            "warning": "Sólo señales históricas. No se observaron en directo ni se simulan órdenes, stops o resultados PAPER."}
