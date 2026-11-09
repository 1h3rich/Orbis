"""Reproducible contract for a standalone GRVT perpetual LIMIT entry."""
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR
from typing import Annotated, Literal, Self
import json

from pydantic import Field, TypeAdapter, model_validator

from app.derivatives.perp_observed_types import MAX_SIGNAL_DELAY_NS
from app.derivatives.perp_observed_wire import ExactInput, Nanoseconds
from app.lab.engine import INTERVALS, number, validate_rule_timeframes, validate_strategy
from app.lab.perp_book_types import Market, QUANTUM, exact, money
from app.lab.perp_dataset import SYMBOLS, TradeBar
from app.lab.perp_execution_math import limit_bound, opening_size
from app.lab.store import content_hash
from app.live_execution.grvt_mainnet_bracket import ZERO_ADDRESS, client_ids, exact_uint
from app.live_execution.grvt_perp_plan import FRESH_NS, PerpMarketInput, PerpRealBudget, planning_market
from app.live_execution.grvt_perp_partial_targets import PerpPartialExitTerms
from app.live_execution.grvt_perp_signals import PerpLimitSignalSeed, PerpPartialSignalSeed, replay_signal_records
from app.research.perp_live_review import ObservedReviewPrefix
from app.research.perp_reference import PerpLimitResearchReference


NS = 10**9
ENGINE = 'grvt-perp-limit-entry-1.0'
PLAN_SCHEMA = 'orbis.grvt.perp.limit.entry.plan.v1'
PARTIAL_PLAN_SCHEMA = 'orbis.grvt.perp.limit.entry.plan.v2'


class PerpLimitEntryTerms(ExactInput):
    schema_name: Literal['orbis.grvt.perp.limit.entry.terms.v1'] = Field(alias='schema')
    research: PerpLimitResearchReference
    paper: ObservedReviewPrefix
    definition: dict
    budget: PerpRealBudget
    max_pending_seconds: int = Field(ge=5, le=86400)

    @property
    def instrument(self) -> str:
        return self.research.symbol.replace('/', '_').replace(':PERP', '_Perp')

    @model_validator(mode='after')
    @exact
    def coherent(self) -> Self:
        normalized = validate_strategy(self.definition)
        validate_rule_timeframes(normalized, self.research.timeframe)
        if (self.research.symbol not in SYMBOLS or normalized != self.definition
                or content_hash(normalized) != self.research.profile.strategy_hash
                or number(normalized['fee_rate']) > number(self.budget.fee_rate_ceiling)
                or number(normalized['slippage_rate']) * 10000 > self.budget.max_slippage_bps):
            raise ValueError('Perfil, costes o presupuesto LIMIT REAL incompatibles')
        if (self.paper.stored_status != 'STOPPED' or self.paper.closed_trades < 1
                or self.paper.observed_bars < 1 or self.paper.has_estimated_closure
                or self.paper.source_advanced):
            raise ValueError('El plan requiere una prueba PAPER LIMIT completa')
        return self


class PerpPartialLimitEntryTerms(PerpPartialExitTerms):
    schema_name: Literal['orbis.grvt.perp.limit.entry.terms.v2'] = Field(alias='schema')
    max_pending_seconds: int = Field(ge=5, le=86400)

    @model_validator(mode='after')
    def limit_profile(self) -> Self:
        if self.research.profile.entry_order is None:
            raise ValueError('Los términos de entrada LIMIT requieren una entrada LIMIT')
        return self

    def exit_terms(self) -> PerpPartialExitTerms:
        raw = self.model_dump(mode='json',by_alias=True,exclude={'max_pending_seconds'})
        return PerpPartialExitTerms.model_validate({**raw,'schema':'orbis.grvt.perp.partial.exit.terms.v1'})


LimitTerms = Annotated[PerpLimitEntryTerms | PerpPartialLimitEntryTerms, Field(discriminator='schema_name')]
LimitSignalSeed = PerpLimitSignalSeed | PerpPartialSignalSeed
_TERMS = TypeAdapter(LimitTerms)


def read_limit_terms(payload: dict) -> LimitTerms:
    return _TERMS.validate_python(payload)


class PerpLimitPlanInput(PerpMarketInput):
    terms: LimitTerms
    signal_candle: TradeBar
    signal_known_at_ns: Nanoseconds
    signal_decision_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    entry_deadline_ns: Nanoseconds


