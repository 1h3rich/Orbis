"""Cálculos puros de Cartera Real, sin API ni almacenamiento.

El método de coste de fase 1 es el promedio ponderado. No constituye un
cálculo fiscal. Las monedas se mantienen separadas: no se suman EUR y USDT sin
un tipo de cambio identificable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Literal

from app.security.sensitive_text import contains_wallet_address


EventKind = Literal[
    "CONTRIBUTION", "WITHDRAWAL", "BUY", "SELL", "FEE",
    "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT",
    "TRANSFER", "TRANSFER_IN", "TRANSFER_OUT", "ADJUSTMENT_IN", "ADJUSTMENT_OUT",
]
KINDS = frozenset((
    "CONTRIBUTION", "WITHDRAWAL", "BUY", "SELL", "FEE",
    "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT",
    "TRANSFER", "TRANSFER_IN", "TRANSFER_OUT", "ADJUSTMENT_IN", "ADJUSTMENT_OUT",
))
# Dos entradas admitidas pueden distar más de 200 órdenes decimales
# (40 dígitos significativos y exponente ±100). 300 conserva hasta 1000
# sumandos sin ocultar una comisión o unidad positiva diminuta.
CALC_PRECISION = 300
MAX_INPUT_DIGITS = 40
MAX_INPUT_EXPONENT = 100


class PortfolioError(ValueError):
    """Un evento no puede formar parte de un libro real coherente."""


def decimal_value(value: Decimal, *, positive: bool = False) -> Decimal:
    """Acepta sólo Decimal finito; nunca interpreta un float financiero."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PortfolioError("Financial values must be finite Decimal values")
    parts = value.as_tuple()
    if abs(parts.exponent) > MAX_INPUT_EXPONENT:
        raise PortfolioError("Financial input exponent is outside supported range")
    if len(parts.digits) > MAX_INPUT_DIGITS + MAX_INPUT_EXPONENT:
        raise PortfolioError("Financial input exceeds supported precision")
    # La base persiste decimales en notación fija. 9e40 reaparece con 41
    # cifras, pero sólo una es significativa; los ceros finales no pueden
    # volver inválido un asiento que se admitió antes de guardarlo.
    trailing_zeros = 0
    if value:
        for digit in reversed(parts.digits):
            if digit != 0:
                break
            trailing_zeros += 1
    significant_digits = len(parts.digits) - trailing_zeros
    effective_exponent = parts.exponent + trailing_zeros
    if significant_digits > MAX_INPUT_DIGITS:
        raise PortfolioError("Financial input exceeds 40 significant digits")
    if abs(effective_exponent) > MAX_INPUT_EXPONENT:
        raise PortfolioError("Financial input exponent is outside supported range")
    if positive and value <= 0:
        raise PortfolioError("Financial value must be greater than zero")
    return value


