"""Estados causales serializables para indicadores de memoria no acotada.

No cambia por sí solo la evaluación PAPER. Sus instantáneas permitirán
reanudar una serie sin recalcularla desde la primera vela, tras comprobar
paridad con el evaluador LAB correspondiente.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from app.lab.engine import Candle, D, LabError, PRECISION


def _decimal(raw: object) -> Decimal:
    if not isinstance(raw, str):
        raise LabError("Estado incremental decimal inválido")
    try:
        value = D(raw)
    except InvalidOperation:
        raise LabError("Estado incremental decimal inválido") from None
    if not value.is_finite():
        raise LabError("Estado incremental decimal no finito")
    return value


def _optional_decimal(raw: object) -> Decimal | None:
    return None if raw is None else _decimal(raw)


@dataclass
class EmaState:
    period: int
    precision: int = PRECISION
    count: int = 0
    seed_sum: Decimal = D(0)
    current: Decimal | None = None
    previous: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.period) is not int or self.period < 2 or type(self.precision) is not int or self.precision < 28:
            raise LabError("Periodo o precisión incremental inválidos")

    def push(self, close: Decimal) -> Decimal | None:
        if not isinstance(close, Decimal) or not close.is_finite():
            raise LabError("Cierre incremental inválido")
        with localcontext() as context:
            context.prec = self.precision
            self.previous = self.current
            if self.count < self.period:
                self.seed_sum += close
                self.count += 1
                if self.count == self.period:
                    self.current = self.seed_sum / D(self.period)
            else:
                alpha = D(2) / D(self.period + 1)
                self.current = alpha * close + (D(1) - alpha) * self.current
                self.count += 1
            return self.current

    def snapshot(self) -> dict:
        return {"version": 1, "period": self.period, "precision": self.precision,
                "count": self.count, "seed_sum": str(self.seed_sum),
                "current": None if self.current is None else str(self.current),
                "previous": None if self.previous is None else str(self.previous)}

    @classmethod
    def restore(cls, raw: dict) -> EmaState:
        if not isinstance(raw, dict) or set(raw) != {"version", "period", "precision", "count", "seed_sum", "current", "previous"} or raw["version"] != 1:
            raise LabError("Versión de estado EMA inválida")
        if type(raw["count"]) is not int or raw["count"] < 0:
            raise LabError("Conteo EMA inválido")
        state = cls(raw["period"], raw["precision"], raw["count"],
                    _decimal(raw["seed_sum"]), _optional_decimal(raw["current"]),
                    _optional_decimal(raw["previous"]))
        if (state.count < state.period and state.current is not None
                or state.count >= state.period and state.current is None
                or state.count <= state.period and state.previous is not None
                or state.count > state.period and state.previous is None
                or state.count == 0 and state.seed_sum != 0):
            raise LabError("Estado EMA incoherente")
        return state


@dataclass
class MacdState:
    fast: EmaState
    slow: EmaState
    signal: EmaState
    precision: int = PRECISION
    line: Decimal | None = None
    previous_line: Decimal | None = None
    previous_signal: Decimal | None = None

    @classmethod
    def create(cls, fast: int, slow: int, signal: int, *, precision: int = PRECISION) -> MacdState:
        if not 2 <= fast < slow or signal < 2:
            raise LabError("Parámetros MACD incrementales inválidos")
        # _ema_values del motor LAB siempre usa PRECISION, incluso cuando
        # IndicatorCache aplica precisión legada al restar las dos EMA.
        return cls(EmaState(fast, PRECISION), EmaState(slow, PRECISION),
                   EmaState(signal, PRECISION), precision)

    def push(self, close: Decimal) -> tuple[Decimal | None, Decimal | None]:
        with localcontext() as context:
            context.prec = self.precision
            fast_value = self.fast.push(close)
            slow_value = self.slow.push(close)
            self.previous_line = self.line
            self.previous_signal = self.signal.current
            self.line = (fast_value - slow_value if fast_value is not None and slow_value is not None
                         else None)
            if self.line is not None:
                self.signal.push(self.line)
            return self.line, self.signal.current

    def snapshot(self) -> dict:
        return {"version": 1, "fast": self.fast.snapshot(), "slow": self.slow.snapshot(),
                "signal": self.signal.snapshot(), "precision": self.precision,
                "line": None if self.line is None else str(self.line),
                "previous_line": None if self.previous_line is None else str(self.previous_line),
                "previous_signal": None if self.previous_signal is None else str(self.previous_signal)}

    @classmethod
    def restore(cls, raw: dict) -> MacdState:
        if not isinstance(raw, dict) or set(raw) != {"version", "fast", "slow", "signal", "precision", "line", "previous_line", "previous_signal"} or raw["version"] != 1:
            raise LabError("Versión de estado MACD inválida")
        fast, slow, signal = (EmaState.restore(raw[name]) for name in ("fast", "slow", "signal"))
        if (not fast.period < slow.period or fast.count != slow.count
                or fast.precision != PRECISION or slow.precision != PRECISION
                or signal.precision != PRECISION
                or type(raw["precision"]) is not int or raw["precision"] < 28
                or signal.count != max(0, fast.count - slow.period + 1)):
            raise LabError("Estado MACD incoherente")
        line = _optional_decimal(raw["line"])
        if (fast.count < slow.period) != (line is None):
            raise LabError("Línea MACD incoherente")
        return cls(fast, slow, signal, raw["precision"], line,
                   _optional_decimal(raw["previous_line"]),
                   _optional_decimal(raw["previous_signal"]))


@dataclass
class AtrState:
    period: int
    precision: int = PRECISION
    count: int = 0
    seed_sum: Decimal = D(0)
    current: Decimal | None = None
    previous_close: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.period) is not int or self.period < 2 or type(self.precision) is not int or self.precision < 28:
            raise LabError("Periodo o precisión ATR inválidos")

    def push(self, bar: Candle) -> Decimal | None:
        if not isinstance(bar, Candle):
            raise LabError("Vela ATR inválida")
        with localcontext() as context:
            context.prec = self.precision
            true_range = (bar.h - bar.l if self.previous_close is None else
                          max(bar.h - bar.l, abs(bar.h - self.previous_close),
                              abs(bar.l - self.previous_close)))
            self.previous_close = bar.c
            if self.count < self.period:
                self.seed_sum += true_range
                self.count += 1
                if self.count == self.period:
                    self.current = self.seed_sum / D(self.period)
            else:
                self.current = (self.current * D(self.period - 1) + true_range) / D(self.period)
                self.count += 1
            return self.current

    def snapshot(self) -> dict:
        return {"version": 1, "period": self.period, "precision": self.precision,
                "count": self.count, "seed_sum": str(self.seed_sum),
                "current": None if self.current is None else str(self.current),
                "previous_close": None if self.previous_close is None else str(self.previous_close)}

    @classmethod
    def restore(cls, raw: dict) -> AtrState:
        if not isinstance(raw, dict) or set(raw) != {"version", "period", "precision", "count", "seed_sum", "current", "previous_close"} or raw["version"] != 1:
            raise LabError("Versión de estado ATR inválida")
        if type(raw["count"]) is not int or raw["count"] < 0:
            raise LabError("Conteo ATR inválido")
        state = cls(raw["period"], raw["precision"], raw["count"], _decimal(raw["seed_sum"]),
                    _optional_decimal(raw["current"]), _optional_decimal(raw["previous_close"]))
        if (state.count < state.period and state.current is not None
                or state.count >= state.period and state.current is None
                or state.count == 0 and state.previous_close is not None
                or state.count > 0 and state.previous_close is None
                or state.count == 0 and state.seed_sum != 0):
            raise LabError("Estado ATR incoherente")
        return state