@exact
def calculate_limit_plan(source: PerpLimitPlanInput) -> dict:
    source = PerpLimitPlanInput.model_validate(source.payload())
    terms = source.terms
    budget = terms.budget
    profile = terms.research.profile
    entry = profile.entry_order
    assert entry is not None
    candle = source.signal_candle
    at = int(source.checked_at_ns)
    known = int(source.signal_known_at_ns)
    deadline = int(source.entry_deadline_ns)
    step = INTERVALS[terms.research.timeframe]
    closed = (candle.t + step) * NS
    expires = min(closed + entry.valid_bars * step * NS,
                  known + terms.max_pending_seconds * NS, deadline)
    if (candle.t % step or candle.t <= terms.research.dataset_to_t
            or closed <= int(terms.paper.last_received_at_ns)
            or not 0 <= known - closed <= MAX_SIGNAL_DELAY_NS
            or not 0 <= at - known <= MAX_SIGNAL_DELAY_NS
            or not 0 < deadline < 2**63
            or not at + NS <= expires < 2**63):
        raise ValueError('Vela de señal, vigencia o plazo LIMIT REAL fuera de alcance')
    instrument, quote, spec, rules, cash = planning_market(source, terms.instrument, budget)
    buying = terms.definition['direction'] == 'LONG'
    bound = limit_bound(number(candle.c, positive=True), number(entry.offset),
                        spec.tick_size, buying=buying)
    exact_uint(bound, 9, 2**64 - 1)
    # The SELL price is a floor. Current bid is part of sizing, and each
    # observed execution must still be checked against the notional budget.
    sizing_price = bound if buying else max(bound, quote.bid)
    policy_size_cap = number(budget.entry_notional_limit) / sizing_price
    policy_size_cap = (policy_size_cap / spec.size_step).to_integral_value(rounding=ROUND_FLOOR) * spec.size_step
    if policy_size_cap < spec.min_size:
        raise ValueError('El tope LIMIT REAL no permite el tamaño mínimo')
    max_size = min(spec.max_size, policy_size_cap) if spec.max_size is not None else policy_size_cap
    market = Market(instrument.name, profile.margin_mode, number(profile.leverage),
                    number(budget.fee_rate_ceiling), replace(spec, max_size=max_size), rules)
    allocated = min(money(cash * number(terms.definition['allocation'])), number(budget.entry_cash_limit))
    size = opening_size(allocated, Decimal(1), market, bound, quote.mark, long=buying, fee_price=sizing_price)
    if size <= 0:
        raise ValueError('Presupuesto, mínimos o apalancamiento LIMIT REAL no ejecutables')
    exact_uint(size, instrument.base_decimals, 2**64 - 1)
    tier = rules.tier_for(size * quote.mark)
    margin = money(size * quote.mark * max(tier.initial_rate, 1 / market.leverage), up=True)
    adverse = max(Decimal(0), bound - quote.mark if buying else quote.mark - bound)
    buffer = money(size * adverse, up=True)
    fees = money(size * sizing_price * market.fee_rate, up=True)
    required = margin + buffer + fees + 2 * QUANTUM
    # A price-improved SELL can cost a larger fee even when its MARK buffer
    # decreases. Reserve both independently, using the worst relevant side.
    if required > allocated:
        raise ValueError('Reserva LIMIT REAL superior al efectivo autorizado')
    return {
        'engine': 'grvt-perp-limit-entry-2.0' if isinstance(terms,PerpPartialLimitEntryTerms) else ENGINE,
        'execution_authority': 'NONE', 'time_in_force': 'GOOD_TILL_TIME',
        'expires_at_ns': str(expires), 'side': 'BUY' if buying else 'SELL',
        'limit_price': format(bound, 'f'), 'size': format(size, 'f'),
        'limit_notional': format(size * bound, 'f'), 'sizing_notional': format(size * sizing_price, 'f'),
        'entry_cash_budget': format(allocated, 'f'), 'margin_estimate': format(margin, 'f'),
        'adverse_mark_buffer': format(buffer, 'f'), 'fee_reserve': format(fees, 'f'),
        'cash_required_estimate': format(required, 'f'),
        'protection': 'MANAGE_CONFIRMED_FILLED_QUANTITY',
    }


