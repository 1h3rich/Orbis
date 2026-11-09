"""Decisión DCA pura para histórico y PAPER. No crea órdenes reales.

Cada decisión usa sólo la última vela cerrada. La compra se valora en la
apertura siguiente, con deslizamiento adverso y comisión incluidos en el
importe solicitado. Todos los saldos se calculan con Decimal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext
from typing import Any

from app.lab.engine import Candle, INTERVALS, LabError, number
from app.lab.incremental_indicators import EmaState


D = Decimal
ENGINE_VERSION = "dca-1.6"
SMA_ENGINE_VERSION = "dca-1.7"
AVERAGE_ENGINE_VERSION = "dca-1.8"
VOLUME_ENGINE_VERSION = "dca-1.9"
AVERAGE_PAPER_ONLY = "Para este ejecutor, el ajuste DCA por media móvil solo está disponible en LAB y PAPER"
VOLUME_LIVE_UNAVAILABLE = "Este ejecutor no admite el filtro DCA de volumen relativo"
SIGNAL_ENGINE_VERSIONS = frozenset((SMA_ENGINE_VERSION, AVERAGE_ENGINE_VERSION, VOLUME_ENGINE_VERSION))
RESERVE_ENGINE_VERSIONS = SIGNAL_ENGINE_VERSIONS | {ENGINE_VERSION}
SUPPORTED_ENGINE_VERSIONS = RESERVE_ENGINE_VERSIONS | {"dca-1.1", "dca-1.2", "dca-1.3", "dca-1.4", "dca-1.5"}
CALC_PRECISION = 300


def precision_for_version(engine_version: str) -> int:
    if engine_version not in SUPPORTED_ENGINE_VERSIONS:
        raise LabError("Motor DCA no disponible")
    return CALC_PRECISION if engine_version in RESERVE_ENGINE_VERSIONS or engine_version == "dca-1.5" else 80


def dca_engine_for_rule(rule: dict) -> str:
    if "volume_gate" in rule:
        return VOLUME_ENGINE_VERSION
    if "ma_adjustment" in rule:
        return AVERAGE_ENGINE_VERSION
    return SMA_ENGINE_VERSION if "sma_gate" in rule else ENGINE_VERSION


def dca_uses_average(rule: dict) -> bool:
    return "sma_gate" in rule or "ma_adjustment" in rule


def dca_signal_window_key(rule: dict) -> str | None:
    if "volume_gate" in rule:
        return "volume_window"
    if "sma_gate" in rule:
        return "sma_window"
    return "average_window" if "ma_adjustment" in rule else None


def require_dca_engine(rule: dict, engine_version: str) -> None:
    precision_for_version(engine_version)
    selected = dca_engine_for_rule(rule)
    if engine_version != selected and (selected in SIGNAL_ENGINE_VERSIONS or engine_version in SIGNAL_ENGINE_VERSIONS):
        if VOLUME_ENGINE_VERSION in (selected, engine_version):
            raise LabError("El motor DCA fijado no corresponde al filtro de volumen del plan")
        raise LabError("El motor DCA fijado no corresponde a la regla de media móvil del plan")


def validate_dca(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LabError("Regla DCA inválida")
    frequency = raw.get("frequency")
    if frequency not in ("daily", "weekly", "monthly"):
        raise LabError("Periodicidad DCA no admitida")
    reference_mode = raw.get("reference_mode", "initial")
    if not isinstance(reference_mode, str) or reference_mode not in ("initial", "high_watermark"):
        raise LabError("Referencia DCA no admitida")
    values = {key: number(raw.get(key), positive=key not in ("fee_rate", "slippage_rate"))
              for key in ("amount", "budget_total", "monthly_limit", "max_exposure", "fee_rate", "slippage_rate")}
    if not 0 < values["max_exposure"] <= 1 or not 0 <= values["fee_rate"] < 1 or not 0 <= values["slippage_rate"] < 1:
        raise LabError("Exposición o costes DCA fuera de rango")
    activation_limit = raw.get("max_extra_activations", 4)
    cooldown = raw.get("cooldown_bars", 0)
    if (isinstance(activation_limit, bool) or not isinstance(activation_limit, int)
            or not 0 <= activation_limit <= 1000 or isinstance(cooldown, bool)
            or not isinstance(cooldown, int) or not 0 <= cooldown <= 500):
        raise LabError("Límite de activaciones o espera DCA inválido")
    tiers = raw.get("dips", [])
    if not isinstance(tiers, list) or len(tiers) > 4:
        raise LabError("Máximo cuatro niveles de caída")
    cleaned: list[dict[str, str]] = []
    seen: set[Decimal] = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            raise LabError("Nivel de caída inválido")
        drop, amount = number(tier.get("drop_pct"), positive=True), number(tier.get("amount"), positive=True)
        if drop >= 1 or drop in seen:
            raise LabError("Los porcentajes de caída deben ser únicos y menores que 1")
        seen.add(drop)
        cleaned.append({"drop_pct": str(drop), "amount": str(amount)})
    cleaned.sort(key=lambda row: D(row["drop_pct"]))
    result = {"frequency": frequency, **{key: str(value) for key, value in values.items()},
              "max_extra_activations": activation_limit, "cooldown_bars": cooldown, "dips": cleaned}
    if "reference_mode" in raw:
        result["reference_mode"] = reference_mode
    if "sma_gate" in raw:
        gate = raw["sma_gate"]
        if (not isinstance(gate, dict) or set(gate) != {"period", "condition"}
                or type(gate["period"]) is not int or not 2 <= gate["period"] <= 120
                or gate["condition"] not in ("below", "above")):
            raise LabError("Filtro SMA inválido: elige un cierre por encima o por debajo de una SMA de 2 a 120 velas")
        result["sma_gate"] = {"period": gate["period"], "condition": gate["condition"]}
    if "ma_adjustment" in raw:
        adjustment = raw["ma_adjustment"]
        if ("sma_gate" in raw or not isinstance(adjustment, dict)
                or set(adjustment) != {"kind", "period", "condition", "matched_factor", "unmatched_factor"}
                or adjustment["kind"] not in ("sma", "ema")
                or type(adjustment["period"]) is not int or not 2 <= adjustment["period"] <= 120
                or adjustment["condition"] not in ("below", "above")):
            raise LabError("Ajuste por media móvil DCA inválido")
        matched = number(adjustment["matched_factor"])
        unmatched = number(adjustment["unmatched_factor"])
        if not 0 <= matched <= 10 or not 0 <= unmatched <= 10 or matched == unmatched:
            raise LabError("Usa dos multiplicadores distintos entre 0 y 10")
        result["ma_adjustment"] = adjustment | {"matched_factor": str(matched), "unmatched_factor": str(unmatched)}
    if "volume_gate" in raw:
        gate = raw["volume_gate"]
        if (dca_uses_average(result) or not isinstance(gate, dict)
                or set(gate) != {"period", "min_ratio"} or type(gate["period"]) is not int
                or not 2 <= gate["period"] <= 120):
            raise LabError("Filtro de volumen DCA inválido: usa entre 2 y 120 velas, sin otro filtro")
        ratio = number(gate["min_ratio"], positive=True)
        if ratio > 100:
            raise LabError("El volumen relativo mínimo debe ser mayor que 0 y como máximo 100")
        result["volume_gate"] = {"period": gate["period"], "min_ratio": str(ratio)}
    return result


def _period_key(timestamp: int, frequency: str) -> str:
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    if frequency == "daily":
        return dt.strftime("%Y-%m-%d")
    if frequency == "weekly":
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"
    return dt.strftime("%Y-%m")


def dca_trigger_candidates(rule: dict, *, signal_close: Decimal, reference: Decimal,
                           period: str, last_period: str | None, triggered: set[str],
                           extra_count: int, index: int, last_extra_index: int | None,
                           mode: str = "conditional", allow_average: bool = False,
                           allow_volume: bool = False) -> list[tuple[str, Decimal, dict]]:
    """Sólo condiciones. No recibe fondos ni calcula ejecuciones ficticias/reales."""
    if dca_uses_average(rule) and not allow_average:
        raise LabError(AVERAGE_PAPER_ONLY)
    if "volume_gate" in rule and not allow_volume:
        raise LabError(VOLUME_LIVE_UNAVAILABLE)
    candidates = []
    if mode == "lump":
        if index == 1:
            candidates.append(("LUMP", D(rule["budget_total"]), {"signal_close": str(signal_close)}))
    elif period != last_period:
        candidates.append(("BASE", D(rule["amount"]), {"signal_close": str(signal_close)}))
    if mode == "conditional":
        for tier in rule["dips"]:
            key = tier["drop_pct"]
            if (key not in triggered and extra_count < rule["max_extra_activations"]
                    and signal_close <= reference * (D(1) - D(key))
                    and (last_extra_index is None or index-last_extra_index >= rule["cooldown_bars"])):
                candidates.append(("DIP", D(tier["amount"]), {"signal_close": str(signal_close), "drop_pct": key}))
    return candidates


def dca_sma_check(gate: dict, signal_bars: Sequence[Candle], *,
                  signal_time: int, signal_close: Decimal) -> tuple[str | None, dict]:
    """Compara el último cierre con la SMA de cierres conocidos hasta esa señal."""
    period, condition = gate["period"], gate["condition"]
    signal = {"sma_period": period, "sma_condition": condition, "sma_value": None}
    if signal_bars and (signal_bars[-1].t != signal_time or signal_bars[-1].c != signal_close):
        raise LabError("La ventana SMA no termina en el cierre de señal")
    if len(signal_bars) < period:
        return "SMA_WARMUP", signal
    with localcontext(Context(prec=CALC_PRECISION)):
        total = sum((bar.c for bar in signal_bars[-period:]), D(0))
        scaled_close = signal_close * period
        matches = scaled_close < total if condition == "below" else scaled_close > total
        signal["sma_value"] = str(total / period)
    return (None if matches else "SMA_CONDITION_NOT_MET"), signal


def dca_volume_check(gate: dict, signal_bars: Sequence[Candle], *,
                     signal_time: int, signal_close: Decimal) -> tuple[str | None, dict]:
    """Compare signal volume with the mean of the previous closed bars."""
    period, threshold = gate["period"], D(gate["min_ratio"])
    evidence = {"volume_period": period, "volume_min_ratio": str(threshold), "volume_ratio": None}
    if not signal_bars or signal_bars[-1].t != signal_time or signal_bars[-1].c != signal_close:
        raise LabError("La ventana de volumen no termina en el cierre de señal")
    if len(signal_bars) <= period:
        return "VOLUME_WARMUP", evidence
    with localcontext(Context(prec=CALC_PRECISION)):
        previous = sum((bar.v for bar in signal_bars[-period - 1:-1]), D(0))
        if previous == 0:
            return "VOLUME_BASELINE_ZERO", evidence
        scaled_signal = signal_bars[-1].v * period
        evidence["volume_ratio"] = str(scaled_signal / previous)
        return (None if scaled_signal >= threshold * previous else "VOLUME_BELOW_MINIMUM"), evidence


def seed_dca_ema(rule: dict, closes: Iterable[Decimal]) -> EmaState | None:
    adjustment = rule.get("ma_adjustment")
    if adjustment is None or adjustment["kind"] != "ema":
        return None
    state = EmaState(adjustment["period"], CALC_PRECISION)
    for close in closes:
        _push_dca_ema(state, close)
    return state


def _push_dca_ema(state: EmaState, close: Decimal) -> Decimal | None:
    previous = state.current
    with localcontext(Context(prec=CALC_PRECISION)):
        state.push(close)
    # Una EMA exactamente igual al cierre no cambia en este paso, incluso
    # cuando los productos de la recurrencia deben redondearse por separado.
    if previous == close:
        state.current = previous
    return state.current


def dca_average_decision(adjustment: dict, signal_bars: Sequence[Candle], *,
                         signal_time: int, signal_close: Decimal, ema: EmaState | None) -> tuple[Decimal | None, dict]:
    """Ajusta el importe con el cierre conocido y avanza la EMA una sola vez."""
    if adjustment["kind"] == "sma":
        rejection, sma = dca_sma_check(adjustment, signal_bars, signal_time=signal_time, signal_close=signal_close)
        value = sma["sma_value"]
        matches = None if value is None else rejection is None
    else:
        if ema is None or ema.period != adjustment["period"] or ema.precision != CALC_PRECISION:
            raise LabError("Estado EMA DCA incompatible")
        average = _push_dca_ema(ema, signal_close)
        value = None if average is None else str(average)
        matches = None if average is None else (
            signal_close < average if adjustment["condition"] == "below" else signal_close > average)
    factor = None if matches is None else D(adjustment["matched_factor" if matches else "unmatched_factor"])
    return factor, {"ma_kind": adjustment["kind"], "ma_period": adjustment["period"],
                    "ma_value": value, "ma_matched": matches,
                    "amount_factor": None if factor is None else str(factor)}


def live_dca_average(rule: dict, bars: Sequence[Candle]) -> tuple[Decimal, str | None, dict]:
    """Reconstruct the LAB/PAPER signal from an approved, contiguous prefix."""
    signal = {"signal_time": bars[-1].t, "signal_close": bars[-1].c}
    gate = rule.get("sma_gate")
    if gate is not None:
        rejection, evidence = dca_sma_check(gate, bars, **signal)
        return D(1), "DCA_" + rejection if rejection else None, evidence
    ema = seed_dca_ema(rule, (bar.c for bar in bars[:-1]))
    factor, evidence = dca_average_decision(rule["ma_adjustment"], bars, ema=ema, **signal)
    if factor is None:
        return D(0), "DCA_MA_WARMUP", evidence
    return factor, "DCA_MA_ZERO_AMOUNT" if not factor else None, evidence


def live_dca_signal(rule: dict, bars: Sequence[Candle]) -> tuple[Decimal, str | None, dict]:
    if "volume_gate" in rule:
        bar = bars[-1]
        rejection, evidence = dca_volume_check(rule["volume_gate"], bars,
                                                signal_time=bar.t, signal_close=bar.c)
        return D(1), "DCA_" + rejection if rejection else None, evidence
    return live_dca_average(rule, bars)


def adjusted_dca_request(amount: Decimal, factor: Decimal) -> Decimal:
    with localcontext(Context(prec=CALC_PRECISION)):
        return amount * factor


@dataclass
class DcaState:
    """Estado ficticio incremental compartido por histórico y PAPER."""

    cash: Decimal
    reference: Decimal
    units: Decimal = D(0)
    spent: Decimal = D(0)
    fees: Decimal = D(0)
    last_period: str | None = None
    last_extra_index: int | None = None
    triggered: set[str] = field(default_factory=set)
    extra_count: int = 0
    monthly_spent: dict[str, Decimal] = field(default_factory=dict)
    ema: EmaState | None = None

    def json(self) -> dict:
        result = {"cash": str(self.cash), "reference": str(self.reference),
                "units": str(self.units), "spent": str(self.spent), "fees": str(self.fees),
                "last_period": self.last_period, "last_extra_index": self.last_extra_index,
                "triggered": sorted(self.triggered), "extra_count": self.extra_count,
                "monthly_spent": {key: str(value) for key, value in self.monthly_spent.items()}}
        if self.ema is not None:
            result["ema"] = self.ema.snapshot()
        return result

    @classmethod
    def from_json(cls, row: dict) -> "DcaState":
        return cls(cash=D(row["cash"]), reference=D(row["reference"]),
                   units=D(row["units"]), spent=D(row["spent"]), fees=D(row["fees"]),
                   last_period=row["last_period"], last_extra_index=row["last_extra_index"],
                   triggered=set(row["triggered"]), extra_count=row["extra_count"],
                   monthly_spent={key: D(value) for key, value in row["monthly_spent"].items()},
                   ema=EmaState.restore(row["ema"]) if "ema" in row else None)


def validate_dca_state(state: DcaState, rule: dict, *, tick_index: int,
                       engine_version: str = ENGINE_VERSION) -> None:
    """Rechaza saldos PAPER alterados antes de evaluar otra compra.

    La precisión fijada por el motor conserva la semántica de sesiones antiguas.
    """
    require_dca_engine(rule, engine_version)
    adjustment = rule.get("ma_adjustment")
    ema_expected = adjustment is not None and adjustment["kind"] == "ema"
    if (ema_expected != (state.ema is not None)
            or state.ema is not None and (state.ema.period != adjustment["period"]
                or state.ema.precision != CALC_PRECISION or state.ema.count < tick_index
                or state.ema.seed_sum <= 0 or state.ema.current is not None and state.ema.current <= 0
                or state.ema.previous is not None and state.ema.previous <= 0)):
        raise LabError("Estado EMA DCA no verificable")
    if (type(tick_index) is not int or tick_index < 0
            or type(state.extra_count) is not int or state.extra_count < 0
            or state.extra_count > rule["max_extra_activations"]
            or state.last_extra_index is not None and
            (type(state.last_extra_index) is not int or not 0 <= state.last_extra_index <= tick_index)
            or state.last_period is not None and not isinstance(state.last_period, str)
            or not isinstance(state.triggered, set)
            or not isinstance(state.monthly_spent, dict)):
        raise LabError("Estado DCA no verificable")
    allowed_dips = {tier["drop_pct"] for tier in rule["dips"]}
    if (state.triggered - allowed_dips or state.extra_count != len(state.triggered)
            or state.extra_count == 0 and state.last_extra_index is not None
            or state.extra_count > 0 and state.last_extra_index is None
            or any(not isinstance(key, str) or len(key) != 7 or key[4] != "-"
                   for key in state.monthly_spent)):
        raise LabError("Estado DCA no verificable")
    amounts = (state.cash, state.reference, state.units, state.spent, state.fees,
               *state.monthly_spent.values())
    if (any(not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in amounts) or state.reference == 0):
        raise LabError("Estado DCA no verificable")
    with localcontext() as context:
        context.prec = precision_for_version(engine_version)
        budget = D(rule["budget_total"])
        if (state.cash > budget or state.spent > budget or state.fees > state.spent
                or state.cash + state.spent != budget
                or sum(state.monthly_spent.values(), D(0)) != state.spent):
            raise LabError("Estado DCA no verificable")


def apply_dca_step(state: DcaState, rule: dict, *, signal_time: int, signal_close: Decimal,
                   execution_time: int, execution_price: Decimal, mark_price: Decimal,
                   index: int, mode: str = "conditional",
                   engine_version: str = ENGINE_VERSION,
                   signal_bars: list[Candle] | None = None) -> tuple[list[dict], list[dict]]:
    """Decide con un cierre conocido y aplica compras ficticias al precio dado.

    En histórico, precio y marca son la apertura siguiente; en PAPER el precio
    es ask observado y la marca es bid. Ninguno equivale a un fill real.
    """
    require_dca_engine(rule, engine_version)
    with localcontext(Context(prec=CALC_PRECISION) if dca_signal_window_key(rule) else None) as context:
        context.prec = precision_for_version(engine_version)
        return _apply_dca_step(state, rule, signal_time=signal_time, signal_close=signal_close,
                               execution_time=execution_time, execution_price=execution_price,
                               mark_price=mark_price, index=index, mode=mode,
                               engine_version=engine_version, signal_bars=signal_bars)


def _apply_dca_step(state: DcaState, rule: dict, *, signal_time: int, signal_close: Decimal,
                    execution_time: int, execution_price: Decimal, mark_price: Decimal,
                    index: int, mode: str, engine_version: str,
                    signal_bars: list[Candle] | None) -> tuple[list[dict], list[dict]]:
    if (mode not in ("conditional", "fixed", "lump")
            or execution_price <= 0 or mark_price <= 0):
        raise LabError("Paso DCA inválido")
    month = datetime.fromtimestamp(execution_time, timezone.utc).strftime("%Y-%m")
    period = _period_key(execution_time, rule["frequency"])
    gate = rule.get("sma_gate") if mode == "conditional" else None
    adjustment = rule.get("ma_adjustment") if mode == "conditional" else None
    volume = rule.get("volume_gate") if mode == "conditional" else None
    rejection, signal_evidence = (None, {})
    factor = D(1)
    if gate is not None:
        if signal_bars is None:
            raise LabError("Falta la ventana de cierres de la SMA")
        rejection, signal_evidence = dca_sma_check(gate, signal_bars, signal_time=signal_time, signal_close=signal_close)
    if adjustment is not None:
        if adjustment["kind"] == "sma" and signal_bars is None:
            raise LabError("Falta la ventana de cierres de la SMA")
        factor, signal_evidence = dca_average_decision(adjustment, signal_bars or [],
            signal_time=signal_time, signal_close=signal_close, ema=state.ema)
        rejection = "MA_WARMUP" if factor is None else "MA_ZERO_AMOUNT" if factor == 0 else None
    if volume is not None:
        rejection, signal_evidence = dca_volume_check(volume, signal_bars or [],
            signal_time=signal_time, signal_close=signal_close)
    if rule.get("reference_mode", "initial") == "high_watermark":
        # Sólo el cierre de señal ya conocido puede elevar la referencia.
        state.reference = max(state.reference, signal_close)
    fee_rate, slip = D(rule["fee_rate"]), D(rule["slippage_rate"])
    candidates = dca_trigger_candidates(rule, signal_close=signal_close, reference=state.reference,
        period=period, last_period=state.last_period, triggered=state.triggered, extra_count=state.extra_count,
        index=index, last_extra_index=state.last_extra_index, mode=mode, allow_average=True, allow_volume=True)
    if rejection is None and mode != "lump" and period != state.last_period:
        state.last_period = period
    events, skipped = [], []
    for kind, requested, signal in candidates:
        signal.update(signal_evidence)
        if adjustment is not None:
            signal["base_requested"] = str(requested)
            requested *= factor if factor is not None else D(0)
        if rejection is not None:
            skipped.append({"time": execution_time, "kind": kind, "requested": str(requested),
                            "reason": rejection, **signal})
            continue
        if kind == "DIP" and (state.extra_count >= rule["max_extra_activations"]
                or (state.last_extra_index is not None and index - state.last_extra_index < rule["cooldown_bars"])):
            skipped.append({"time": execution_time, "kind": kind, "requested": str(requested),
                            "reason": "ACTIVATION_OR_COOLDOWN_LIMIT", **signal})
            continue
        fill = execution_price * (D(1) + slip)
        equity_open = state.cash + state.units * mark_price
        exposure_room = max(D(0), D(rule["max_exposure"]) * equity_open - state.units * mark_price)
        # Sea k = valor adquirido a precio de marca por unidad monetaria
        # gastada. Tras gastar x, activo=A+kx y patrimonio=E-(1-k)x.
        # Resolver A+kx <= m*(E-(1-k)x) evita superar el porcentaje
        # configurado por efecto de comisión, spread o deslizamiento.
        marked_per_spent = mark_price / (fill * (D(1) + fee_rate))
        exposure_spend_room = exposure_room / (marked_per_spent + D(rule["max_exposure"]) * (D(1) - marked_per_spent))
        # La compra única es el referente buy-and-hold: desde 1.3 no se
        # fracciona por un límite pensado para aportaciones periódicas.
        # Mantener el comportamiento anterior permite repetir runs antiguos.
        monthly_room = (state.cash if kind == "LUMP" and engine_version not in ("dca-1.1", "dca-1.2")
                        else max(D(0), D(rule["monthly_limit"]) - state.monthly_spent.get(month, D(0))))
        actual = min(requested, state.cash, monthly_room, exposure_spend_room)
        if actual <= 0:
            skipped.append({"time": execution_time, "kind": kind, "requested": str(requested),
                            "reason": "BUDGET_OR_EXPOSURE_LIMIT", **signal})
            continue
        commission = actual * fee_rate / (D(1) + fee_rate)
        acquired = (actual - commission) / fill
        state.cash -= actual
        state.units += acquired
        state.spent += actual
        state.fees += commission
        state.monthly_spent[month] = state.monthly_spent.get(month, D(0)) + actual
        events.append({"kind": kind, "signal_time": signal_time, "execution_time": execution_time,
                       "requested": str(requested), "spent": str(actual), "price": str(fill),
                       "units": str(acquired), "fee": str(commission), **signal})
        if kind == "DIP":
            state.triggered.add(signal["drop_pct"])
            state.extra_count += 1
            state.last_extra_index = index
    return events, skipped


def _window_reference(candles: list[Candle], rule: dict, entry_start_index: int) -> Candle:
    if entry_start_index > 1 and rule.get("reference_mode") == "high_watermark":
        return max(candles[:entry_start_index], key=lambda bar: bar.c)
    return candles[0]


def _simulate_one(candles: list[Candle], rule: dict, mode: str, engine_version: str,
                  *, entry_start_index: int = 1) -> dict:
    with localcontext(Context(prec=CALC_PRECISION) if dca_signal_window_key(rule) else None) as context:
        context.prec = precision_for_version(engine_version)
        budget = D(rule["budget_total"])
        state = DcaState(cash=budget, reference=_window_reference(candles, rule, entry_start_index).c)
        if mode == "conditional":
            state.ema = seed_dca_ema(rule, (candles[i].c for i in range(entry_start_index - 1)))
        events, skipped, equity, drawdown = [], [], [], []
        marked_values: list[Decimal] = []
        peak, max_dd = budget, D(0)
        average = rule.get("sma_gate") or rule.get("ma_adjustment")
        window_size = 0
        if mode == "conditional":
            if "volume_gate" in rule:
                window_size = rule["volume_gate"]["period"] + 1
            elif average and average.get("kind", "sma") == "sma":
                window_size = average["period"]

        for i in range(entry_start_index - 1, len(candles)):
            bar = candles[i]
            if i >= entry_start_index:
                previous = candles[i - 1]
                new_events, new_skipped = apply_dca_step(
                    state, rule, signal_time=previous.t, signal_close=previous.c,
                    execution_time=bar.t, execution_price=bar.o, mark_price=bar.o,
                    index=i - entry_start_index + 1, mode=mode, engine_version=engine_version,
                    signal_bars=candles[max(0, i - window_size):i] if window_size else None)
                events.extend(new_events)
                skipped.extend(new_skipped)
            marked = state.cash + state.units * bar.c
            marked_values.append(marked)
            peak = max(peak, marked)
            dd = (peak - marked) / peak if peak > 0 else D(0)
            max_dd = max(max_dd, dd)
            equity.append({"t": bar.t, "value": str(marked)})
            drawdown.append({"t": bar.t, "pct": str(dd * 100)})

        final_value = state.units * candles[-1].c
        metrics = {"buys": len(events), "conditional_activations": state.extra_count,
                            "invested": str(state.spent), "units": str(state.units), "average_cost": str(state.spent / state.units) if state.units else None,
                            "fees": str(state.fees), "remaining_cash": str(state.cash), "asset_value": str(final_value),
                            "final_equity": str(state.cash + final_value), "net_profit": str(final_value - state.spent),
                            "return_on_invested_pct": str((final_value - state.spent) / state.spent * 100) if state.spent else None,
                            "max_drawdown_pct": str(max_dd * 100)}
        if engine_version not in ("dca-1.1", "dca-1.2", "dca-1.3"):
            # Mismo denominador para todas las variantes. El efectivo sin
            # invertir sigue en patrimonio; no se confunde con beneficio.
            metrics["return_on_initial_capital_pct"] = str((state.cash + final_value - budget) / budget * 100)
        if engine_version in RESERVE_ENGINE_VERSIONS:
            # Desviación poblacional de los retornos de patrimonio entre
            # cierres consecutivos. El efectivo no invertido forma parte de
            # cada punto; las tres variantes comparten el mismo calendario.
            returns = [marked_values[i] / marked_values[i - 1] - D(1)
                       for i in range(1, len(marked_values))]
            mean = sum(returns, D(0)) / len(returns)
            variance = sum(((value - mean) ** 2 for value in returns), D(0)) / len(returns)
            metrics["equity_volatility_per_bar_pct"] = str(variance.sqrt() * 100)
            metrics["equity_return_observations"] = len(returns)
        return {"mode": mode, "events": events, "skipped": skipped, "equity": equity, "drawdown": drawdown,
                "metrics": metrics}


def simulate_dca(candles: list[Candle], rule: dict, *, timeframe: str,
                 engine_version: str | None = None, entry_start_index: int = 1) -> dict:
    engine_version = dca_engine_for_rule(rule) if engine_version is None else engine_version
    require_dca_engine(rule, engine_version)
    if not isinstance(candles, list) or len(candles) < 3:
        raise LabError("Se requieren al menos tres velas cerradas para DCA")
    if type(entry_start_index) is not int or not 1 <= entry_start_index < len(candles):
        raise LabError("Inicio de ventana DCA inválido")
    average = rule.get("sma_gate") or rule.get("ma_adjustment")
    if average is not None and len(candles) <= average["period"]:
        raise LabError("El histórico necesita al menos una vela más que el periodo de la media móvil")
    if "volume_gate" in rule and len(candles) < rule["volume_gate"]["period"] + 2:
        raise LabError("El histórico necesita la ventana de volumen, la señal y una apertura posterior")
    if entry_start_index != 1 and engine_version not in RESERVE_ENGINE_VERSIONS:
        raise LabError("El motor DCA fijado no admite una ventana de reserva")
    if timeframe not in INTERVALS:
        raise LabError("Temporalidad DCA no admitida")
    if not all(isinstance(bar, Candle) and type(bar.t) is int for bar in candles):
        raise LabError("Velas DCA inválidas")
    step = INTERVALS[timeframe]
    if any(
        type(bar.t) is not int or bar.t <= 0 or bar.t % step != 0
        or any(type(value) is not Decimal or not value.is_finite()
               for value in (bar.o, bar.h, bar.l, bar.c, bar.v))
        or min(bar.o, bar.h, bar.l, bar.c) <= 0 or bar.v < 0
        or bar.h < max(bar.o, bar.c, bar.l) or bar.l > min(bar.o, bar.c, bar.h)
        for bar in candles
    ) or any(current.t - previous.t != step for previous, current in zip(candles, candles[1:])):
        raise LabError("Histórico DCA con huecos, velas desordenadas o datos inválidos")
    reference_note = ("Caídas medidas desde el máximo de cierres ya observados y activadas una vez por nivel"
                      if rule.get("reference_mode") == "high_watermark"
                      else "Caídas medidas desde el primer cierre del dataset y activadas una vez por nivel")
    assumptions = ["Decisión con vela cerrada; compra en la apertura siguiente",
                   reference_note,
                   ("Las tres variantes comparten presupuesto, límite mensual, exposición, comisión y slippage"
                    if engine_version in ("dca-1.1", "dca-1.2") else
                    "Las tres variantes comparten presupuesto, exposición, comisión y slippage; el límite mensual solo rige las compras periódicas y condicionadas"),
                   "Compra inicial intenta invertir el presupuesto completo en la primera apertura disponible",
                   "No hay órdenes reales ni PAPER ni se conoce el precio futuro al decidir"]
    if engine_version in RESERVE_ENGINE_VERSIONS:
        assumptions.append("Volatilidad del patrimonio: desviación poblacional de retornos entre cierres; incluye efectivo y no se anualiza")
    if engine_version == SMA_ENGINE_VERSION:
        assumptions.append("La SMA incluye el último cierre conocido y filtra solo las compras de la variante condicionada. La igualdad no habilita una compra.")
    if engine_version == AVERAGE_ENGINE_VERSION:
        assumptions.append("SMA o EMA de cierres conocidos. La EMA comienza con la SMA inicial y conserva todo el prefijo conocido. La igualdad usa el importe alternativo.")
        assumptions.append("El factor modifica tanto la aportación periódica como los extras. Se mantienen los límites de presupuesto y exposición.")
    if engine_version == VOLUME_ENGINE_VERSION:
        assumptions.append("El volumen de señal se compara con la media de las velas cerradas anteriores. Sin media positiva o sin cobertura, la compra condicionada se omite.")
    anchor = _window_reference(candles, rule, entry_start_index)
    result = {"reference_close": str(anchor.c), "reference_time": anchor.t,
            "rule": rule, "results": {mode: _simulate_one(candles, rule, mode, engine_version,
                                                         entry_start_index=entry_start_index)
                                    for mode in ("conditional", "fixed", "lump")},
            "assumptions": assumptions}
    if entry_start_index != 1:
        result["evaluation_window"] = {"entry_start_index": entry_start_index,
            "context_t": candles[entry_start_index - 1].t, "from_t": candles[entry_start_index].t,
            "to_t": candles[-1].t, "known_prefix_bars": entry_start_index}
        assumptions.append("Reserva con presupuesto nuevo, sin unidades, consumos ni activaciones previas; sólo la referencia conserva precios ya conocidos")
    return result