def utc_datetime(value: datetime) -> datetime:
    """Normaliza instantes explícitos a UTC antes de ordenarlos."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PortfolioError("Event timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _symbol(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise PortfolioError(f"{label} is required")
    result = value.strip().upper()
    if not (2 <= len(result) <= 12 and result.isalnum() and result.isascii()):
        raise PortfolioError(f"Invalid {label}")
    return result


@dataclass(frozen=True)
class EventInput:
    """Propuesta manual validada; `id` y `recorded_at` los asigna el almacén."""

    kind: EventKind
    quote_currency: str
    occurred_at: datetime
    asset: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    fee: Decimal = Decimal("0")
    note: str | None = None
    # Contravalor exacto comunicado por el mercado cuando difiere de precio × cantidad.
    gross_override: Decimal | None = None
    # None significa comisión en quote para conservar eventos legados.
    fee_asset: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise PortfolioError("Unsupported event kind")
        object.__setattr__(self, "quote_currency", _symbol(self.quote_currency, "quote currency"))
        object.__setattr__(self, "occurred_at", utc_datetime(self.occurred_at))
        decimal_value(self.fee)
        if self.fee < 0:
            raise PortfolioError("Fee cannot be negative")
        if self.note is not None:
            clean_note = self.note.strip()
            if len(clean_note) > 1000:
                raise PortfolioError("Note is too long")
            if contains_wallet_address(clean_note):
                raise PortfolioError("Do not store complete wallet addresses in notes")
            object.__setattr__(self, "note", clean_note or None)
        if self.kind == "FEE" and not self.note:
            raise PortfolioError("Fee expense requires an explanatory note")

        if self.kind in ("BUY", "SELL", "TRANSFER", "TRANSFER_IN", "TRANSFER_OUT",
                         "ADJUSTMENT_IN", "ADJUSTMENT_OUT") or self.kind == "FEE" and self.asset is not None:
            object.__setattr__(self, "asset", _symbol(self.asset, "asset"))
            if self.asset == self.quote_currency:
                raise PortfolioError("Asset and quote currency must differ")
            decimal_value(self.quantity, positive=True)
            if self.amount is not None:
                raise PortfolioError("Asset movement amount is derived from quantity and price")
            if self.kind in ("BUY", "SELL"):
                decimal_value(self.unit_price, positive=True)
                if self.gross_override is not None:
                    decimal_value(self.gross_override, positive=True)
                if self.fee_asset is not None:
                    fee_asset = _symbol(self.fee_asset, "fee asset")
                    if fee_asset not in (self.asset, self.quote_currency):
                        raise PortfolioError("Trade fee asset must be base or quote")
                    object.__setattr__(self, "fee_asset", None if fee_asset == self.quote_currency else fee_asset)
                if self.fee_asset == self.asset and (self.fee <= 0 or self.kind == "BUY" and self.fee >= self.quantity):
                    raise PortfolioError("Base asset fee must be positive and leave acquired quantity")
            elif self.kind == "ADJUSTMENT_IN":
                decimal_value(self.unit_price)
                if self.unit_price < 0:
                    raise PortfolioError("Adjustment unit cost cannot be negative")
            elif self.unit_price is not None:
                raise PortfolioError("This movement cannot contain a unit price")
            if self.kind in ("TRANSFER_IN", "TRANSFER_OUT"):
                if (not isinstance(self.gross_override, Decimal)
                        or not self.gross_override.is_finite()
                        or self.gross_override < 0
                        or len(self.gross_override.as_tuple().digits) > CALC_PRECISION):
                    raise PortfolioError("Transferred cost basis is invalid")
            if self.kind == "TRANSFER_OUT" and self.fee:
                if self.fee >= self.quantity or _symbol(self.fee_asset, "fee asset") != self.asset:
                    raise PortfolioError("Transfer fee must be in the sent asset and below sent quantity")
            elif self.kind in ("TRANSFER", "TRANSFER_IN", "TRANSFER_OUT",
                               "ADJUSTMENT_IN", "ADJUSTMENT_OUT", "FEE") and self.fee != 0:
                raise PortfolioError("This movement cannot contain a fee")
            if self.kind not in ("BUY", "SELL", "TRANSFER_IN", "TRANSFER_OUT") and self.gross_override is not None:
                raise PortfolioError("Gross override is only valid for trades")
            if self.kind not in ("BUY", "SELL") and self.fee_asset is not None:
                if self.kind != "TRANSFER_OUT" or self.fee == 0:
                    raise PortfolioError("Fee asset is only valid for trades or outgoing transfers")
                object.__setattr__(self, "fee_asset", _symbol(self.fee_asset, "fee asset"))
            if self.kind in ("ADJUSTMENT_IN", "ADJUSTMENT_OUT") and not self.note:
                raise PortfolioError("Adjustment requires an explanatory note")
        else:
            decimal_value(self.amount, positive=True)
            if self.asset is not None or self.quantity is not None or self.unit_price is not None:
                raise PortfolioError("Cash movements cannot contain an asset trade")
            if self.fee != 0 and self.kind not in ("WITHDRAWAL","CASH_TRANSFER_OUT"):
                raise PortfolioError("Cash movement fee requires a separate event")
            if self.gross_override is not None:
                raise PortfolioError("Gross override is only valid for trades")
            if self.fee_asset is not None:
                raise PortfolioError("Fee asset is only valid for trades")

    @property
    def effective_fee_asset(self) -> str | None:
        if self.fee and self.kind in ("WITHDRAWAL","CASH_TRANSFER_OUT"):
            return self.quote_currency
        return (self.fee_asset or self.quote_currency) if self.kind in ("BUY", "SELL") else None

    @property
    def gross_amount(self) -> Decimal:
        if self.gross_override is not None:
            return self.gross_override
        if self.kind in ("BUY", "SELL", "ADJUSTMENT_IN"):
            with localcontext() as context:
                context.prec = CALC_PRECISION
                return self.quantity * self.unit_price
        return self.amount if self.amount is not None else Decimal("0")


@dataclass(frozen=True)
class AccountingContext:
    original: PortfolioEvent
    effective_input: EventInput
    ordinal: int
    acceptance_ids: tuple[str, ...]
    accepted_prefix: bool
    source_order_record_id: str | None
    transfer_basis_derived: bool

    def __post_init__(self):
        if (not isinstance(self.original, PortfolioEvent) or self.original.accounting is not None
                or not isinstance(self.effective_input, EventInput)
                or type(self.ordinal) is not int or self.ordinal < 0
                or type(self.accepted_prefix) is not bool or type(self.transfer_basis_derived) is not bool
                or type(self.acceptance_ids) is not tuple or not self.acceptance_ids):
            raise PortfolioError('Invalid economic event context')
        original = self.original.input
        if (any(getattr(original, item.name) != getattr(self.effective_input, item.name)
                for item in fields(EventInput) if item.name not in ('occurred_at', 'gross_override'))
                or original.kind not in ('TRANSFER_OUT', 'TRANSFER_IN')
                and original.gross_override != self.effective_input.gross_override):
            raise PortfolioError('Economic context cannot replace original financial facts')


@dataclass(frozen=True)
class PortfolioEvent:
    id: str
    account_id: str
    request_id: str
    recorded_at: datetime
    input: EventInput
    accounting: AccountingContext | None = None

    def __post_init__(self):
        if self.accounting is not None:
            if not isinstance(self.accounting, AccountingContext):
                raise PortfolioError('Invalid economic event context')
            original = self.accounting.original
            if (self.id, self.account_id, self.request_id, self.recorded_at, self.input) != (
                    original.id, original.account_id, original.request_id, original.recorded_at, original.input):
                raise PortfolioError('Economic context lost its original event')

    @property
    def financial_input(self) -> EventInput:
        return self.accounting.effective_input if self.accounting is not None else self.input

    @property
    def occurred_at(self) -> datetime:
        return self.financial_input.occurred_at

    @property
    def sort_key(self) -> tuple:
        if self.accounting is not None:
            return self.occurred_at, 0, self.accounting.ordinal, self.id
        return self.occurred_at, 1, self.recorded_at, self.id


@dataclass
class Position:
    asset: str
    quote_currency: str
    quantity: Decimal = Decimal("0")
    cost_basis: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    fees_by_asset: dict[str, Decimal] = field(default_factory=dict)

    @property
    def average_cost(self) -> Decimal:
        if self.quantity == 0:
            return Decimal("0")
        with localcontext() as context:
            context.prec = CALC_PRECISION
            return self.cost_basis / self.quantity


@dataclass
class AccountSnapshot:
    account_id: str
    cash: dict[str, Decimal] = field(default_factory=dict)
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    contributions: dict[str, Decimal] = field(default_factory=dict)
    withdrawals: dict[str, Decimal] = field(default_factory=dict)
    cash_fees: dict[str, Decimal] = field(default_factory=dict)


def realized_pnl_by_currency(snapshot: AccountSnapshot) -> dict[str, Decimal]:
    """Resultado por ventas y gastos, por divisa; incluye posiciones cerradas."""
    totals = {currency: Decimal("0") for currency in snapshot.cash}
    with localcontext() as context:
        context.prec = CALC_PRECISION
        for currency,fee in snapshot.cash_fees.items():
            totals[currency] = totals.get(currency,Decimal(0))-fee
        for position in snapshot.positions.values():
            currency = position.quote_currency
            totals[currency] = totals.get(currency, Decimal("0")) + position.realized_pnl
    return totals


def apply_portfolio_event(snapshot: AccountSnapshot, event: PortfolioEvent) -> None:
    """Apply one event without choosing its chronological position.

    Mutates only the supplied snapshot. Shared by the legacy fold and the
    separately versioned reconciliation engine; never performs storage I/O.
    """
    with localcontext() as context:
        context.prec = CALC_PRECISION
        if event.account_id != snapshot.account_id:
            raise PortfolioError("Events from different accounts cannot be mixed")
        item = event.financial_input
        quote = item.quote_currency
        snapshot.cash.setdefault(quote, Decimal("0"))
        if item.kind == "CONTRIBUTION":
            snapshot.cash[quote] += item.amount
            snapshot.contributions[quote] = snapshot.contributions.get(quote, Decimal("0")) + item.amount
            return
        if item.kind == "WITHDRAWAL":
            snapshot.cash[quote] -= item.amount+item.fee
            if item.fee:
                snapshot.cash_fees[quote] = snapshot.cash_fees.get(quote,Decimal(0))+item.fee
            snapshot.withdrawals[quote] = snapshot.withdrawals.get(quote, Decimal("0")) + item.amount
            return
        if item.kind == "CASH_TRANSFER_OUT":
            if item.amount+item.fee > snapshot.cash[quote]:
                raise PortfolioError("Cash transfer exceeds available balance at that time")
            snapshot.cash[quote] -= item.amount+item.fee
            if item.fee:
                snapshot.cash_fees[quote] = snapshot.cash_fees.get(quote,Decimal(0))+item.fee
            return
        if item.kind == "CASH_TRANSFER_IN":
            snapshot.cash[quote] += item.amount
            return
        if item.kind == "FEE" and item.asset is None:
            if item.amount > snapshot.cash[quote]:
                raise PortfolioError("Fee expense exceeds available cash at that time")
            snapshot.cash[quote] -= item.amount
            snapshot.cash_fees[quote] = snapshot.cash_fees.get(quote, Decimal(0)) + item.amount
            return

        key = (item.asset, quote)
        position = snapshot.positions.setdefault(key, Position(asset=item.asset, quote_currency=quote))
        gross = item.gross_amount
        if item.kind == "BUY":
            base_fee = item.fee if item.fee_asset == item.asset else Decimal(0)
            quote_fee = item.fee if base_fee == 0 else Decimal(0)
            position.quantity += item.quantity - base_fee
            position.cost_basis += gross + quote_fee
            position.fees += quote_fee
            if item.fee:
                position.fees_by_asset[item.effective_fee_asset] = (
                    position.fees_by_asset.get(item.effective_fee_asset, Decimal(0)) + item.fee)
            snapshot.cash[quote] -= gross + quote_fee
            return

        if item.kind == "ADJUSTMENT_IN":
            position.quantity += item.quantity
            position.cost_basis += gross
            return
        if item.kind == "TRANSFER_IN":
            position.quantity += item.quantity
            position.cost_basis += gross
            return

        consumed = item.quantity + (item.fee if item.kind == "SELL" and item.fee_asset == item.asset
                                    else Decimal(0))
        if consumed > position.quantity:
            raise PortfolioError("Movement quantity exceeds available real position")
        if item.kind == "TRANSFER":
            # Movimiento entre ubicaciones propias de la misma cuenta:
            # sólo se audita; no altera inventario, coste, caja ni P/L.
            return
        average_before = position.average_cost
        allocated_cost = (
            position.cost_basis
            if consumed == position.quantity
            else average_before * consumed
        )
        if item.kind == "TRANSFER_OUT" and gross != allocated_cost:
            raise PortfolioError("Transferred cost basis differs from source inventory")
        position.quantity -= consumed
        position.cost_basis -= allocated_cost
        if item.kind == "FEE":
            # Coste de las unidades consumidas, no una venta a precio de
            # mercado ni una conversión fiscal/FX inferida.
            position.realized_pnl -= allocated_cost
            position.fees += allocated_cost
            position.fees_by_asset[item.asset] = position.fees_by_asset.get(item.asset, Decimal(0)) + consumed
        if item.kind == "TRANSFER_OUT" and item.fee:
            position.fees_by_asset[item.asset] = (
                position.fees_by_asset.get(item.asset, Decimal(0)) + item.fee)
        if item.kind == "SELL":
            quote_fee = item.fee if item.fee_asset != item.asset else Decimal(0)
            position.realized_pnl += gross - quote_fee - allocated_cost
            position.fees += quote_fee
            if item.fee:
                position.fees_by_asset[item.effective_fee_asset] = (
                    position.fees_by_asset.get(item.effective_fee_asset, Decimal(0)) + item.fee)
            snapshot.cash[quote] += gross - quote_fee


def fold_events(account_id: str, events: list[PortfolioEvent]) -> AccountSnapshot:
    """Reconstruye efectivo y posiciones; rechaza ventas sin inventario.

    Una entrada retroactiva se ordena por fecha económica y puede invalidar
    una venta posterior. El repositorio valida todo el historial antes del
    commit para evitar dejar esa inconsistencia guardada.
    """
    snapshot = AccountSnapshot(account_id=account_id)
    ordered = sorted(events, key=lambda event: event.sort_key)
    with localcontext() as context:
        context.prec = CALC_PRECISION
        if any(event.accounting is not None for event in ordered):
            context.rounding = ROUND_HALF_EVEN
        for event in ordered:
            apply_portfolio_event(snapshot, event)

    return snapshot
