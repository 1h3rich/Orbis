"""Instantánea parcial y verificable de indicadores causales para PAPER.

Por ahora conserva EMA, MACD y ATR de reglas en la temporalidad base. El
evaluador histórico continúa decidiendo señales; esta instantánea no permite
retirar el límite de velas hasta cubrir también las demás reglas.
"""

from __future__ import annotations

from decimal import Decimal

from app.lab.engine import Candle, IndicatorCache, LabError
from app.lab.incremental_indicators import AtrState, EmaState, MacdState


def _specs(definition: dict) -> tuple[tuple[int, ...], tuple[tuple[int, int, int], ...], tuple[int, ...]]:
    ema: set[int] = set()
    macd: set[tuple[int, int, int]] = set()
    atr: set[int] = set()

    def visit(rule: dict) -> None:
        if rule.get("op") in ("AND", "OR"):
            for child in rule["children"]:
                visit(child)
            return
        if "timeframe" in rule:
            return
        kind = rule["kind"]
        if kind in ("CLOSE_ABOVE_EMA", "CLOSE_BELOW_EMA"):
            ema.add(rule["period"])
        elif kind in ("EMA_CROSS_ABOVE_EMA", "EMA_CROSS_BELOW_EMA"):
            ema.update((rule["period"], rule["slow_period"]))
        elif kind in ("MACD_CROSS_ABOVE_SIGNAL", "MACD_CROSS_BELOW_SIGNAL"):
            macd.add((rule["period"], rule["slow_period"], rule["signal_period"]))
        elif kind in ("ATR_PCT_GT", "ATR_PCT_LT"):
            atr.add(rule["period"])

    visit(definition["entry"])
    if "exit" in definition:
        visit(definition["exit"])
    return tuple(sorted(ema)), tuple(sorted(macd)), tuple(sorted(atr))


def _states(definition: dict, precision: int) -> tuple[dict, dict, dict]:
    ema_periods, macd_params, atr_periods = _specs(definition)
    return ({str(period): EmaState(period, precision) for period in ema_periods},
            {"-".join(map(str, params)): MacdState.create(*params, precision=precision)
             for params in macd_params},
            {str(period): AtrState(period, precision) for period in atr_periods})


def _bar(raw: dict) -> Candle:
    return Candle(raw["t"], *(Decimal(raw[name]) for name in ("o", "h", "l", "c", "v")))


def build_paper_indicator_state(definition: dict, history: list[dict], precision: int) -> dict:
    ema, macd, atr = _states(definition, precision)
    for raw in history:
        candle = _bar(raw)
        for state in ema.values():
            state.push(candle.c)
        for state in macd.values():
            state.push(candle.c)
        for state in atr.values():
            state.push(candle)
    return {"version": 1, "precision": precision, "count": len(history),
            "last_t": history[-1]["t"] if history else None,
            "ema": {key: value.snapshot() for key, value in ema.items()},
            "macd": {key: value.snapshot() for key, value in macd.items()},
            "atr": {key: value.snapshot() for key, value in atr.items()}}


def advance_paper_indicator_state(snapshot: dict, definition: dict, raw: dict,
                                  *, previous_t: int, precision: int) -> dict:
    ema_template, macd_template, atr_template = _states(definition, precision)
    if (not isinstance(snapshot, dict) or snapshot.get("version") != 1
            or snapshot.get("precision") != precision or snapshot.get("last_t") != previous_t
            or type(snapshot.get("count")) is not int or snapshot["count"] < 1
            or set(snapshot.get("ema", {})) != set(ema_template)
            or set(snapshot.get("macd", {})) != set(macd_template)
            or set(snapshot.get("atr", {})) != set(atr_template)
            or raw["t"] <= previous_t):
        raise LabError("Estado incremental PAPER incoherente")
    ema = {key: EmaState.restore(value) for key, value in snapshot["ema"].items()}
    macd = {key: MacdState.restore(value) for key, value in snapshot["macd"].items()}
    atr = {key: AtrState.restore(value) for key, value in snapshot["atr"].items()}
    if (any(state.count != snapshot["count"] for state in (*ema.values(), *atr.values()))
            or any(state.fast.count != snapshot["count"] for state in macd.values())):
        raise LabError("Conteo incremental PAPER incoherente")
    candle = _bar(raw)
    for state in ema.values():
        state.push(candle.c)
    for state in macd.values():
        state.push(candle.c)
    for state in atr.values():
        state.push(candle)
    return {"version": 1, "precision": precision, "count": snapshot["count"] + 1,
            "last_t": candle.t,
            "ema": {key: value.snapshot() for key, value in ema.items()},
            "macd": {key: value.snapshot() for key, value in macd.items()},
            "atr": {key: value.snapshot() for key, value in atr.items()}}


def paper_indicator_state_matches(snapshot: dict, definition: dict,
                                  history: list[dict], precision: int) -> bool:
    try:
        expected = build_paper_indicator_state(definition, history, precision)
        return snapshot == expected
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def paper_indicator_values_match(snapshot: dict, bars: list[Candle], definition: dict,
                                 precision: int, cache: IndicatorCache | None = None) -> bool:
    """Comprueba el último punto frente al evaluador antes de archivarlo."""
    if not bars or snapshot.get("count") != len(bars) or snapshot.get("last_t") != bars[-1].t:
        return False
    cache = cache or IndicatorCache(bars, precision=precision)
    index = len(bars) - 1
    ema_periods, macd_params, atr_periods = _specs(definition)
    for period in ema_periods:
        state = EmaState.restore(snapshot["ema"][str(period)])
        if state.current != cache.value("EMA", period, index):
            return False
    for fast, slow, signal in macd_params:
        state = MacdState.restore(snapshot["macd"][f"{fast}-{slow}-{signal}"])
        if (state.line, state.signal.current) != cache.macd(fast, slow, signal, index):
            return False
    for period in atr_periods:
        state = AtrState.restore(snapshot["atr"][str(period)])
        if state.current != cache.value("ATR", period, index):
            return False
    return True
