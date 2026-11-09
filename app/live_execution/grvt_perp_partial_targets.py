"""GRVT reduction decisions from terminal fills and a reconciled private position."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Literal, Self

from pydantic import Field, model_validator

from app.derivatives.perp_observed_wire import ExactInput
from app.lab.engine import number as strategy_number, validate_rule_timeframes, validate_strategy
from app.lab.perp_book_types import exact, value
from app.lab.perp_dataset import SYMBOLS
from app.lab.perp_profile import validate_partial_targets
from app.lab.store import content_hash
from app.live_execution.grvt_mainnet_bracket import MainnetInstrument, client_ids, exact_uint
from app.live_execution.grvt_mainnet_preflight import number
from app.live_execution.grvt_mainnet_reduction_order import BoundedReductionScope, bounded_reduction, unsigned_reduction
from app.live_execution.grvt_perp_plan import FRESH_NS, PerpRealBudget
from app.research.perp_live_review import ObservedReviewPrefix
from app.research.perp_reference import PerpPartialResearchReference


ZERO = Decimal(0)
ExitReason = Literal['EXIT_RULE', 'TIME_LIMIT', 'STOP_OR_TRAILING', 'TAKE_PROFIT',
    'DATA_GAP', 'DATA_STALE', 'MAX_EXPOSURE_TIME', 'ENTRY_LIMIT_BREACH', 'EXIT_LIMIT_BREACH', 'MANUAL_CLOSE']
EXIT_REASONS = {'EXIT_RULE', 'TIME_LIMIT', 'STOP_OR_TRAILING', 'TAKE_PROFIT', 'DATA_GAP',
    'DATA_STALE', 'MAX_EXPOSURE_TIME', 'ENTRY_LIMIT_BREACH', 'EXIT_LIMIT_BREACH', 'MANUAL_CLOSE'}


@dataclass(frozen=True)
class ReconciledReduction:
    client_order_id: str
    target_index: int | None
    requested: Decimal
    # None keeps the claim pending, including a send with a lost response.
    terminal_filled: Decimal | None


@dataclass(frozen=True)
class PartialExitObservation:
    entry_size: Decimal
    entry_notional: Decimal
    confirmed_position: Decimal | None
    reconciled_at_ns: int | None
    attempts: tuple[ReconciledReduction,...]
    close_reason: ExitReason | None
    quote: dict | None
    at_ns: int


@dataclass(frozen=True)
class PartialExitDecision:
    action: Literal['HOLD','PARTIAL','CLOSE','WAIT_RECEIPT','FLAT','REVIEW']
    reason: str
    remaining: Decimal
    target_index: int | None = None
    quantity: Decimal | None = None
    price_bound: Decimal | None = None
    pending_id: str | None = None


class PerpPartialExitTerms(ExactInput):
    schema_name: Literal['orbis.grvt.perp.partial.exit.terms.v1'] = Field(alias='schema')
    research: PerpPartialResearchReference
    paper: ObservedReviewPrefix
    paper_partial_fills: int = Field(ge=1)
    definition: dict
    budget: PerpRealBudget
    max_attempts: int = Field(ge=2, le=20)
    reserved_close_attempts: int = Field(ge=1, le=19)
    execution_authority: Literal['NONE']

    @property
    def instrument(self) -> str:
        return self.research.symbol.replace('/', '_').replace(':PERP', '_Perp')

    @model_validator(mode='after')
    @exact
    def coherent(self) -> Self:
        definition = validate_strategy(self.definition)
        profile = self.research.profile
        validate_rule_timeframes(definition, self.research.timeframe)
        validate_partial_targets(profile, definition)
        if (self.research.symbol not in SYMBOLS or definition != self.definition
                or content_hash(definition) != profile.strategy_hash
                or strategy_number(definition['fee_rate']) > number(self.budget.fee_rate_ceiling)
                or strategy_number(definition['slippage_rate'])*10000 > self.budget.max_slippage_bps
                or definition['direction'] == 'SHORT' and strategy_number(definition['take_profit']) >= 1
                or self.max_attempts < len(profile.partial_targets)+self.reserved_close_attempts):
            raise ValueError('Perfil, costes o reserva de intentos parciales REAL incompatibles')
        if (self.paper.stored_status != 'STOPPED' or self.paper.closed_trades < 1
                or self.paper.observed_bars < 1 or self.paper.has_estimated_closure
                or self.paper.source_advanced
                or self.paper_partial_fills > self.paper.closed_trades*len(profile.partial_targets)):
            raise ValueError('Se requiere una prueba PAPER cerrada con parciales observados')
        return self


def _lot_floor(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _size(quantity: Decimal, step: Decimal) -> Decimal:
    value(quantity)
    if quantity < 0 or quantity % step:
        raise ValueError('Cantidad de salida parcial fuera del lote GRVT')
    return quantity


def _active_target(unfilled: list[Decimal], minimum: Decimal) -> int | None:
    return next((index for index, amount in enumerate(unfilled) if amount >= minimum), None)


def _ledger(entry_size: Decimal, allocations: list[Decimal], instrument: MainnetInstrument,
            step: Decimal, attempts: tuple[ReconciledReduction,...],
            partial_attempt_limit: int) -> tuple[list[Decimal],Decimal,bool,str|None]:
    remaining = entry_size
    unfilled = allocations.copy()
    seen: set[str] = set()
    closing = False
    pending = None
    for ordinal, attempt in enumerate(attempts):
        if not isinstance(attempt, ReconciledReduction):
            raise ValueError('Intento de reducción sin evidencia conciliada')
        cid = client_ids(attempt.client_order_id)[0]
        requested = _size(attempt.requested, step)
        if pending is not None or cid in seen or not instrument.min_size <= requested <= remaining:
            raise ValueError('Intento de reducción repetido, sin conciliación previa o fuera de posición')
        exact_uint(requested, instrument.base_decimals, 2**64-1)
        seen.add(cid)
        index = attempt.target_index
        if index is None:
            closing = True
        elif (closing or type(index) is not int or not 1 <= index <= len(unfilled)
                or ordinal >= partial_attempt_limit
                or index-1 != _active_target(unfilled, instrument.min_size)
                or requested > unfilled[index-1] or remaining-requested < instrument.min_size):
            raise ValueError('Intento de reducción fuera del objetivo activo o sin residuo protegible')
        if attempt.terminal_filled is None:
            pending = cid
            continue
        filled = _size(attempt.terminal_filled, step)
        if filled > requested:
            raise ValueError('La cantidad ejecutada supera la orden de reducción')
        remaining -= filled
        if index is not None:
            unfilled[index-1] -= filled
    return unfilled, remaining, closing, pending


def _scope(terms: PerpPartialExitTerms, remaining: Decimal) -> BoundedReductionScope:
    budget = terms.budget
    return BoundedReductionScope(budget.sub_account_id, terms.instrument, terms.definition['direction'], remaining,
        number(budget.exit_notional_limit), budget.max_spread_bps, budget.max_slippage_bps)


@exact
def decide_partial_exit(terms: PerpPartialExitTerms, instrument: MainnetInstrument,
                        source: PartialExitObservation) -> PartialExitDecision:
    """Pure evidence for the next signed order, using the original executed size.

    A caller must replay the authenticated order/fill/position evidence and
    separately enforce the permission epoch and immutable target binding.
    """
    terms = PerpPartialExitTerms.model_validate(terms.payload())
    profile, definition = terms.research.profile, terms.definition
    entry_size, entry_notional = source.entry_size, source.entry_notional
    attempts, close_reason, at_ns = source.attempts, source.close_reason, source.at_ns
    quote = source.quote.copy() if source.quote is not None else None
    confirmed_position, reconciled_at_ns = source.confirmed_position, source.reconciled_at_ns
    if (terms.instrument != instrument.name or type(at_ns) is not int or not 0 < at_ns < 2**63
            or not isinstance(attempts, tuple) or len(attempts) > terms.max_attempts
            or close_reason is not None and (type(close_reason) is not str or close_reason not in EXIT_REASONS)):
        raise ValueError('Ámbito de salidas parciales GRVT no válido')
    value(entry_notional, positive=True)
    step = Decimal(1).scaleb(-instrument.base_decimals)
    _size(entry_size, step)
    exact_uint(entry_size, instrument.base_decimals, 2**64-1)
    long = definition['direction'] == 'LONG'
    sign = 1 if long else -1
    allocations = [_lot_floor(entry_size * strategy_number(rule.fraction), step) for rule in profile.partial_targets]
    partial_attempt_limit = terms.max_attempts-terms.reserved_close_attempts
    unfilled, remaining, closing, pending = _ledger(entry_size, allocations, instrument, step, attempts, partial_attempt_limit)
    if pending is not None:
        return PartialExitDecision('WAIT_RECEIPT','ORDER_UNRECONCILED',remaining,pending_id=pending)
    if confirmed_position is None or value(confirmed_position) != sign * remaining:
        return PartialExitDecision('REVIEW','POSITION_MISMATCH',remaining)
    if type(reconciled_at_ns) is not int or not 0 < reconciled_at_ns <= at_ns <= reconciled_at_ns+FRESH_NS:
        return PartialExitDecision('REVIEW','POSITION_STALE',remaining)
    if remaining == 0:
        return PartialExitDecision('FLAT','POSITION_RECONCILED',remaining)
    if len(attempts) >= terms.max_attempts:
        return PartialExitDecision('REVIEW','ATTEMPTS_EXHAUSTED',remaining)
    if quote is None:
        return PartialExitDecision('REVIEW','QUOTE_UNAVAILABLE',remaining)
    try:
        close_size, bound = bounded_reduction(quote, _scope(terms,remaining), instrument, remaining, at_ns=at_ns)
        if quote['observed_at_ns'] < reconciled_at_ns:
            return PartialExitDecision('REVIEW','QUOTE_PRECEDES_RECONCILIATION',remaining)
        if quote['event_time_ns'] > quote['observed_at_ns']:
            return PartialExitDecision('REVIEW','QUOTE_NOT_EXECUTABLE',remaining)
    except (ValueError, KeyError, TypeError):
        return PartialExitDecision('REVIEW','QUOTE_NOT_EXECUTABLE',remaining)
    min_quantity = max(instrument.min_size,
        (instrument.min_notional/(bound*step)).to_integral_value(rounding=ROUND_CEILING)*step)

    def close() -> PartialExitDecision:
        size = close_size if close_size == remaining else min(close_size, remaining-min_quantity)
        if size < min_quantity:
            return PartialExitDecision('REVIEW','RESIDUAL_BELOW_MINIMUM',remaining)
        return PartialExitDecision('CLOSE',close_reason or 'CLOSE_IN_PROGRESS',remaining,quantity=size,price_bound=bound)

    # A prior close claim keeps the rest in the closing phase. A final target
    # can first consume crossed partials while the protected attempt reserve holds.
    if closing or close_reason not in (None,'TAKE_PROFIT'):
        return close()
    active = _active_target(unfilled, instrument.min_size)
    if active is None or len(attempts) >= partial_attempt_limit:
        return close() if close_reason else PartialExitDecision('HOLD',
            'PARTIALS_DONE' if active is None else 'CLOSE_ATTEMPTS_RESERVED',remaining)
    target = strategy_number(profile.partial_targets[active].target)
    trigger_notional = entry_notional * (1 + sign*target)
    current_notional = number(quote['bid' if long else 'ask']) * entry_size
    if (current_notional < trigger_notional if long else current_notional > trigger_notional):
        return close() if close_reason else PartialExitDecision('HOLD','TARGET_NOT_REACHED',remaining)
    size = min(unfilled[active], close_size)
    residual = remaining-size
    if size < min_quantity or residual < min_quantity:
        return close() if close_reason else PartialExitDecision('HOLD','PARTIAL_OR_RESIDUAL_BELOW_MINIMUM',remaining)
    return PartialExitDecision('PARTIAL','PARTIAL_TARGET',remaining,active+1,size,bound)


def plan_partial_exit_order(terms: PerpPartialExitTerms, instrument: MainnetInstrument,
                            source: PartialExitObservation, *, client_order_id: str, nonce: int,
                            expiration_ns: int) -> tuple[PartialExitDecision,dict|None]:
    terms = PerpPartialExitTerms.model_validate(terms.payload())
    decision = decide_partial_exit(terms,instrument,source)
    if decision.quantity is None:
        return decision,None
    if any(row.client_order_id == client_order_id for row in source.attempts):
        raise ValueError('Identificador de reducción ya utilizado')
    assert decision.price_bound is not None
    return decision,unsigned_reduction(_scope(terms,decision.remaining),instrument,cid=client_order_id,
        size=decision.quantity,price=decision.price_bound,nonce=nonce,
        issued_at_ns=source.at_ns,expiration_ns=expiration_ns)
