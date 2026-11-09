"""Guardia determinista para futuras intenciones MEXC Spot reales.

Este módulo no abre conexiones, no persiste políticas y no envía órdenes.
Toda autorización es provisional: el ejecutor deberá reservar fondos de forma
atómica, consultar reglas del instrumento y reconciliar antes de transmitir.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re
from typing import Literal
from uuid import UUID


_SYMBOL = re.compile(r"^[A-Z0-9]{3,30}$")
_ASSET = re.compile(r"^[A-Z0-9]{2,16}$")
_REF = re.compile(r"^[0-9a-f]{64}$")
# Reserved origin for limits that authorize manual orders only. LAB and PAPER
# strategy versions use UUIDs. Keeping the origin in the existing canonical
# scope preserves the policy and intent schemas and every prior digest.
MEXC_MANUAL_ORIGIN = "orbis:mexc:manual:v1"


def _money(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if (type(value) is not Decimal or not value.is_finite()
            or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100
            or value < 0 or positive and value == 0):
        raise ValueError(f"{name} must be a finite Decimal")
    return value


def _derived_money(value: Decimal, name: str, *, signed: bool = False) -> Decimal:
    """El libro calcula a 300 dígitos; sus cocientes no son entradas de usuario.

    Un coste medio como 100/3 conserva esa precisión sin convertirse en un
    importe externo admisible. Política, orden, precio y saldo crudos siguen
    sujetos al límite de 40 dígitos de `_money`.
    """
    if (type(value) is not Decimal or not value.is_finite()
            or len(value.as_tuple().digits) > 300 or abs(value.as_tuple().exponent) > 600
            or not signed and value < 0):
        raise ValueError(f"{name} must be a bounded derived Decimal")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must have a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class LiveSpotPolicy:
    policy_id: str
    strategy_version_id: str
    account_ref: str
    symbol: str
    base_asset: str
    quote_asset: str
    max_order_quote: Decimal
    max_daily_turnover_quote: Decimal
    max_exposure_quote: Decimal
    max_daily_loss_quote: Decimal
    max_fee_bps: Decimal
    max_slippage_bps: Decimal
    max_data_age_seconds: int
    expires_at: datetime
    enabled: bool = False
    reconciliation_anchor_id: str | None = None
    allow_risk_reducing_exits: bool = False
    resumed_after_stop_at: datetime | None = None

    @property
    def manual_only(self) -> bool:
        return self.strategy_version_id == MEXC_MANUAL_ORIGIN

    def __post_init__(self) -> None:
        try:
            UUID(self.policy_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("policy_id must be a UUID") from exc
        if not isinstance(self.strategy_version_id, str) or not self.strategy_version_id.strip():
            raise ValueError("strategy version is required")
        if not isinstance(self.account_ref, str) or not _REF.fullmatch(self.account_ref):
            raise ValueError("account reference is invalid")
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("Spot symbol is invalid")
        if (not isinstance(self.base_asset, str) or not _ASSET.fullmatch(self.base_asset)
                or not isinstance(self.quote_asset, str) or not _ASSET.fullmatch(self.quote_asset)
                or self.base_asset == self.quote_asset
                or self.base_asset + self.quote_asset != self.symbol):
            raise ValueError("Spot assets do not match symbol")
        for name in ("max_order_quote", "max_daily_turnover_quote", "max_exposure_quote",
                     "max_daily_loss_quote"):
            _money(getattr(self, name), name, positive=True)
        for name in ("max_fee_bps", "max_slippage_bps"):
            value = _money(getattr(self, name), name)
            if value > Decimal("10000"):
                raise ValueError(f"{name} exceeds 100%")
        if type(self.max_data_age_seconds) is not int or not 1 <= self.max_data_age_seconds <= 300:
            raise ValueError("max_data_age_seconds is invalid")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if type(self.allow_risk_reducing_exits) is not bool:
            raise ValueError("risk reducing exits must be boolean")
        if self.resumed_after_stop_at is not None:
            object.__setattr__(self, "resumed_after_stop_at", _utc(self.resumed_after_stop_at, "resumed_after_stop_at"))
        if self.reconciliation_anchor_id is not None:
            try:
                UUID(self.reconciliation_anchor_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("reconciliation anchor must be a UUID") from exc
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))


@dataclass(frozen=True, slots=True)
class LiveSpotIntent:
    intent_id: str
    policy_id: str
    strategy_version_id: str
    account_ref: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity_base: Decimal
    limit_price_quote: Decimal
    estimated_fee_quote: Decimal
    signal_at: datetime
    max_spend_quote: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("intent_id", "policy_id"):
            try:
                UUID(getattr(self, name))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a UUID") from exc
        if not isinstance(self.strategy_version_id, str) or not self.strategy_version_id.strip():
            raise ValueError("strategy version is required")
        if not isinstance(self.account_ref, str) or not _REF.fullmatch(self.account_ref):
            raise ValueError("account reference is invalid")
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("Spot symbol is invalid")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        _money(self.quantity_base, "quantity_base", positive=True)
        _money(self.limit_price_quote, "limit_price_quote", positive=True)
        _money(self.estimated_fee_quote, "estimated_fee_quote")
        if self.max_spend_quote is not None:
            if self.side != "BUY":
                raise ValueError("A spending cap only applies to BUY")
            _money(self.max_spend_quote, "max_spend_quote", positive=True)
        object.__setattr__(self, "signal_at", _utc(self.signal_at, "signal_at"))


@dataclass(frozen=True, slots=True)
class LiveSpotState:
    account_ref: str
    symbol: str
    reconciled: bool
    stopped: bool
    account_verified: bool
    instrument_verified: bool
    observed_at: datetime
    quote_at: datetime
    best_bid: Decimal
    best_ask: Decimal
    free_base: Decimal
    free_quote: Decimal
    pending_base: Decimal
    pending_quote: Decimal
    daily_turnover_quote: Decimal
    pending_turnover_quote: Decimal
    exposure_quote: Decimal
    daily_realized_pnl_quote: Decimal
    # Coste medio de la posición reconciliada, en quote por unidad base.
    # Sin este dato no se puede estimar la pérdida de una venta.
    average_cost_quote_per_base: Decimal | None = None
    pending_projected_loss_quote: Decimal = Decimal(0)
    unrealized_loss_quote: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not isinstance(self.account_ref, str) or not _REF.fullmatch(self.account_ref):
            raise ValueError("account reference is invalid")
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("Spot symbol is invalid")
        if any(type(getattr(self, name)) is not bool for name in (
                "reconciled", "stopped", "account_verified", "instrument_verified")):
            raise ValueError("live safety flags must be boolean")
        for name in ("observed_at", "quote_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        for name in ("best_bid", "best_ask"):
            _money(getattr(self, name), name, positive=True)
        if self.best_bid > self.best_ask:
            raise ValueError("bid exceeds ask")
        for name in ("free_base", "free_quote"):
            _money(getattr(self, name), name)
        for name in ("pending_base", "pending_quote", "daily_turnover_quote",
                     "pending_turnover_quote", "exposure_quote"):
            _derived_money(getattr(self, name), name)
        _derived_money(self.daily_realized_pnl_quote, "daily_realized_pnl_quote", signed=True)
        if self.average_cost_quote_per_base is not None:
            _derived_money(self.average_cost_quote_per_base, "average_cost_quote_per_base")
        _derived_money(self.pending_projected_loss_quote, "pending_projected_loss_quote")
        _derived_money(self.unrealized_loss_quote, "unrealized_loss_quote")


@dataclass(frozen=True, slots=True)
class LiveSpotDecision:
    allowed: bool
    reason: str
    notional_quote: Decimal
    reserved_quote: Decimal


def sell_base_reservation(intent: LiveSpotIntent) -> Decimal:
    """Reserva unidades para una posible comisión cobrada en el activo base.

    El preflight del instrumento exige que estimated_fee_quote cubra la tasa
    máxima publicada; dividir por el precio límite da el equivalente base.
    """
    if intent.side != "SELL":
        return Decimal(0)
    with localcontext() as context:
        context.prec = 300
        return intent.quantity_base + intent.estimated_fee_quote / intent.limit_price_quote


def evaluate_spot_intent(policy: LiveSpotPolicy, intent: LiveSpotIntent,
                         state: LiveSpotState, *, now: datetime) -> LiveSpotDecision:
    """Autoriza sólo con identidad, datos, saldo y límites verificables.

    Es una decisión pura, no una reserva. La transmisión requiere después un
    registro idempotente y una transición atómica a estado autorizado.
    """
    now = _utc(now, "now")
    with localcontext() as context:
        context.prec = 300
        notional = intent.quantity_base * intent.limit_price_quote
        spend = notional + intent.estimated_fee_quote if intent.side == "BUY" else Decimal(0)

        def deny(code: str) -> LiveSpotDecision:
            return LiveSpotDecision(False, code, notional, spend)

        if not policy.enabled:
            return deny("POLICY_DISABLED")
        if state.stopped:
            return deny("STOPPED")
        if (intent.policy_id != policy.policy_id or intent.strategy_version_id != policy.strategy_version_id
                or intent.account_ref != policy.account_ref or state.account_ref != policy.account_ref
                or intent.symbol != policy.symbol or state.symbol != policy.symbol):
            return deny("SCOPE_MISMATCH")
        if not state.account_verified:
            return deny("UNVERIFIED_ACCOUNT")
        if not state.instrument_verified:
            return deny("UNVERIFIED_INSTRUMENT")
        if now >= policy.expires_at or intent.signal_at > now or intent.signal_at >= policy.expires_at:
            return deny("POLICY_OR_SIGNAL_TIME_INVALID")
        if not state.reconciled:
            return deny("NOT_RECONCILED")
        age_limit = policy.max_data_age_seconds
        if (state.observed_at > now or state.quote_at > now
                or (now - state.observed_at).total_seconds() > age_limit
                or (now - state.quote_at).total_seconds() > age_limit
                or (now - intent.signal_at).total_seconds() > age_limit):
            return deny("STALE_DATA_OR_SIGNAL")
        if notional > policy.max_order_quote:
            return deny("ORDER_LIMIT")
        if intent.max_spend_quote is not None and spend > intent.max_spend_quote:
            return deny("BUDGET_LIMIT")
        if intent.estimated_fee_quote * Decimal(10000) > notional * policy.max_fee_bps:
            return deny("FEE_LIMIT")
        reducing_exit = policy.allow_risk_reducing_exits and intent.side == "SELL"
        if (not reducing_exit and state.daily_turnover_quote + state.pending_turnover_quote + notional
                > policy.max_daily_turnover_quote):
            return deny("DAILY_TURNOVER_LIMIT")
        if (not reducing_exit and state.daily_realized_pnl_quote - state.unrealized_loss_quote
                - state.pending_projected_loss_quote <= -policy.max_daily_loss_quote):
            return deny("DAILY_LOSS_LIMIT")
        slippage = policy.max_slippage_bps / Decimal(10000)
        if intent.side == "BUY":
            if intent.limit_price_quote > state.best_ask * (Decimal(1) + slippage):
                return deny("PRICE_BOUND")
            if state.exposure_quote + state.pending_quote + notional > policy.max_exposure_quote:
                return deny("EXPOSURE_LIMIT")
            if spend > state.free_quote - state.pending_quote:
                return deny("INSUFFICIENT_QUOTE")
        else:
            if intent.limit_price_quote < state.best_bid * (Decimal(1) - slippage):
                return deny("PRICE_BOUND")
            if sell_base_reservation(intent) > state.free_base - state.pending_base:
                return deny("INSUFFICIENT_BASE")
            if state.average_cost_quote_per_base is None:
                return deny("SELL_COST_BASIS_UNKNOWN")
            # El límite SELL es el peor precio ejecutable de la orden. Reservar
            # una pérdida que agotaría el presupuesto evita depender de un
            # P/L que sólo se actualizaría después del fill.
            # MEXC puede cargar la comisión en quote o en base. La segunda
            # consume coste medio adicional y puede ser la peor pérdida.
            effective_fee_quote = max(
                intent.estimated_fee_quote,
                intent.estimated_fee_quote * state.average_cost_quote_per_base
                / intent.limit_price_quote,
            )
            projected_pnl = (
                state.daily_realized_pnl_quote - state.pending_projected_loss_quote + notional
                - intent.quantity_base * state.average_cost_quote_per_base
                - effective_fee_quote
            )
            if not reducing_exit and projected_pnl <= -policy.max_daily_loss_quote:
                return deny("PROJECTED_DAILY_LOSS_LIMIT")
        return LiveSpotDecision(True, "AUTHORIZED_FOR_RESERVATION", notional, spend)
