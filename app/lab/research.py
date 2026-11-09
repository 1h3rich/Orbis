"""Investigación descriptiva de estructuras, sin órdenes ni capital simulado."""

from __future__ import annotations

from decimal import Decimal, localcontext

from app.lab.engine import (Candle, D, INTERVALS, IndicatorCache, LabError, evaluate,
                            uses_rsi_divergence, validate_candle_values, validate_rule)


ENGINE_VERSION = "research-1.2"
SEGMENTED_ENGINE_VERSION = "research-1.1"
LEGACY_ENGINE_VERSION = "research-1.0"
HORIZONS = (3600, 14400, 86400, 604800)


def _quantile(values: list[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    position = D(len(ordered) - 1) * fraction
    lower = int(position)
    weight = position - lower
    return ordered[lower] if weight == 0 else ordered[lower] * (D(1) - weight) + ordered[lower + 1] * weight


def _distribution(values: list[Decimal]) -> dict:
    if not values:
        return {"count": 0, "mean_pct": None, "p25_pct": None, "median_pct": None,
                "p75_pct": None, "min_pct": None, "max_pct": None, "positive_rate": None}
    return {"count": len(values), "mean_pct": str(sum(values, D(0)) / D(len(values))),
            "p25_pct": str(_quantile(values, D("0.25"))),
            "median_pct": str(_quantile(values, D("0.5"))),
            "p75_pct": str(_quantile(values, D("0.75"))),
            "min_pct": str(min(values)), "max_pct": str(max(values)),
            "positive_rate": str(D(sum(value > 0 for value in values)) / D(len(values)))}


def _run_contiguous(candles: list[Candle], rule: dict, timeframe: str) -> dict:
    """Busca todos los cierres que cumplen una regla causal y etiqueta su futuro.

    Las etiquetas posteriores nunca se pasan a `evaluate`; cada horizonte
    aporta a su distribución sólo cuando está completamente cubierto.
    """
    if timeframe not in INTERVALS or len(candles) < 3:
        raise LabError("Histórico o temporalidad insuficiente para investigar")
    step = INTERVALS[timeframe]
    if any(current.t - previous.t != step for previous, current in zip(candles, candles[1:])):
        raise LabError("El histórico tiene huecos o velas desordenadas; no se investigan horizontes ficticios")
    with localcontext() as context:
        context.prec = 80
        cache = IndicatorCache(candles)
        events: list[dict] = []
        distributions: dict[str, list[Decimal]] = {str(horizon): [] for horizon in HORIZONS}
        followup_bars = 604800 // step
        for index, bar in enumerate(candles):
            matched, explanation = evaluate(rule, candles, index, cache, base_timeframe=timeframe)
            if not matched:
                continue
            outcomes: dict[str, str | None] = {}
            for horizon in HORIZONS:
                offset = horizon // step if horizon % step == 0 else 0
                outcome = (candles[index + offset].c / bar.c - 1) * D(100) if offset and index + offset < len(candles) else None
                outcomes[str(horizon)] = str(outcome) if outcome is not None else None
                if outcome is not None:
                    distributions[str(horizon)].append(outcome)
            future = candles[index + 1:min(len(candles), index + followup_bars + 1)]
            high = max(future, key=lambda item: item.h) if future else None
            low = min(future, key=lambda item: item.l) if future else None
            events.append({"signal_time": bar.t, "close": str(bar.c), "volume": str(bar.v),
                           "explanation": explanation, "outcomes_pct": outcomes,
                           "max_up_pct": str(max(D(0), (high.h / bar.c - 1) * D(100))) if high else None,
                           "max_down_pct": str(min(D(0), (low.l / bar.c - 1) * D(100))) if low else None,
                           "max_at": high.t if high else None, "min_at": low.t if low else None,
                           "followup_end_time": future[-1].t if future else None,
                           "full_7d_followup": index + followup_bars < len(candles)})
        return {"rule": rule, "events": events,
                "summary": {"cases": len(events), "horizons": {key: _distribution(values) for key, values in distributions.items()}},
                "assumptions": ["La regla sólo observa velas cerradas; resultados posteriores son etiquetas descriptivas",
                                "Cada horizonte usa sólo casos con cobertura futura completa",
                                "Máximo avance/retroceso usa hasta 7 días disponibles y puede quedar truncado al final del histórico",
                                "Los eventos pueden solaparse: no son muestras independientes",
                                "Variaciones brutas de precio, sin órdenes, comisiones ni capital simulado"]}


def run_research(candles: list[Candle], rule: dict, timeframe: str,
                 *, engine_version: str = ENGINE_VERSION) -> dict:
    """Investiga tramos contiguos sin calentar indicadores ni mirar a través de huecos."""
    validate_candle_values(candles, timeframe)
    if engine_version != ENGINE_VERSION and uses_rsi_divergence(rule):
        raise LabError("Versión de investigación incompatible con la divergencia RSI")
    if engine_version == LEGACY_ENGINE_VERSION:
        return _run_contiguous(candles, rule, timeframe)
    if engine_version not in (SEGMENTED_ENGINE_VERSION, ENGINE_VERSION):
        raise LabError("Versión del motor de investigación no disponible")
    if timeframe not in INTERVALS or len(candles) < 3:
        raise LabError("Histórico o temporalidad insuficiente para investigar")
    step = INTERVALS[timeframe]
    segments: list[list[Candle]] = []
    start = 0
    missing_bars = 0
    for index in range(1, len(candles)):
        delta = candles[index].t - candles[index - 1].t
        if delta <= 0 or delta % step:
            raise LabError("El histórico tiene velas duplicadas o desordenadas")
        if delta > step:
            segments.append(candles[start:index])
            missing_bars += delta // step - 1
            start = index
    segments.append(candles[start:])

    events: list[dict] = []
    for segment in segments:
        if len(segment) >= 3:
            events.extend(_run_contiguous(segment, rule, timeframe)["events"])
    distributions: dict[str, list[Decimal]] = {str(horizon): [] for horizon in HORIZONS}
    for event in events:
        for horizon, value in event["outcomes_pct"].items():
            if value is not None:
                distributions[horizon].append(D(value))
    horizon_summaries = {key: _distribution(values) for key, values in distributions.items()}
    if engine_version == ENGINE_VERSION:
        # One deterministic, earliest-first subset per horizon. This removes
        # overlapping future windows; it does not make observations independent.
        for horizon in HORIZONS:
            key = str(horizon)
            selected: list[Decimal] = []
            next_signal_time: int | None = None
            for event in events:
                value = event["outcomes_pct"][key]
                if value is not None and (next_signal_time is None or event["signal_time"] >= next_signal_time):
                    selected.append(D(value))
                    next_signal_time = event["signal_time"] + horizon
            horizon_summaries[key]["non_overlapping"] = _distribution(selected)
    assumptions = [
        "La regla sólo observa velas cerradas; resultados posteriores son etiquetas descriptivas",
        "Cada tramo reinicia los indicadores y no usa velas anteriores al hueco",
        "Cada horizonte usa sólo casos con cobertura futura completa dentro del mismo tramo",
        "Máximo avance/retroceso usa hasta 7 días disponibles dentro del tramo y puede quedar truncado",
        "Los eventos pueden solaparse: no son muestras independientes",
        "Variaciones brutas de precio, sin órdenes, comisiones ni capital simulado",
    ]
    if engine_version == ENGINE_VERSION:
        assumptions.append(
            "Subconjunto sin solapamiento: por horizonte se elige el primer cierre elegible y "
            "luego el siguiente al menos un horizonte después; reduce solapamiento, no prueba independencia"
        )
    return {
        "rule": rule,
        "events": events,
        "summary": {
            "cases": len(events),
            "horizons": horizon_summaries,
            "coverage": {
                "segments": len(segments), "missing_bars": missing_bars,
                "segment_ranges": [{"start_t": part[0].t, "end_t": part[-1].t,
                                    "candles": len(part)} for part in segments],
            },
        },
        "assumptions": assumptions,
    }


def validate_research_rule(raw: dict) -> dict:
    return validate_rule(raw)