class PerpLimitEntryPlan(ExactInput):
    schema_name: Literal['orbis.grvt.perp.limit.entry.plan.v1','orbis.grvt.perp.limit.entry.plan.v2'] = Field(alias='schema')
    source: PerpLimitPlanInput
    result: dict
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @property
    def revision(self) -> Literal[1,2]:
        return 2 if isinstance(self.source.terms,PerpPartialLimitEntryTerms) else 1

    @model_validator(mode='after')
    def replay(self) -> Self:
        if (self.schema_name != (PARTIAL_PLAN_SCHEMA if self.revision==2 else PLAN_SCHEMA)
                or content_hash(self.payload_without_hash()) != self.sha256
                or self.result != calculate_limit_plan(self.source)):
            raise ValueError('Plan LIMIT REAL no reproducible')
        return self

    def payload_without_hash(self) -> dict:
        return self.model_dump(mode='json', by_alias=True, exclude={'sha256'})


def build_limit_plan(source: PerpLimitPlanInput) -> PerpLimitEntryPlan:
    result = calculate_limit_plan(source)
    schema = PARTIAL_PLAN_SCHEMA if isinstance(source.terms,PerpPartialLimitEntryTerms) else PLAN_SCHEMA
    payload = {'schema': schema, 'source': source.payload(), 'result': result}
    return PerpLimitEntryPlan.model_validate({**payload, 'sha256': content_hash(payload)})


def limit_plan_from_signal(terms: LimitTerms, market: PerpMarketInput,
                           seed: LimitSignalSeed, records: list[dict], *,
                           entry_deadline_ns: str) -> PerpLimitEntryPlan:
    """Recalculate the entry decision from a new REAL feed and bind its candle."""
    terms = read_limit_terms(terms.payload())
    market = PerpMarketInput.model_validate(market.payload())
    seed_type = PerpPartialSignalSeed if isinstance(terms,PerpPartialLimitEntryTerms) else PerpLimitSignalSeed
    seed = seed_type.model_validate(seed.payload())
    reference = terms.research
    if (seed.origin_reference_hash != reference.sha256
            or seed.profile.payload() != reference.profile.payload()
            or seed.definition != terms.definition
            or seed.instrument != terms.instrument or seed.timeframe != reference.timeframe
            or int(seed.requested_at_ns) < int(terms.paper.last_received_at_ns)):
        raise ValueError('El calentamiento LIMIT no corresponde al origen revisado')
    replayed = replay_signal_records(seed, records)
    if (replayed.state.status != 'RUNNING' or not replayed.decisions_json
            or int(market.checked_at_ns) < replayed.state.last_accepted_ns
            or int(market.quote.event_time_ns) < replayed.state.last_quote.event_time_ns):
        raise ValueError('Falta una señal LIMIT vigente y reproducible')
    decision = json.loads(replayed.decisions_json[-1])
    candle = replayed.state.history[-1]
    if decision['entry_matched'] is not True or decision['source_candle_t'] != candle.t:
        raise ValueError('La última vela no acredita una señal LIMIT de entrada')
    source = PerpLimitPlanInput.model_validate({
        **market.payload(), 'terms': terms.payload(), 'signal_candle': candle.json(),
        'signal_known_at_ns': decision['known_at_ns'], 'signal_decision_hash': decision['sha256'],
        'entry_deadline_ns': entry_deadline_ns,
    })
    return build_limit_plan(source)


def unsigned_limit_entry(plan: PerpLimitEntryPlan, *, client_order_id: str, nonce: int,
                         issued_at_ns: int) -> dict:
    plan = PerpLimitEntryPlan.model_validate(plan.payload())
    client_ids(client_order_id)
    expiry = int(plan.result['expires_at_ns'])
    checked = int(plan.source.checked_at_ns)
    known = int(plan.source.signal_known_at_ns)
    if (type(nonce) is not int or not 0 <= nonce < 2**32 or type(issued_at_ns) is not int
            or not 0 <= issued_at_ns - checked <= FRESH_NS
            or issued_at_ns - known > MAX_SIGNAL_DELAY_NS or issued_at_ns + NS > expiry):
        raise ValueError('Vigencia de firma LIMIT REAL fuera de alcance')
    terms = plan.source.terms
    return {
        'sub_account_id': terms.budget.sub_account_id, 'is_market': False,
        'time_in_force': 'GOOD_TILL_TIME', 'post_only': False, 'reduce_only': False,
        'legs': [{'instrument': terms.instrument, 'size': plan.result['size'],
                  'limit_price': plan.result['limit_price'],
                  'is_buying_asset': plan.result['side'] == 'BUY'}],
        'metadata': {'client_order_id': client_order_id}, 'builder': ZERO_ADDRESS, 'builder_fee': '0',
        'signature': {'nonce': nonce, 'expiration': str(expiry), 'chain_id': '325'},
    }
