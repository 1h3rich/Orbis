"""Evaluación causal de una ventana reciente con estado acumulado verificado.

Sólo admite reglas de la temporalidad base y precisión actual. No reemplaza
todavía el evaluador de las sesiones PAPER ni autoriza retirar su límite.
"""

from __future__ import annotations

from app.lab.engine import (Candle, CURRENT_ENGINE_VERSION, IndicatorCache,
                            LabError, PRECISION, evaluate)
from app.lab.incremental_indicators import AtrState, EmaState, MacdState


RECENT_BARS = 512


def recent_supported(definition: dict, engine_version: str) -> bool:
    if engine_version != CURRENT_ENGINE_VERSION:
        return False

    def base_only(rule: dict) -> bool:
        if rule.get("op") in ("AND", "OR"):
            return all(base_only(child) for child in rule["children"])
        return "timeframe" not in rule

    return base_only(definition["entry"]) and (
        "exit" not in definition or base_only(definition["exit"]))


class RecentIndicatorCache(IndicatorCache):
    def __init__(self, candles: list[Candle], snapshot: dict):
        super().__init__(candles, precision=PRECISION)
        if not candles or len(candles) > RECENT_BARS or snapshot.get("last_t") != candles[-1].t:
            raise LabError("Ventana reciente PAPER incoherente")
        self.snapshot = snapshot

    def value(self, kind: str, period: int, i: int, multiplier=None):
        latest = len(self.candles) - 1
        if kind == "EMA" and i in (latest, latest - 1):
            state = EmaState.restore(self.snapshot["ema"][str(period)])
            return state.current if i == latest else state.previous
        if kind == "ATR" and i == latest:
            return AtrState.restore(self.snapshot["atr"][str(period)]).current
        return super().value(kind, period, i, multiplier)

    def macd(self, fast: int, slow: int, signal: int, i: int):
        latest = len(self.candles) - 1
        if i in (latest, latest - 1):
            state = MacdState.restore(self.snapshot["macd"][f"{fast}-{slow}-{signal}"])
            return ((state.line, state.signal.current) if i == latest
                    else (state.previous_line, state.previous_signal))
        return super().macd(fast, slow, signal, i)

    def resampled(self, base_timeframe: str, higher_timeframe: str):
        raise LabError("Regla multitemporal no admitida en ventana reciente")


def evaluate_recent(rule: dict, recent_bars: list[Candle], snapshot: dict,
                    *, engine_version: str, base_timeframe: str) -> tuple[bool, dict]:
    if engine_version != CURRENT_ENGINE_VERSION:
        raise LabError("Motor legado requiere histórico completo")
    if len(recent_bars) < RECENT_BARS:
        raise LabError("La ventana PAPER aún no está completa")
    cache = RecentIndicatorCache(recent_bars, snapshot)
    return evaluate(rule, recent_bars, len(recent_bars) - 1, cache,
                    base_timeframe=base_timeframe)
