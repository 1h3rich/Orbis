"""Motor determinista de la primera estrategia de Laboratorio.

Una señal sólo observa velas *cerradas*; se ejecuta en la apertura siguiente.
Si stop y objetivo coinciden en una vela, prevalece el stop (conservador).
Los shorts son teóricos: no se modelan préstamo, financiación ni liquidación.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any


class LabError(ValueError):
    pass


INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
MAX_DATASET_CANDLES = 20000
LEGACY_TRAILING_ENGINE_VERSION = "lab-1.14"
PREVIOUS_ENGINE_VERSION = "lab-1.15"
CURRENT_ENGINE_VERSION = "lab-1.16"
RSI_DIVERGENCE_KINDS = ("RSI_BULLISH_DIVERGENCE", "RSI_BEARISH_DIVERGENCE")
D = Decimal
PRECISION = 80
LEGACY_PAPER_PRECISION = 28
LEGACY_PAPER_ENGINES = frozenset(f"lab-1.{minor}" for minor in range(5, 16))
NO_OPEN_EXIT_EXPOSURE_ENGINES = (PREVIOUS_ENGINE_VERSION, CURRENT_ENGINE_VERSION)
RECENT_ENGINES = (LEGACY_TRAILING_ENGINE_VERSION, PREVIOUS_ENGINE_VERSION, CURRENT_ENGINE_VERSION)


def paper_precision_for_engine(version: str) -> int:
    """Conserva señales PAPER antiguas; las nuevas comparten precisión LAB."""
    if version == CURRENT_ENGINE_VERSION:
        return PRECISION
    if version in LEGACY_PAPER_ENGINES:
        return LEGACY_PAPER_PRECISION
    raise LabError("Versión PAPER del evaluador no disponible")


def uses_rsi_divergence(rule: object) -> bool:
    if not isinstance(rule, dict):
        return False
    if rule.get("op") in ("AND", "OR"):
        return any(uses_rsi_divergence(child) for child in rule.get("children", ()))
    return rule.get("kind") in RSI_DIVERGENCE_KINDS


def supports_engine_version(version: str, definition: dict) -> bool:
    """Los motores previos siguen disponibles si sus reglas no cambiaron."""
    if version != CURRENT_ENGINE_VERSION and any(uses_rsi_divergence(definition.get(side))
                                                for side in ("entry", "exit")):
        return False

    def uses_ascending_volume(node: object) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get("op") in ("AND", "OR"):
            return any(uses_ascending_volume(child) for child in node.get("children", ()))
        return node.get("kind") == "VOLUME_ASCENDING_BARS"

    if version not in RECENT_ENGINES and (uses_ascending_volume(definition.get("entry"))
                                              or uses_ascending_volume(definition.get("exit"))):
        return False
    if "trailing_stop" in definition and version not in RECENT_ENGINES:
        return False
    if "max_holding_bars" in definition and version not in ("lab-1.11", "lab-1.12", "lab-1.13", *RECENT_ENGINES):
        return False

    def uses_higher_timeframe(node: object) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get("op") in ("AND", "OR"):
            return any(uses_higher_timeframe(child) for child in node.get("children", ()))
        return "timeframe" in node

    if version not in ("lab-1.13", *RECENT_ENGINES) and (uses_higher_timeframe(definition.get("entry"))
                                              or uses_higher_timeframe(definition.get("exit"))):
        return False

    def uses_swing_structure(node: object) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get("op") in ("AND", "OR"):
            return any(uses_swing_structure(child) for child in node.get("children", ()))
        return node.get("kind") in ("STRUCTURE_HH_HL", "STRUCTURE_LH_LL")

    if version not in ("lab-1.12", "lab-1.13", *RECENT_ENGINES) and (uses_swing_structure(definition.get("entry"))
                                              or uses_swing_structure(definition.get("exit"))):
        return False
    if version in ("lab-1.9", "lab-1.10", "lab-1.11", "lab-1.12", "lab-1.13", *RECENT_ENGINES):
        return True
    if version not in ("lab-1.5", "lab-1.6", "lab-1.7", "lab-1.8"):
        return False

    unavailable = {"EMA_CROSS_ABOVE_EMA", "EMA_CROSS_BELOW_EMA"}
    if version in ("lab-1.5", "lab-1.6", "lab-1.7"):
        unavailable.update(("MACD_CROSS_ABOVE_SIGNAL", "MACD_CROSS_BELOW_SIGNAL"))
    if version in ("lab-1.5", "lab-1.6"):
        unavailable.update(("CLOSE_ABOVE_BB_UPPER", "CLOSE_BELOW_BB_LOWER"))
    if version == "lab-1.5":
        unavailable.update(("CLOSE_ABOVE_SMA", "CLOSE_BELOW_SMA"))

    def original_rules(node: object) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get("op") in ("AND", "OR"):
            return all(original_rules(child) for child in node.get("children", ()))
        return node.get("kind") not in unavailable

    return original_rules(definition.get("entry")) and (
        "exit" not in definition or original_rules(definition["exit"]))


def number(value: Any, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or len(value) > 80:
        raise LabError("Los números deben enviarse como cadenas decimales")
    try:
        result = D(value)
    except Exception as exc:
        raise LabError("Número decimal inválido") from exc
    # Un exponente enorme ocupa pocos caracteres, pero puede desbordar
    # aritmética, serialización o memoria al ejecutar una simulación.
    if (not result.is_finite() or len(result.as_tuple().digits) > 40
            or result.as_tuple().exponent < -40
            or (result and result.adjusted() > 40)
            or (positive and result <= 0)):
        raise LabError("Número decimal fuera de rango")
    return result


@dataclass(frozen=True)
class Candle:
    t: int
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    v: Decimal

    def json(self) -> dict:
        return {"t": self.t, "o": str(self.o), "h": str(self.h), "l": str(self.l), "c": str(self.c), "v": str(self.v)}


def validate_candles(rows: list[dict], timeframe: str, *, now: int) -> tuple[list[Candle], list[dict]]:
    if timeframe not in INTERVALS:
        raise LabError("Temporalidad no admitida en Laboratorio")
    if not isinstance(rows, list) or not 3 <= len(rows) <= MAX_DATASET_CANDLES:
        raise LabError(f"Se requieren entre 3 y {MAX_DATASET_CANDLES} velas")
    step = INTERVALS[timeframe]
    candles: list[Candle] = []
    gaps: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or isinstance(row.get("t"), bool) or not isinstance(row.get("t"), int):
            raise LabError("Timestamp de vela inválido")
        bar = Candle(row["t"], *(number(row.get(field)) for field in ("o", "h", "l", "c", "v")))
        if bar.t <= 0 or bar.t + step > now or bar.t % step != 0:
            raise LabError("Vela abierta, futura o desalineada")
        if min(bar.o, bar.h, bar.l, bar.c) <= 0 or bar.v < 0 or bar.h < max(bar.o, bar.c, bar.l) or bar.l > min(bar.o, bar.c, bar.h):
            raise LabError("OHLC o volumen inconsistente")
        if candles:
            difference = bar.t - candles[-1].t
            if difference <= 0 or difference % step:
                raise LabError("Velas duplicadas, desordenadas o desalineadas")
            if difference > step:
                gaps.append({"after": candles[-1].t, "before": bar.t, "missing": difference // step - 1})
        candles.append(bar)
    return candles, gaps


def validate_candle_values(candles: list[Candle], timeframe: str) -> None:
    """Defiende los motores llamados directamente; no impone un reloj externo."""
    if timeframe not in INTERVALS:
        raise LabError("Temporalidad no admitida")
    step = INTERVALS[timeframe]
    for bar in candles:
        if (not isinstance(bar, Candle) or type(bar.t) is not int or bar.t <= 0
                or bar.t % step != 0):
            raise LabError("Timestamp de vela inválido o desalineado")
        values = (bar.o, bar.h, bar.l, bar.c, bar.v)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise LabError("OHLC o volumen no finito")
        if (min(bar.o, bar.h, bar.l, bar.c) <= 0 or bar.v < 0
                or bar.h < max(bar.o, bar.c, bar.l)
                or bar.l > min(bar.o, bar.c, bar.h)):
            raise LabError("OHLC o volumen inconsistente")


def validate_rule(rule: dict, depth: int = 0, count: list[int] | None = None) -> dict:
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > 64:
        raise LabError("Máximo 64 nodos de reglas por estrategia")
    if not isinstance(rule, dict) or depth > 4:
        raise LabError("Regla inválida o demasiado anidada")
    if rule.get("op") in ("AND", "OR"):
        if set(rule) - {"op", "children"}:
            raise LabError("Parámetros de regla no implementados")
        children = rule.get("children")
        if not isinstance(children, list) or not 1 <= len(children) <= 8:
            raise LabError("Un grupo requiere entre 1 y 8 reglas")
        return {"op": rule["op"], "children": [validate_rule(child, depth + 1, count) for child in children]}
    kind = rule.get("kind")
    if kind not in (*RSI_DIVERGENCE_KINDS, "RSI_LT", "RSI_GT", "CLOSE_ABOVE_EMA", "CLOSE_BELOW_EMA",
                    "EMA_CROSS_ABOVE_EMA", "EMA_CROSS_BELOW_EMA",
                    "CLOSE_ABOVE_SMA", "CLOSE_BELOW_SMA", "CLOSE_ABOVE_BB_UPPER",
                    "CLOSE_BELOW_BB_LOWER", "MACD_CROSS_ABOVE_SIGNAL",
                    "MACD_CROSS_BELOW_SIGNAL", "VOLUME_ABOVE_AVG",
                    "ATR_PCT_GT", "ATR_PCT_LT",
                    "RETEST_ABOVE_RANGE", "RETEST_BELOW_RANGE", "BEARISH_STREAK", "BULLISH_STREAK",
                    "VOLUME_RISING_STREAK", "VOLUME_ASCENDING_BARS",
                    "STRUCTURE_HH_HL", "STRUCTURE_LH_LL"):
        raise LabError("Indicador o condición no implementados")
    period = rule.get("period")
    if isinstance(period, bool) or not isinstance(period, int) or not 2 <= period <= 300:
        raise LabError("Periodo fuera de rango (2–300)")
    output = {"kind": kind, "period": period}
    if "timeframe" in rule:
        higher = rule["timeframe"]
        if not isinstance(higher, str) or higher not in INTERVALS:
            raise LabError("Temporalidad superior no admitida")
        output["timeframe"] = higher
    if kind in ("BEARISH_STREAK", "BULLISH_STREAK", "VOLUME_RISING_STREAK", "VOLUME_ASCENDING_BARS") and period > 10:
        raise LabError("La secuencia requiere entre 2 y 10 velas")
    if kind in ("RSI_LT", "RSI_GT"):
        threshold = number(rule.get("threshold"))
        if not 0 < threshold < 100:
            raise LabError("El umbral RSI debe estar entre 0 y 100")
        output["threshold"] = str(threshold)
    if kind in ("ATR_PCT_GT", "ATR_PCT_LT"):
        threshold = number(rule.get("threshold"), positive=True)
        if threshold > 1000:
            raise LabError("El umbral ATR % debe estar entre 0 y 1000")
        output["threshold"] = str(threshold)
    if kind == "VOLUME_ABOVE_AVG":
        factor = number(rule.get("factor"), positive=True)
        output["factor"] = str(factor)
    if kind in ("CLOSE_ABOVE_BB_UPPER", "CLOSE_BELOW_BB_LOWER"):
        multiplier = number(rule.get("multiplier"), positive=True)
        if multiplier > 10:
            raise LabError("Multiplicador Bollinger fuera de rango (0–10]")
        output["multiplier"] = str(multiplier)
    if kind in ("MACD_CROSS_ABOVE_SIGNAL", "MACD_CROSS_BELOW_SIGNAL"):
        slow = rule.get("slow_period")
        signal = rule.get("signal_period")
        if (type(slow) is not int or type(signal) is not int
                or not period < slow <= 300 or not 2 <= signal <= 300):
            raise LabError("MACD requiere rápida < lenta ≤ 300 y señal entre 2 y 300")
        output["slow_period"] = slow
        output["signal_period"] = signal
    if kind in ("EMA_CROSS_ABOVE_EMA", "EMA_CROSS_BELOW_EMA"):
        slow = rule.get("slow_period")
        if type(slow) is not int or not period < slow <= 300:
            raise LabError("Cruce EMA requiere rápida < lenta ≤ 300")
        output["slow_period"] = slow
    if kind in ("RETEST_ABOVE_RANGE", "RETEST_BELOW_RANGE"):
        bars = rule.get("retest_bars")
        tolerance = number(rule.get("tolerance_pct"))
        if period > 100 or isinstance(bars, bool) or not isinstance(bars, int) or not 1 <= bars <= 10:
            raise LabError("Retesteo: rango de 2–100 velas y ventana de 1–10 velas")
        if not 0 <= tolerance <= D("0.1"):
            raise LabError("Retesteo: tolerancia entre 0 y 0.1")
        output["retest_bars"] = bars
        output["tolerance_pct"] = str(tolerance)
    if kind in ("STRUCTURE_HH_HL", "STRUCTURE_LH_LL"):
        radius = rule.get("pivot_bars")
        if (type(radius) is not int or not 2 <= radius <= 10
                or period < 6 * radius + 1):
            raise LabError("Estructura: 3 giros por lado, ventana ≥ 6 × giro + 1 y giro de 2–10 velas")
        output["pivot_bars"] = radius
    if kind in RSI_DIVERGENCE_KINDS:
        radius, lookback = rule.get("pivot_bars"), rule.get("lookback_bars")
        if (type(radius) is not int or not 2 <= radius <= 10 or
                type(lookback) is not int or not radius < lookback <= 200):
            raise LabError("Divergencia RSI: giro de 2–10 velas y distancia máxima de giro + 1 hasta 200")
        output.update(pivot_bars=radius, lookback_bars=lookback)
    if set(rule) - set(output):
        raise LabError("Parámetros de regla no implementados")
    return output


def validate_strategy(raw: dict) -> dict:
    if not isinstance(raw, dict) or raw.get("direction") not in ("LONG", "SHORT"):
        raise LabError("Dirección LONG o SHORT requerida")
    if set(raw) - {"direction", "initial_capital", "allocation", "fee_rate",
                    "slippage_rate", "stop_loss", "take_profit", "entry", "exit",
                    "max_holding_bars", "trailing_stop"}:
        raise LabError("Parámetros de estrategia no implementados")
    capital = number(raw.get("initial_capital"), positive=True)
    fraction = number(raw.get("allocation"), positive=True)
    fee = number(raw.get("fee_rate"))
    slippage = number(raw.get("slippage_rate"))
    stop = number(raw.get("stop_loss"), positive=True)
    target = number(raw.get("take_profit"), positive=True)
    if fraction > 1 or not 0 <= fee < 1 or not 0 <= slippage < 1 or stop >= 1 or target >= 10:
        raise LabError("Parámetros de capital, coste o salida fuera de rango")
    count = [0]
    definition = {"direction": raw["direction"], "initial_capital": str(capital), "allocation": str(fraction),
            "fee_rate": str(fee), "slippage_rate": str(slippage), "stop_loss": str(stop),
            "take_profit": str(target), "entry": validate_rule(raw.get("entry"), count=count)}
    if "max_holding_bars" in raw:
        holding = raw["max_holding_bars"]
        if type(holding) is not int or not 1 <= holding <= 10000:
            raise LabError("Límite de permanencia: entre 1 y 10000 velas")
        definition["max_holding_bars"] = holding
    if "trailing_stop" in raw:
        trailing = number(raw["trailing_stop"], positive=True)
        if trailing >= 1:
            raise LabError("Stop móvil: distancia entre 0 y 1")
        definition["trailing_stop"] = str(trailing)
    if raw.get("exit") is not None:
        definition["exit"] = validate_rule(raw["exit"], count=count)
    return definition


def validate_rule_timeframes(strategy: dict, base_timeframe: str) -> None:
    """Rechaza combinaciones imposibles antes de activar LAB o PAPER."""
    if base_timeframe not in INTERVALS:
        raise LabError("Temporalidad base no admitida")

    def visit(node: dict) -> None:
        if "op" in node:
            for child in node["children"]:
                visit(child)
            return
        higher = node.get("timeframe")
        if higher is None:
            return
        if higher not in INTERVALS or (INTERVALS[higher] <= INTERVALS[base_timeframe]
                                      or INTERVALS[higher] % INTERVALS[base_timeframe]):
            raise LabError("La condición requiere una temporalidad superior divisible por la base")

    visit(strategy["entry"])
    if "exit" in strategy:
        visit(strategy["exit"])


def rsi(candles: list[Candle], i: int, period: int) -> Decimal | None:
    if i < period:
        return None
    gains = losses = D(0)
    for j in range(i - period + 1, i + 1):
        change = candles[j].c - candles[j - 1].c
        if change > 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return D(100) if gains > 0 else D(50)
    return D(100) - D(100) / (D(1) + gains / losses)


def ema(candles: list[Candle], i: int, period: int) -> Decimal | None:
    if i < period - 1:
        return None
    value = sum((bar.c for bar in candles[:period]), D(0)) / D(period)
    alpha = D(2) / D(period + 1)
    for bar in candles[period:i + 1]:
        value = alpha * bar.c + (D(1) - alpha) * value
    return value


def sma(candles: list[Candle], i: int, period: int) -> Decimal | None:
    """Media de cierres de la ventana terminada en la vela i, sin futuro."""
    if i < period - 1:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        return sum((bar.c for bar in candles[i - period + 1:i + 1]), D(0)) / D(period)


def bollinger(candles: list[Candle], i: int, period: int,
              multiplier: Decimal) -> tuple[Decimal, Decimal] | None:
    """SMA ± multiplicador × desviación poblacional de cierres cerrados."""
    if i < period - 1:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        window = [bar.c for bar in candles[i - period + 1:i + 1]]
        middle = sum(window, D(0)) / D(period)
        variance = sum(((close - middle) ** 2 for close in window), D(0)) / D(period)
        spread = multiplier * variance.sqrt()
        return middle + spread, middle - spread


def _ema_values(values: list[Decimal], period: int) -> list[Decimal | None]:
    """EMA con semilla SMA; auxiliar independiente de las series EMA legadas."""
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    with localcontext() as context:
        context.prec = PRECISION
        current = sum(values[:period], D(0)) / D(period)
        result[period - 1] = current
        alpha = D(2) / D(period + 1)
        for index in range(period, len(values)):
            current = alpha * values[index] + (D(1) - alpha) * current
            result[index] = current
    return result


def macd(candles: list[Candle], i: int, fast: int, slow: int,
         signal: int) -> tuple[Decimal | None, Decimal | None]:
    """MACD y señal en i; ambos ausentes hasta completar su calentamiento."""
    return IndicatorCache(candles[:i + 1]).macd(fast, slow, signal, i)


def _true_range(candles: list[Candle], i: int) -> Decimal:
    bar = candles[i]
    if i == 0:
        return bar.h - bar.l
    previous_close = candles[i - 1].c
    return max(bar.h - bar.l, abs(bar.h - previous_close), abs(bar.l - previous_close))


def atr(candles: list[Candle], i: int, period: int) -> Decimal | None:
    """ATR de Wilder: media inicial de TR y después suavizado causal."""
    if i < period - 1:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        value = sum((_true_range(candles, index) for index in range(period)), D(0)) / D(period)
        for index in range(period, i + 1):
            value = (value * D(period - 1) + _true_range(candles, index)) / D(period)
        return value


class IndicatorCache:
    """Series causales por periodo; cada punto usa sólo velas hasta su índice."""

    def __init__(self, candles: list[Candle], *, precision: int = PRECISION):
        self.candles = candles
        self.precision = precision
        self.series: dict[tuple[str, int, str | None], list[Decimal | None]] = {}
        self.macd_series: dict[tuple[int, int, int], tuple[list[Decimal | None], list[Decimal | None]]] = {}
        self.swing_series: dict[int, tuple[list[int], list[int]]] = {}
        self.higher_series: dict[tuple[str, str], tuple[list[Candle], list[int], IndicatorCache]] = {}

    def resampled(self, base_timeframe: str, higher_timeframe: str) -> tuple[list[Candle], list[int], IndicatorCache]:
        """Agrega sólo buckets UTC completos y contiguos; conserva el índice que los hizo observables."""
        with localcontext() as context:
            context.prec = self.precision
            return self._resampled(base_timeframe, higher_timeframe)

    def _resampled(self, base_timeframe: str, higher_timeframe: str) -> tuple[list[Candle], list[int], IndicatorCache]:
        key = base_timeframe, higher_timeframe
        if key not in self.higher_series:
            base_step, higher_step = INTERVALS[base_timeframe], INTERVALS[higher_timeframe]
            ratio = higher_step // base_step
            result: list[Candle] = []
            ends: list[int] = []
            bucket: list[Candle] = []
            bucket_t = -1
            for index, bar in enumerate(self.candles):
                start = bar.t // higher_step * higher_step
                if start != bucket_t:
                    bucket_t, bucket = start, []
                if not bucket and bar.t != start:
                    continue
                if bucket and bar.t != bucket[-1].t + base_step:
                    bucket = []
                    continue
                bucket.append(bar)
                if len(bucket) == ratio and bar.t + base_step == start + higher_step:
                    result.append(Candle(start, bucket[0].o,
                                         max(item.h for item in bucket),
                                         min(item.l for item in bucket), bar.c,
                                         sum((item.v for item in bucket), D(0))))
                    ends.append(index)
            self.higher_series[key] = result, ends, IndicatorCache(result, precision=self.precision)
        return self.higher_series[key]

    def swing_pivots(self, radius: int) -> tuple[list[int], list[int]]:
        """Precalcula pivotes; el evaluador sólo verá los ya confirmados."""
        if radius not in self.swing_series:
            highs, lows = [], []
            bars = self.candles
            for pivot in range(radius, len(bars) - radius):
                neighbors = (*bars[pivot - radius:pivot], *bars[pivot + 1:pivot + radius + 1])
                if all(bars[pivot].h > bar.h for bar in neighbors):
                    highs.append(pivot)
                if all(bars[pivot].l < bar.l for bar in neighbors):
                    lows.append(pivot)
            self.swing_series[radius] = highs, lows
        return self.swing_series[radius]

    def macd(self, fast: int, slow: int, signal: int,
             i: int) -> tuple[Decimal | None, Decimal | None]:
        with localcontext() as context:
            context.prec = self.precision
            return self._macd(fast, slow, signal, i)

    def _macd(self, fast: int, slow: int, signal: int,
              i: int) -> tuple[Decimal | None, Decimal | None]:
        key = fast, slow, signal
        if key not in self.macd_series:
            closes = [bar.c for bar in self.candles]
            fast_values, slow_values = _ema_values(closes, fast), _ema_values(closes, slow)
            lines = [a - b if a is not None and b is not None else None
                     for a, b in zip(fast_values, slow_values)]
            first_valid = slow - 1
            valid_lines = [value for value in lines[first_valid:] if value is not None]
            valid_signals = _ema_values(valid_lines, signal)
            signals: list[Decimal | None] = [None] * first_valid + valid_signals
            self.macd_series[key] = lines, signals
        lines, signals = self.macd_series[key]
        return lines[i], signals[i]

    def value(self, kind: str, period: int, i: int,
              multiplier: Decimal | None = None) -> Decimal | None:
        with localcontext() as context:
            context.prec = self.precision
            return self._value(kind, period, i, multiplier)

    def _value(self, kind: str, period: int, i: int,
               multiplier: Decimal | None = None) -> Decimal | None:
        key = kind, period, str(multiplier) if multiplier is not None else None
        if kind in ("BB_UPPER", "BB_LOWER"):
            if multiplier is None:
                raise LabError("Multiplicador Bollinger requerido")
            upper_key, lower_key = ("BB_UPPER", period, str(multiplier)), ("BB_LOWER", period, str(multiplier))
            if upper_key not in self.series:
                upper: list[Decimal | None] = []
                lower: list[Decimal | None] = []
                for index in range(len(self.candles)):
                    bands = bollinger(self.candles, index, period, multiplier)
                    upper.append(bands[0] if bands else None)
                    lower.append(bands[1] if bands else None)
                self.series[upper_key], self.series[lower_key] = upper, lower
            return self.series[key][i]
        if key not in self.series:
            bars = self.candles
            values: list[Decimal | None] = [None] * len(bars)
            if kind == "EMA" and len(bars) >= period:
                current = sum((bar.c for bar in bars[:period]), D(0)) / D(period)
                values[period - 1] = current
                alpha = D(2) / D(period + 1)
                for index in range(period, len(bars)):
                    current = alpha * bars[index].c + (D(1) - alpha) * current
                    values[index] = current
            elif kind == "SMA" and len(bars) >= period:
                with localcontext() as context:
                    context.prec = PRECISION
                    prefix = [D(0)]
                    for bar in bars:
                        prefix.append(prefix[-1] + bar.c)
                    for index in range(period - 1, len(bars)):
                        values[index] = (prefix[index + 1] - prefix[index + 1 - period]) / D(period)
            elif kind == "RSI":
                gain_prefix, loss_prefix = [D(0)], [D(0)]
                for index in range(1, len(bars)):
                    change = bars[index].c - bars[index - 1].c
                    gain_prefix.append(gain_prefix[-1] + max(change, D(0)))
                    loss_prefix.append(loss_prefix[-1] + max(-change, D(0)))
                for index in range(period, len(bars)):
                    gains = gain_prefix[index] - gain_prefix[index - period]
                    losses = loss_prefix[index] - loss_prefix[index - period]
                    values[index] = (D(100) if gains > 0 else D(50)) if losses == 0 else D(100) - D(100) / (D(1) + gains / losses)
            elif kind == "VOLUME":
                prefix = [D(0)]
                for bar in bars:
                    prefix.append(prefix[-1] + bar.v)
                for index in range(period, len(bars)):
                    values[index] = (prefix[index] - prefix[index - period]) / D(period)
            elif kind == "ATR" and len(bars) >= period:
                with localcontext() as context:
                    context.prec = PRECISION
                    current = sum((_true_range(bars, index) for index in range(period)), D(0)) / D(period)
                    values[period - 1] = current
                    for index in range(period, len(bars)):
                        current = (current * D(period - 1) + _true_range(bars, index)) / D(period)
                        values[index] = current
            self.series[key] = values
        return self.series[key][i]


def range_retest(rule: dict, candles: list[Candle], i: int) -> tuple[bool, dict]:
    """Ruptura de rango previo + primer retesteo confirmado al cierre.

    Sólo lee velas <= i. La vela de ruptura no puede ser la de confirmación.
    Un cierre que invalida el nivel o un retesteo anterior consumen la señal.
    """
    above = rule["kind"] == "RETEST_ABOVE_RANGE"
    period, window = rule["period"], rule["retest_bars"]
    tolerance = D(rule["tolerance_pct"])
    base = {"kind": rule["kind"], "period": period, "retest_bars": window,
            "tolerance_pct": str(tolerance), "matched": False,
            "breakout_time": None, "level": None, "observed": str(candles[i].c), "threshold": None}
    for b in range(i - 1, max(period - 1, i - window - 1), -1):
        history = candles[b - period:b]
        level = max(bar.h for bar in history) if above else min(bar.l for bar in history)
        broke = (candles[b].c > level and candles[b - 1].c <= level) if above else (
            candles[b].c < level and candles[b - 1].c >= level)
        if not broke:
            continue
        detail = {**base, "breakout_time": candles[b].t, "level": str(level),
                  "threshold": str(level), "elapsed_bars": i - b}
        def invalid(bar: Candle) -> bool:
            return bar.c < level * (D(1) - tolerance) if above else bar.c > level * (D(1) + tolerance)
        def confirmed(bar: Candle) -> bool:
            wick = bar.l if above else bar.h
            touch = level * (D(1) - tolerance) <= wick <= level * (D(1) + tolerance)
            close = bar.c > level if above else bar.c < level
            return touch and close
        middle = candles[b + 1:i]
        if any(invalid(bar) or confirmed(bar) for bar in middle):
            detail["reason"] = "INVALIDATED_OR_ALREADY_CONFIRMED"
            return False, detail
        if invalid(candles[i]):
            detail["reason"] = "INVALIDATED"
            return False, detail
        detail["matched"] = confirmed(candles[i])
        detail["retest_extreme"] = str(candles[i].l if above else candles[i].h)
        detail["reason"] = "CONFIRMED" if detail["matched"] else "NO_CONFIRMATION"
        return detail["matched"], detail
    return False, base


def _rsi_divergence(rule: dict, candles: list[Candle], i: int, cache: IndicatorCache,
                    timeframe: str | None) -> tuple[bool, dict]:
    """Compara dos giros consecutivos al confirmar el segundo, con un giro opuesto intermedio."""
    if timeframe not in INTERVALS:
        raise LabError("La divergencia RSI requiere la temporalidad base")
    radius, period, lookback = rule["pivot_bars"], rule["period"], rule["lookback_bars"]
    detail = {"kind": rule["kind"], "period": period, "pivot_bars": radius,
              "lookback_bars": lookback, "matched": False, "reason": "NO_NEW_PIVOT"}
    bullish = rule["kind"] == "RSI_BULLISH_DIVERGENCE"
    highs, lows = cache.swing_pivots(radius)
    pivots, opposite = (lows, highs) if bullish else (highs, lows)
    latest = i - radius
    at = bisect_left(pivots, latest)
    if at == len(pivots) or pivots[at] != latest:
        return False, detail
    if at < 1 or latest - pivots[at - 1] > lookback:
        return False, {**detail, "reason": "NO_PRIOR_PIVOT"}
    first = pivots[at - 1]
    between = bisect_left(opposite, latest) - 1
    if between < 0 or opposite[between] <= first:
        return False, {**detail, "reason": "NO_INTERVENING_PIVOT"}
    start = first - max(period, radius)
    if start < 0:
        return False, {**detail, "reason": "RSI_WARMUP"}
    step = INTERVALS[timeframe]
    if any(candles[index].t - candles[index - 1].t != step for index in range(start + 1, i + 1)):
        return False, {**detail, "reason": "CANDLE_GAP"}
    previous_rsi, latest_rsi = (cache.value("RSI", period, pivot) for pivot in (first, latest))
    if previous_rsi is None or latest_rsi is None:
        return False, {**detail, "reason": "RSI_WARMUP"}
    previous_price, latest_price = ((candles[first].l, candles[latest].l) if bullish
                                    else (candles[first].h, candles[latest].h))
    matched = ((latest_price < previous_price and latest_rsi > previous_rsi) if bullish else
               (latest_price > previous_price and latest_rsi < previous_rsi))
    return matched, {**detail, "reason": "CONFIRMED" if matched else "NO_DIVERGENCE",
                     "pivots": [{"t": candles[first].t, "price": str(previous_price), "rsi": str(previous_rsi)},
                                {"t": candles[latest].t, "price": str(latest_price), "rsi": str(latest_rsi)}],
                     "intervening_pivot_t": candles[opposite[between]].t,
                     "confirmed_at": candles[i].t, "confirmation_close_t": candles[i].t + step,
                     "matched": matched}


def evaluate(rule: dict, candles: list[Candle], i: int, cache: IndicatorCache | None = None,
             *, base_timeframe: str | None = None) -> tuple[bool, dict]:
    """Evalúa con la misma precisión en LAB, PAPER y llamadas independientes."""
    with localcontext() as context:
        context.prec = cache.precision if cache is not None else PRECISION
        return _evaluate(rule, candles, i, cache, base_timeframe=base_timeframe)


def _evaluate(rule: dict, candles: list[Candle], i: int, cache: IndicatorCache | None = None,
              *, base_timeframe: str | None = None) -> tuple[bool, dict]:
    if "op" in rule:
        parts = [_evaluate(child, candles, i, cache, base_timeframe=base_timeframe) for child in rule["children"]]
        matched = all(part[0] for part in parts) if rule["op"] == "AND" else any(part[0] for part in parts)
        return matched, {"op": rule["op"], "children": [part[1] for part in parts], "matched": matched}
    if "timeframe" in rule:
        higher = rule["timeframe"]
        if base_timeframe not in INTERVALS or higher not in INTERVALS:
            raise LabError("La regla multitemporal requiere temporalidad base válida")
        base_step, higher_step = INTERVALS[base_timeframe], INTERVALS[higher]
        if higher_step <= base_step or higher_step % base_step:
            raise LabError("La regla requiere una temporalidad superior divisible por la base")
        indicator = cache or IndicatorCache(candles[:i + 1])
        higher_bars, source_ends, higher_cache = indicator.resampled(base_timeframe, higher)
        index = bisect_right(source_ends, i) - 1
        current_bucket = candles[i].t // higher_step * higher_step
        expected = current_bucket if candles[i].t + base_step == current_bucket + higher_step else current_bucket - higher_step
        leaf = {key: value for key, value in rule.items() if key != "timeframe"}
        if index < 0 or higher_bars[index].t != expected:
            return False, {"kind": rule["kind"], "period": rule["period"],
                           "timeframe": higher, "base_timeframe": base_timeframe,
                           "higher_candle_t": None, "reason": "NO_COMPLETE_CANDLE",
                           "matched": False}
        if leaf["kind"] in RSI_DIVERGENCE_KINDS and source_ends[index] != i:
            return False, {"kind": rule["kind"], "period": rule["period"],
                           "timeframe": higher, "base_timeframe": base_timeframe,
                           "higher_candle_t": higher_bars[index].t, "reason": "NO_NEW_HIGHER_CLOSE",
                           "matched": False}
        matched, detail = _evaluate(leaf, higher_bars, index, higher_cache,
                                    base_timeframe=higher)
        return matched, {**detail, "timeframe": higher, "base_timeframe": base_timeframe,
                         "higher_candle_t": higher_bars[index].t}
    kind, period = rule["kind"], rule["period"]
    if kind in ("EMA_CROSS_ABOVE_EMA", "EMA_CROSS_BELOW_EMA"):
        slow = rule["slow_period"]
        indicator = cache or IndicatorCache(candles[:i + 1])
        fast_now = indicator.value("EMA", period, i)
        slow_now = indicator.value("EMA", slow, i)
        fast_before = indicator.value("EMA", period, i - 1) if i > 0 else None
        slow_before = indicator.value("EMA", slow, i - 1) if i > 0 else None
        ready = all(value is not None for value in (fast_now, slow_now, fast_before, slow_before))
        upward = kind == "EMA_CROSS_ABOVE_EMA"
        matched = bool(ready and ((fast_before <= slow_before and fast_now > slow_now)
                                  if upward else (fast_before >= slow_before and fast_now < slow_now)))
        return matched, {"kind": kind, "period": period, "slow_period": slow,
                         "observed": str(fast_now) if fast_now is not None else None,
                         "threshold": str(slow_now) if slow_now is not None else None,
                         "previous_observed": str(fast_before) if fast_before is not None else None,
                         "previous_threshold": str(slow_before) if slow_before is not None else None,
                         "matched": matched}
    if kind in ("MACD_CROSS_ABOVE_SIGNAL", "MACD_CROSS_BELOW_SIGNAL"):
        slow, signal = rule["slow_period"], rule["signal_period"]
        indicator = cache or IndicatorCache(candles[:i + 1])
        current_line, current_signal = indicator.macd(period, slow, signal, i)
        previous_line, previous_signal = indicator.macd(period, slow, signal, i - 1) if i > 0 else (None, None)
        ready = all(value is not None for value in (
            current_line, current_signal, previous_line, previous_signal))
        above = kind == "MACD_CROSS_ABOVE_SIGNAL"
        matched = bool(ready and ((previous_line <= previous_signal and current_line > current_signal)
                                  if above else (previous_line >= previous_signal and current_line < current_signal)))
        return matched, {"kind": kind, "period": period, "slow_period": slow,
                         "signal_period": signal,
                         "observed": str(current_line) if current_line is not None else None,
                         "threshold": str(current_signal) if current_signal is not None else None,
                         "previous_observed": str(previous_line) if previous_line is not None else None,
                         "previous_threshold": str(previous_signal) if previous_signal is not None else None,
                         "matched": matched}
    if kind in RSI_DIVERGENCE_KINDS:
        return _rsi_divergence(rule, candles, i, cache or IndicatorCache(candles[:i + 1]), base_timeframe)
    if kind in ("RETEST_ABOVE_RANGE", "RETEST_BELOW_RANGE"):
        return range_retest(rule, candles, i)
    if kind in ("STRUCTURE_HH_HL", "STRUCTURE_LH_LL"):
        radius = rule["pivot_bars"]
        indicator = cache or IndicatorCache(candles[:i + 1])
        high_indices, low_indices = indicator.swing_pivots(radius)
        first, last = max(0, i - period + 1), i - radius
        highs = high_indices[bisect_left(high_indices, first):bisect_right(high_indices, last)][-3:]
        lows = low_indices[bisect_left(low_indices, first):bisect_right(low_indices, last)][-3:]
        ascending = kind == "STRUCTURE_HH_HL"
        complete = len(highs) == 3 and len(lows) == 3
        sequence = sorted([(pivot, "H") for pivot in highs] + [(pivot, "L") for pivot in lows])
        alternating = complete and len({pivot for pivot, _ in sequence}) == 6 and all(
            sequence[index][1] != sequence[index + 1][1] for index in range(5))
        if ascending:
            ordered = alternating and all(candles[a].h < candles[b].h for a, b in zip(highs, highs[1:])) \
                and all(candles[a].l < candles[b].l for a, b in zip(lows, lows[1:]))
        else:
            ordered = alternating and all(candles[a].h > candles[b].h for a, b in zip(highs, highs[1:])) \
                and all(candles[a].l > candles[b].l for a, b in zip(lows, lows[1:]))
        newly_confirmed = last >= 0 and (last in highs or last in lows)
        matched = bool(ordered and newly_confirmed)
        return matched, {"kind": kind, "period": period, "pivot_bars": radius,
                         "highs": [{"t": candles[p].t, "value": str(candles[p].h)} for p in highs],
                         "lows": [{"t": candles[p].t, "value": str(candles[p].l)} for p in lows],
                         "confirmed_at": candles[i].t,
                         "alternating": alternating,
                         "newly_confirmed": newly_confirmed,
                         "matched": matched}
    if kind == "VOLUME_ASCENDING_BARS":
        # N velas ascendentes requieren N-1 comparaciones dentro de la ventana.
        # La vela anterior a la ventana no pertenece a esta estructura.
        observed = 1
        for index in range(i, max(0, i - period + 1), -1):
            if candles[index].v <= candles[index - 1].v:
                break
            observed += 1
        matched = observed >= period and i >= period - 1
        return matched, {"kind": kind, "period": period, "observed": str(observed),
                         "threshold": str(period), "matched": matched}
    if kind in ("BEARISH_STREAK", "BULLISH_STREAK", "VOLUME_RISING_STREAK"):
        observed = 0
        for index in range(i, max(-1, i - period), -1):
            bar = candles[index]
            if kind == "BEARISH_STREAK":
                current = bar.c < bar.o
            elif kind == "BULLISH_STREAK":
                current = bar.c > bar.o
            else:
                current = index == 0 or bar.v > candles[index - 1].v
            if not current:
                break
            observed += 1
        matched = observed >= period and i >= period - 1
        return matched, {"kind": kind, "period": period, "observed": str(observed),
                         "threshold": str(period), "matched": matched}
    observed: Decimal | None
    threshold: Decimal | None = None
    if kind.startswith("RSI_"):
        observed = cache.value("RSI", period, i) if cache else rsi(candles, i, period)
        threshold = D(rule["threshold"])
    elif kind.startswith("ATR_PCT_"):
        current_atr = cache.value("ATR", period, i) if cache else atr(candles, i, period)
        with localcontext() as context:
            context.prec = PRECISION
            observed = current_atr / candles[i].c * D(100) if current_atr is not None else None
        threshold = D(rule["threshold"])
    elif "EMA" in kind:
        observed = candles[i].c
        threshold = cache.value("EMA", period, i) if cache else ema(candles, i, period)
    elif "SMA" in kind:
        observed = candles[i].c
        threshold = cache.value("SMA", period, i) if cache else sma(candles, i, period)
    elif kind in ("CLOSE_ABOVE_BB_UPPER", "CLOSE_BELOW_BB_LOWER"):
        observed = candles[i].c
        multiplier = D(rule["multiplier"])
        if cache:
            threshold = cache.value("BB_UPPER" if kind == "CLOSE_ABOVE_BB_UPPER" else "BB_LOWER",
                                    period, i, multiplier)
        else:
            bands = bollinger(candles, i, period, multiplier)
            threshold = (bands[0] if kind == "CLOSE_ABOVE_BB_UPPER" else bands[1]) if bands else None
    else:
        observed = candles[i].v
        if cache:
            average = cache.value("VOLUME", period, i)
            threshold = average * D(rule["factor"]) if average is not None else None
        elif i >= period:
            threshold = sum((bar.v for bar in candles[i - period:i]), D(0)) / D(period) * D(rule["factor"])
    matched = False
    if threshold is not None and observed is not None:
        if kind == "RSI_LT": matched = observed < threshold
        elif kind == "RSI_GT": matched = observed > threshold
        elif kind == "ATR_PCT_LT": matched = observed < threshold
        elif kind == "ATR_PCT_GT": matched = observed > threshold
        elif kind in ("CLOSE_ABOVE_EMA", "CLOSE_ABOVE_SMA"): matched = observed > threshold
        elif kind in ("CLOSE_BELOW_EMA", "CLOSE_BELOW_SMA"): matched = observed < threshold
        elif kind == "CLOSE_ABOVE_BB_UPPER": matched = observed > threshold
        elif kind == "CLOSE_BELOW_BB_LOWER": matched = observed < threshold
        else: matched = observed > threshold
    detail = {"kind": kind, "period": period, "observed": str(observed) if observed is not None else None,
              "threshold": str(threshold) if threshold is not None else None, "matched": matched}
    if kind in ("CLOSE_ABOVE_BB_UPPER", "CLOSE_BELOW_BB_LOWER"):
        detail["multiplier"] = rule["multiplier"]
    return matched, detail


def run_backtest(candles: list[Candle], strategy: dict, timeframe: str = "1h", *,
                 entry_start_index: int = 0,
                 engine_version: str = CURRENT_ENGINE_VERSION) -> dict:
    """No mira el futuro para emitir señales; modela una posición cada vez."""
    if timeframe not in INTERVALS:
        raise LabError("Temporalidad no admitida")
    step = INTERVALS[timeframe]
    validate_candle_values(candles, timeframe)
    if any(current.t - previous.t != step for previous, current in zip(candles, candles[1:])):
        raise LabError("El histórico tiene huecos o velas desordenadas; no se simulan horizontes ficticios")
    if not supports_engine_version(engine_version, strategy):
        raise LabError("Versión de motor no compatible con la estrategia")
    validate_rule_timeframes(strategy, timeframe)
    if type(entry_start_index) is not int or not 0 <= entry_start_index < len(candles):
        raise LabError("Inicio de evaluación fuera del histórico")
    with localcontext() as context:
        context.prec = PRECISION
        direction = strategy["direction"]
        sign = D(1) if direction == "LONG" else D(-1)
        cash = initial = D(strategy["initial_capital"])
        allocation, fee, slip = (D(strategy[key]) for key in ("allocation", "fee_rate", "slippage_rate"))
        stop_pct, target_pct = D(strategy["stop_loss"]), D(strategy["take_profit"])
        trailing_pct = D(strategy["trailing_stop"]) if "trailing_stop" in strategy else None
        events, exit_events, trades, equity, drawdown = [], [], [], [], []
        open_trade: dict | None = None
        entry_index: int | None = None
        trailing_extreme: Decimal | None = None
        exit_pending: dict | None = None
        max_holding = strategy.get("max_holding_bars")
        peak = initial
        max_dd = D(0)
        exposure_bars = 0
        horizon_seconds = (3600, 14400, 86400, 604800)
        indicator_cache = IndicatorCache(candles)
        for i in range(entry_start_index, len(candles)):
            bar = candles[i]
            if open_trade is not None:
                entry, units = D(open_trade["entry_price"]), D(open_trade["quantity"])
                fixed_stop = entry * (D(1) - sign * stop_pct)
                trailing_level = (trailing_extreme * (D(1) - sign * trailing_pct)
                                  if trailing_pct is not None and trailing_extreme is not None else None)
                trailing_dominates = trailing_level is not None and (
                    trailing_level > fixed_stop if direction == "LONG" else trailing_level < fixed_stop)
                stop = trailing_level if trailing_dominates else fixed_stop
                stop_reason = "TRAILING_STOP" if trailing_dominates else "STOP"
                target = entry * (D(1) + sign * target_pct)
                exit_reason = None
                exit_at_open = exit_pending is not None
                if exit_pending is not None:
                    # Una regla evaluada al cierre previo ejecuta en esta
                    # apertura, antes del rango intravela actual.
                    exit_reason, raw_exit = "EXIT_RULE", bar.o
                else:
                    if engine_version not in NO_OPEN_EXIT_EXPOSURE_ENGINES:
                        exposure_bars += 1
                    stop_hit = bar.l <= stop if direction == "LONG" else bar.h >= stop
                    target_hit = bar.h >= target if direction == "LONG" else bar.l <= target
                    gap_stop = bar.o <= stop if direction == "LONG" else bar.o >= stop
                    gap_target = bar.o >= target if direction == "LONG" else bar.o <= target
                    if engine_version in ("lab-1.10", "lab-1.11", "lab-1.12", "lab-1.13", *RECENT_ENGINES) and gap_stop:
                        # La apertura es anterior a los extremos intravela.
                        exit_reason, raw_exit = stop_reason, bar.o
                        exit_at_open = True
                    elif engine_version in ("lab-1.10", "lab-1.11", "lab-1.12", "lab-1.13", *RECENT_ENGINES) and gap_target:
                        # Precio objetivo conservador: no se presume mejora
                        # aunque la apertura haya saltado más allá del límite.
                        exit_reason, raw_exit = "TARGET_OPEN", target
                        exit_at_open = True
                    elif max_holding is not None and entry_index is not None and i - entry_index >= max_holding:
                        # La posición sobrevivió a max_holding velas enteras;
                        # salida en esta apertura antes del rango intravela.
                        exit_reason, raw_exit = "TIME_LIMIT", bar.o
                        exit_at_open = True
                    elif stop_hit:
                        exit_reason = ("TRAILING_STOP_AND_TARGET_AMBIGUOUS" if trailing_dominates else "STOP_AND_TARGET_AMBIGUOUS") if target_hit else stop_reason
                        raw_exit = bar.o if gap_stop else stop
                    elif target_hit:
                        exit_reason = "TARGET"
                        raw_exit = target
                    elif i == len(candles) - 1:
                        exit_reason = "END_OF_DATA"
                        raw_exit = bar.c
                    if engine_version in NO_OPEN_EXIT_EXPOSURE_ENGINES and not exit_at_open:
                        exposure_bars += 1
                if exit_reason:
                    exit_price = raw_exit * (D(1) - sign * slip)
                    exit_fee = units * exit_price * fee
                    pnl = sign * units * (exit_price - entry) - D(open_trade["entry_fee"]) - exit_fee
                    cash += pnl
                    trades.append({**open_trade, "exit_time": bar.t, "exit_price": str(exit_price),
                                   "exit_fee": str(exit_fee), "pnl": str(pnl), "reason": exit_reason,
                                   "duration_seconds": bar.t - open_trade["entry_time"],
                                   "exit_signal_time": exit_pending["signal_time"] if exit_pending else None,
                                   "exit_explanation": exit_pending["explanation"] if exit_pending else None})
                    open_trade = None
                    entry_index = None
                    trailing_extreme = None
                    exit_pending = None
            if open_trade is not None and trailing_pct is not None and trailing_extreme is not None:
                # El extremo de esta vela sólo estará disponible para el stop
                # de la siguiente: nunca se ajusta y ejecuta dentro de ella.
                trailing_extreme = (max(trailing_extreme, bar.h) if direction == "LONG"
                                    else min(trailing_extreme, bar.l))
            if open_trade is not None and strategy.get("exit") is not None and i < len(candles) - 1:
                should_exit, exit_explanation = evaluate(strategy["exit"], candles, i, indicator_cache,
                                                         base_timeframe=timeframe)
                if should_exit:
                    exit_pending = {"signal_time": bar.t, "planned_exit_time": candles[i + 1].t,
                                    "explanation": exit_explanation}
                    exit_events.append(exit_pending)
            matched, explanation = evaluate(strategy["entry"], candles, i, indicator_cache,
                                            base_timeframe=timeframe)
            if matched:
                # Recopilar *todas* las estructuras, incluso si una posición
                # abierta impide operar. Los resultados futuros son sólo
                # anotaciones posteriores; no entran al evaluador.
                horizons = {}
                for seconds in horizon_seconds:
                    offset = seconds // step
                    horizons[str(seconds)] = str((candles[i + offset].c / bar.c - 1) * 100) if seconds % step == 0 and i + offset < len(candles) else None
                future = candles[i + 1:min(len(candles), i + 1 + 604800 // step)]
                best = max(future, key=lambda item: item.h) if future else None
                worst = min(future, key=lambda item: item.l) if future else None
                action = "SKIPPED_POSITION_OPEN" if open_trade else "SKIPPED_NO_NEXT_CANDLE" if i == len(candles) - 1 else "ENTER_NEXT_OPEN"
                if action == "ENTER_NEXT_OPEN":
                    next_bar = candles[i + 1]
                    entry_price = next_bar.o * (D(1) + sign * slip)
                    notional = cash * allocation
                    if notional <= 0:
                        action = "SKIPPED_NO_CAPITAL"
                    else:
                        units = notional / (entry_price * (D(1) + fee))
                        entry_fee = units * entry_price * fee
                        open_trade = {"signal_time": bar.t, "entry_time": next_bar.t,
                                      "entry_price": str(entry_price), "entry_fee": str(entry_fee),
                                      "quantity": str(units), "direction": direction}
                        entry_index = i + 1
                        trailing_extreme = entry_price if trailing_pct is not None else None
                favorable = best if direction == "LONG" else worst
                adverse = worst if direction == "LONG" else best
                mfe = ((favorable.h / bar.c - 1) * 100 if direction == "LONG" else
                       (1 - favorable.l / bar.c) * 100) if favorable else None
                mae = ((adverse.l / bar.c - 1) * 100 if direction == "LONG" else
                       (1 - adverse.h / bar.c) * 100) if adverse else None
                events.append({"signal_time": bar.t, "entry_time": candles[i + 1].t if action == "ENTER_NEXT_OPEN" else None,
                               "action": action, "open": str(bar.o), "high": str(bar.h), "low": str(bar.l),
                               "close": str(bar.c), "volume": str(bar.v), "explanation": explanation,
                               "outcomes_pct": horizons,
                               "mfe_pct": str(max(D(0), mfe)) if mfe is not None else None,
                               "mae_pct": str(min(D(0), mae)) if mae is not None else None,
                               "max_at": best.t if best else None, "min_at": worst.t if worst else None,
                               "favorable_at": favorable.t if favorable else None,
                               "adverse_at": adverse.t if adverse else None})
            marked = cash
            if open_trade and bar.t >= open_trade["entry_time"]:
                marked += sign * D(open_trade["quantity"]) * (bar.c - D(open_trade["entry_price"])) - D(open_trade["entry_fee"])
            peak = max(peak, marked)
            current_dd = (peak - marked) / peak if peak > 0 else D(0)
            max_dd = max(max_dd, current_dd)
            equity.append({"t": bar.t, "equity": str(marked)})
            drawdown.append({"t": bar.t, "drawdown_pct": str(current_dd * 100)})
        wins = [D(trade["pnl"]) for trade in trades if D(trade["pnl"]) > 0]
        losses = [D(trade["pnl"]) for trade in trades if D(trade["pnl"]) < 0]
        net = cash - initial
        average_win = sum(wins, D(0)) / D(len(wins)) if wins else None
        average_loss = -sum(losses, D(0)) / D(len(losses)) if losses else None
        total_fees = sum((D(trade["entry_fee"]) + D(trade["exit_fee"]) for trade in trades), D(0))
        ambiguous_exits = sum(trade["reason"] in ("STOP_AND_TARGET_AMBIGUOUS", "TRAILING_STOP_AND_TARGET_AMBIGUOUS") for trade in trades)
        metrics = {"trades": len(trades), "win_rate": str(D(len(wins)) / D(len(trades))) if trades else None,
                   "net_profit": str(net), "return_pct": str(net / initial * 100),
                   "expectancy": str(net / D(len(trades))) if trades else None,
                   "profit_factor": str(sum(wins, D(0)) / -sum(losses, D(0))) if losses else None,
                   "reward_risk_ratio": str(average_win / average_loss) if average_win is not None and average_loss else None,
                   "average_duration_seconds": str(D(sum(trade["duration_seconds"] for trade in trades)) / D(len(trades))) if trades else None,
                   "total_fees": str(total_fees), "exposure_pct": str(D(exposure_bars) / D(len(candles) - entry_start_index) * 100),
                   "sample_size": len(events), "ambiguous_exits": ambiguous_exits,
                   "max_drawdown_pct": str(max_dd * 100), "final_equity": str(cash)}
        return {"events": events, "exit_events": exit_events, "trades": trades, "equity": equity, "drawdown": drawdown, "metrics": metrics,
                "assumptions": ["Señal al cierre; entrada en la siguiente apertura", "Una posición simultánea",
                                "Regla de salida opcional al cierre; salida en la siguiente apertura",
                                "Stop prevalece ante ambigüedad intravela", "Sin liquidez ni profundidad de mercado",
                                "Resultados posteriores de eventos son descriptivos y no intervienen en las señales",
                                "RSI usa media simple de ganancias/pérdidas del periodo, no suavizado Wilder"] +
                               (["Si la apertura ya cruzó stop u objetivo, decide antes del rango intravela; el objetivo se liquida al precio límite conservador"]
                                if engine_version in ("lab-1.10", "lab-1.11", "lab-1.12", "lab-1.13", *RECENT_ENGINES) else []) +
                               (["Exposición: no cuenta la vela de salida si la posición se cierra en su apertura"]
                                if engine_version in NO_OPEN_EXIT_EXPOSURE_ENGINES else []) +
                               (["Stop móvil: sólo se ajusta con extremos de velas cerradas; prevalece el stop ante ambigüedad intravela"]
                                if trailing_pct is not None else []) +
                               (["Límite de permanencia: salida en la apertura tras " +
                                 str(max_holding) + " velas completas; stop/objetivo de apertura prevalecen"]
                                if max_holding is not None else []) +
                               (["Short sintético experimental; sin préstamo, financiación ni liquidación"] if direction == "SHORT" else [])}
