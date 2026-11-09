"""Internal experimental derivative book. No exchange authority or wire format.

All settlement amounts use 18 decimal places. Prices and sizes remain exact
Decimal inputs. The existing PAPER book and lab-1.16 contracts are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context, Decimal, DivisionByZero, InvalidOperation, Overflow,
    ROUND_CEILING, ROUND_HALF_EVEN, localcontext,
)
from functools import wraps
import re
from typing import Callable, Literal, ParamSpec, TypeVar

from app.derivatives.paper_math import InstrumentSpec, MarginRules, PaperPerpError


D = Decimal
ZERO = D('0')
QUANTUM = D('0.000000000000000001')
INTERNAL_BOOK_VERSION = 'perp-account-0.1'
P = ParamSpec('P')
R = TypeVar('R')
MarginMode = Literal['CROSS', 'ISOLATED']
OrderStatus = Literal['OPEN', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED']


class PerpAccountError(ValueError):
    pass


def exact(function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        # Construct a fresh context; do not inherit caller precision or traps.
        with localcontext(Context(prec=120, rounding=ROUND_HALF_EVEN, Emin=-999, Emax=999,
                capitals=1, clamp=0, flags=[], traps=[InvalidOperation, DivisionByZero, Overflow])):
            return function(*args, **kwargs)
    return wrapped


def value(number: Decimal, *, positive: bool = False) -> Decimal:
    if (type(number) is not Decimal or not number.is_finite()
            or len(number.as_tuple().digits) > 40
            or not -40 <= number.as_tuple().exponent <= 40
            or abs(number) > D('1e18') or (positive and number <= 0)):
        raise PerpAccountError('Se requiere Decimal finito dentro de rango')
    return number


def money(number: Decimal, *, up: bool = False) -> Decimal:
    return number.quantize(QUANTUM, rounding=ROUND_CEILING if up else ROUND_HALF_EVEN)


@dataclass(frozen=True, order=True)
class Cursor:
    time_ns: int
    ordinal: int

    def __post_init__(self) -> None:
        if (type(self.time_ns) is not int or not 0 < self.time_ns < 10**20
                or type(self.ordinal) is not int or not 0 <= self.ordinal <= 10**9):
            raise PerpAccountError('Tiempo/secuencia explícitos requeridos')


@dataclass(frozen=True)
class Market:
    instrument: str
    mode: MarginMode
    leverage: Decimal
    fee_rate: Decimal
    specification: InstrumentSpec
    rules: MarginRules
    rule_assumption: Literal['CURRENT_RULES_APPLIED_EXPERIMENTALLY'] = 'CURRENT_RULES_APPLIED_EXPERIMENTALLY'

    @exact
    def __post_init__(self) -> None:
        if (type(self.instrument) is not str or re.fullmatch(r'[A-Z0-9]{1,20}_USDT_Perp', self.instrument) is None
                or self.mode not in ('CROSS', 'ISOLATED')
                or self.rule_assumption != 'CURRENT_RULES_APPLIED_EXPERIMENTALLY'):
            raise PerpAccountError('Perfil perpetuo experimental no válido')
        if not 1 <= value(self.leverage, positive=True) <= 10000 or not 0 <= value(self.fee_rate) <= 1:
            raise PerpAccountError('Apalancamiento/comisión fuera de rango')
        spec = self.specification
        if not isinstance(spec, InstrumentSpec) or not isinstance(self.rules, MarginRules):
            raise PerpAccountError('Se requieren reglas/especificación fijadas')
        if spec.instrument != self.instrument or self.rules.instrument != self.instrument:
            raise PerpAccountError('Reglas de otro instrumento')
        if type(self.rules.fetched_at_ns) is not int or self.rules.fetched_at_ns <= 0:
            raise PerpAccountError('Reglas sin fecha exacta')
        for number in (spec.min_size, spec.size_step, spec.min_notional, spec.tick_size):
            value(number, positive=True)
        if (type(spec.fetched_at_ns) is not int or spec.fetched_at_ns <= 0
                or spec.min_size % spec.size_step or self.rules.max_size < spec.min_size
                or (spec.max_size is not None and value(spec.max_size, positive=True) < spec.min_size)):
            raise PerpAccountError('Especificación incompatible')
        try:
            checked = MarginRules.from_payload(self.rules.json(), self.instrument, self.rules.fetched_at_ns)
        except (PaperPerpError, ValueError, TypeError, ArithmeticError) as exc:
            raise PerpAccountError('Tramos de margen incompatibles') from exc
        if checked != self.rules:
            raise PerpAccountError('Tramos de margen no canónicos')


@dataclass(frozen=True)
class Position:
    instrument: str
    size: Decimal                 # Signed base units, positive LONG.
    basis: Decimal                # Positive remaining entry cost, not cash paid.
    collateral: Decimal = ZERO     # Zero for CROSS; separate pocket otherwise.


@dataclass(frozen=True)
class Order:
    id: str
    instrument: str
    side: Literal['BUY', 'SELL']
    quantity: Decimal
    remaining: Decimal
    price_bound: Decimal          # Max buy / min sell, including MARKET guards.
    kind: Literal['MARKET', 'LIMIT']
    reduce_only: bool
    status: OrderStatus = 'OPEN'
    reason: str | None = None

    @property
    def active(self) -> bool:
        return self.status in ('OPEN', 'PARTIAL')


@dataclass(frozen=True)
class Account:
    initial_cash: Decimal
    cash: Decimal
    markets: tuple[Market, ...]
    positions: tuple[Position, ...] = ()
    orders: tuple[Order, ...] = ()
    marks: tuple[tuple[str, Decimal], ...] = ()
    mark_time_ns: int | None = None
    funding_through: tuple[tuple[str, int], ...] = ()
    cursor: Cursor | None = None
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    funding_paid: Decimal = ZERO   # Signed, negative means a credit.
    insurance_transferred: Decimal = ZERO
    insurance_covered: Decimal = ZERO

    def market(self, instrument: str) -> Market:
        for market in self.markets:
            if market.instrument == instrument:
                return market
        raise PerpAccountError('Instrumento ajeno al perfil')

    def position(self, instrument: str) -> Position | None:
        return next((p for p in self.positions if p.instrument == instrument), None)

    def order(self, identifier: str) -> Order:
        for order in self.orders:
            if order.id == identifier:
                return order
        raise PerpAccountError('Orden no disponible')

    def mark(self, instrument: str) -> Decimal:
        for name, price in self.marks:
            if name == instrument:
                return price
        raise PerpAccountError('Falta la marca del instrumento')


@dataclass(frozen=True)
class Entry:
    cursor: Cursor
    kind: str
    instrument: str | None = None
    order_id: str | None = None
    reason: str | None = None
    size_delta: Decimal = ZERO
    price: Decimal | None = None
    cash_delta: Decimal = ZERO
    collateral_delta: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fee: Decimal = ZERO
    funding_payment: Decimal = ZERO
    insurance_transfer: Decimal = ZERO
    insurance_cover: Decimal = ZERO


@dataclass(frozen=True)
class Transition:
    account: Account
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Reservations:
    cross_initial: Decimal
    isolated_pending: Decimal
    fees: Decimal
    cross_equity: Decimal

    @property
    @exact
    def available(self) -> Decimal:
        return self.cross_equity - self.cross_initial - self.isolated_pending - self.fees


@dataclass(frozen=True)
class RiskPocket:
    instrument: str | None        # None for the pooled cross account.
    equity: Decimal
    maintenance: Decimal

    @property
    def breached(self) -> bool:
        return self.equity <= self.maintenance
