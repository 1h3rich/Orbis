"""Planificador puro LONG: señales LAB/PAPER y posición exclusivamente REAL.

La salida usa precio/coste de fills reales, nunca la posición ficticia. Cada
plan es provisional: las comprobaciones del instrumento y el guardián REAL
se repetirán antes de cualquier POST.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from math import lcm
from typing import Literal

from app.lab.engine import INTERVALS, validate_strategy
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.policy import LiveSpotPolicy, LiveSpotState, _money, _utc
from app.live_execution.spot_bot_source import SpotBotObservation


class SpotBotPlanError(ValueError):
    """El plan no puede construirse con capital y observaciones verificables."""


@dataclass(frozen=True, slots=True)
class SpotBotPosition:
    units: Decimal
    cost_quote: Decimal
    entry_candle_t: int | None
    last_entry_intent_id: str | None

    def __post_init__(self):
        for name in ("units", "cost_quote"):
            value = getattr(self, name)
            if (type(value) is not Decimal or not value.is_finite() or value < 0
                    or len(value.as_tuple().digits) > 300 or abs(value.as_tuple().exponent) > 300):
                raise SpotBotPlanError("La posición propia contiene un decimal inválido")
        if ((self.units == 0) != (self.cost_quote == 0)
                or self.units > 0 and (type(self.entry_candle_t) is not int or not self.last_entry_intent_id)):
            raise SpotBotPlanError("La posición propia del bot no tiene coste o procedencia")


@dataclass(frozen=True, slots=True)
class SpotBotCursor:
    last_candle_t: int
    last_checked_at: datetime
    trailing_high: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SpotBotPlan:
    action: Literal["WAIT", "BUY", "SELL", "PAUSE"]
    reason: str
    candle_t: int
    quantity_base: Decimal = Decimal(0)
    limit_price_quote: Decimal = Decimal(0)
    estimated_fee_quote: Decimal = Decimal(0)
    trailing_high: Decimal | None = None
    dca: dict | None = None


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _base_step(rules: MexcSpotRules) -> Decimal:
    """Múltiplo común exacto de LOT_SIZE y precisión, no un simple máximo."""
    precision_step = Decimal(1).scaleb(-rules.base_precision)
    exponent = min(rules.base_step.as_tuple().exponent, precision_step.as_tuple().exponent)
    scale = Decimal(1).scaleb(exponent)
    return Decimal(lcm(int(rules.base_step / scale), int(precision_step / scale))) * scale


def plan_spot_bot(
    policy: LiveSpotPolicy, state: LiveSpotState, rules: MexcSpotRules,
    observation: SpotBotObservation, position: SpotBotPosition, cursor: SpotBotCursor, *,
    entry_budget_quote: Decimal, now: datetime,
) -> SpotBotPlan:
    """Un paso sin efectos: sin futuros, piramidación ni ventas de otras unidades.

    Una interrupción mayor de 30 s con posición requiere revisión. Una vela
    saltada no se reconstruye para enviar órdenes retrospectivas. Las ventas
    reservan también la posible comisión en base; el polvo no negociable se
    conserva y nunca se presenta como liquidación completa.
    """
    now = _utc(now, "now")
    _money(entry_budget_quote, "entry_budget_quote", positive=True)
    definition = validate_strategy(observation.definition)
    last_checked = _utc(cursor.last_checked_at, "last_checked_at")
    if (not policy.enabled or policy.reconciliation_anchor_id is None
            or observation.version_id != policy.strategy_version_id
            or observation.symbol.replace("/", "") != policy.symbol
            or definition["direction"] != "LONG"
            or state.account_ref != policy.account_ref or state.symbol != policy.symbol
            or rules.symbol != policy.symbol or rules.base_asset != policy.base_asset
            or rules.quote_asset != policy.quote_asset
            or entry_budget_quote > policy.max_order_quote or last_checked > now):
        raise SpotBotPlanError("El bot, la política, el presupuesto o el reloj no coinciden")
    for stamp in (state.observed_at, state.quote_at, rules.observed_at):
        if stamp > now or (now - stamp).total_seconds() > policy.max_data_age_seconds:
            raise SpotBotPlanError("Los datos REAL están caducados")
    if (type(rules.base_step) is not Decimal or not rules.base_step.is_finite()
            or rules.base_step <= 0 or abs(rules.base_step.as_tuple().exponent) > 100
            or type(rules.fee_rate_upper) is not Decimal or not rules.fee_rate_upper.is_finite()
            or rules.fee_rate_upper < 0 or rules.fee_rate_upper > 1
            or type(rules.quote_precision) is not int or not 0 <= rules.quote_precision <= 18
            or type(rules.base_precision) is not int or not 0 <= rules.base_precision <= 18):
        raise SpotBotPlanError("Las reglas del instrumento son inválidas")
    step = INTERVALS[observation.timeframe]

    def result(action, reason, *, high=cursor.trailing_high):
        return SpotBotPlan(action, reason, observation.candle_t, trailing_high=high)

    if state.stopped or not state.reconciled or now >= policy.expires_at:
        return result("PAUSE", "REAL_ACCOUNT_UNAVAILABLE")
    if not state.account_verified or not state.instrument_verified:
        return result("PAUSE", "REAL_IDENTITY_UNVERIFIED")
    if state.quote_at < last_checked:
        return result("PAUSE", "QUOTE_CLOCK_REGRESSED")
    if (observation.candle_t < cursor.last_candle_t
            or (observation.candle_t - cursor.last_candle_t) % step
            or observation.candle_t > cursor.last_candle_t + step):
        return result("PAUSE", "CANDLE_CONTINUITY_LOST")
    due_t = int(now.timestamp()) // step * step - step
    if observation.candle_t < due_t - step or observation.candle_t > due_t:
        return result("PAUSE", "PAPER_OBSERVATION_STALE")
    if (observation.candle_t == cursor.last_candle_t and observation.candle_t < due_t
            and int(now.timestamp()) - due_t - step > policy.max_data_age_seconds):
        return result("PAUSE", "PAPER_CANDLE_NOT_OBSERVED")
    if position.units > 0 and (now - last_checked).total_seconds() > 30:
        return result("PAUSE", "REAL_RISK_OBSERVATION_GAP")
    if position.units > state.free_base:
        return result("PAUSE", "OWNED_POSITION_EXCEEDS_FREE_BALANCE")
    if state.pending_base > 0 or state.pending_quote > 0:
        return result("WAIT", "ORDER_PENDING")
    new_candle = observation.candle_t > cursor.last_candle_t
    with localcontext() as context:
        context.prec = 300
        tick = Decimal(1).scaleb(-rules.quote_precision)
        slip = policy.max_slippage_bps / Decimal(10000)
        # El presupuesto cubre el peor coste autorizado, no sólo una tarifa
        # publicada que pueda variar por nivel de cuenta o redondeo de fees.
        fee_rate = max(rules.fee_rate_upper, policy.max_fee_bps / Decimal(10000))
        # Redondear HACIA DENTRO de la banda autorizada: hacerlo hacia fuera
        # violaría el límite de slippage por una fracción de tick.
        price = _floor(state.best_ask * (1 + slip), tick)
        high = cursor.trailing_high
        dust = position.units > 0 and position.units * state.best_bid < rules.min_quote
        if position.units > 0 and not dust:
            average = position.cost_quote / position.units
            if high is not None and (not high.is_finite() or high < average):
                return result("PAUSE", "TRAILING_STATE_INVALID")
            stop = average * (1 - Decimal(definition["stop_loss"]))
            reason = "STOP_LOSS" if state.best_bid <= stop else None
            if (reason is None and "trailing_stop" in definition and high is not None
                    and state.best_bid <= high * (1 - Decimal(definition["trailing_stop"]))):
                reason = "TRAILING_STOP"
            if reason is None and state.best_bid >= average * (1 + Decimal(definition["take_profit"])):
                reason = "TAKE_PROFIT"
            if reason is None and new_candle and observation.exit_match:
                reason = "EXIT_RULE"
            if (reason is None and new_candle and "max_holding_bars" in definition
                    and observation.candle_t - position.entry_candle_t >= definition["max_holding_bars"] * step):
                reason = "TIME_LIMIT"
            next_high = max(average, state.best_bid, high or average)
            if reason is None:
                return result("WAIT", "POSITION_OPEN", high=next_high)
            if not rules.sell_allowed:
                return result("PAUSE", "SELL_DISABLED", high=next_high)
            price = (state.best_bid * (1 - slip) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            if price <= 0:
                return result("PAUSE", "SELL_PRICE_UNUSABLE", high=next_high)
            base_step = _base_step(rules)
            # El presupuesto máximo por orden también se aplica a salidas.
            cap = min(position.units / (1 + fee_rate), policy.max_order_quote / price)
            quantity = _floor(cap, base_step)
            high = next_high
            action = "SELL"
        else:
            if not new_candle or not observation.entry_match:
                return result("WAIT", "WAITING_ENTRY", high=high)
            signal_at = datetime.fromtimestamp(observation.candle_t + step, tz=now.tzinfo)
            if (signal_at > now or (now - signal_at).total_seconds() > policy.max_data_age_seconds
                    or observation.observed_at < signal_at or observation.observed_at > now):
                return result("PAUSE", "ENTRY_SIGNAL_EXPIRED")
            if not rules.buy_allowed:
                return result("PAUSE", "BUY_DISABLED")
            if price <= 0:
                return result("PAUSE", "BUY_PRICE_UNUSABLE")
            budget = min(entry_budget_quote, state.free_quote)
            base_step = _base_step(rules)
            quantity = _floor(budget / (price * (1 + fee_rate)), base_step)
            reason, action, high = "ENTRY_RULE", "BUY", None
        if price <= 0 or quantity <= 0 or quantity * price < rules.min_quote:
            return result("PAUSE" if action == "SELL" else "WAIT", "BELOW_MINIMUM_ORDER", high=high)
        if quantity * price > rules.max_quote:
            return result("PAUSE", "EXCHANGE_ORDER_LIMIT", high=high)
        return SpotBotPlan(action, reason, observation.candle_t, quantity, price,
                           quantity * price * fee_rate, high)
