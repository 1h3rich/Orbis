"""Exact REAL sizing, with independent limits and no execution authority.

The shared book estimates a reserve, not exchange acceptance. PAPER capital is
never an input to the size. The execution mapping explicitly differs from a
historical assumed market fill: bounded FOK with native LAST-based brackets.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Literal, Self

from pydantic import Field, model_validator

from app.derivatives.perp_observed_wire import (
    ExactInput, ObservedRiskSnapshot, ObservedSpecification, QuoteInput, _rules, _specification,
)
from app.lab.engine import INTERVALS,number,validate_rule_timeframes,validate_strategy
from app.lab.perp_account import fill_order, mark_all, new_account, risk_pockets, submit_order
from app.lab.perp_book_types import Cursor, Market, QUANTUM, exact, money
from app.lab.perp_execution_math import adverse_price, opening_size
from app.lab.perp_profile import PerpProfile
from app.lab.store import content_hash
from app.live_execution.grvt_mainnet_bracket import MainnetBracketDraft, MainnetInstrument, unsigned_mainnet_bracket


PLAN_ENGINE = 'grvt-perp-entry-1.0'
FRESH_NS = 5_000_000_000


class PerpRealBudget(ExactInput):
    schema_name: Literal['orbis.grvt.perp.real.budget.v1'] = Field(alias='schema')
    sub_account_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    collateral_limit: str
    entry_cash_limit: str
    entry_notional_limit: str
    exit_notional_limit: str
    fee_rate_ceiling: str
    exit_seconds: int = Field(ge=120,le=86400)
    max_spread_bps: int = Field(ge=0,le=100)
    max_slippage_bps: int = Field(ge=0,le=100)
    collateral_scope: Literal['WHOLE_DEDICATED_USDT_SUBACCOUNT']

    @model_validator(mode='after')
    @exact
    def valid(self) -> Self:
        if int(self.sub_account_id) >= 2**64:
            raise ValueError('Subcuenta fuera de rango')
        limits = [number(getattr(self,key),positive=True) for key in
                  ('collateral_limit','entry_cash_limit','entry_notional_limit','exit_notional_limit')]
        if (any(money(value)!=value for value in limits)
                or not limits[1] <= limits[0] <= 10000
                or not limits[2] <= 1000 or not limits[2] <= limits[3] <= 10000
                or not 0 <= number(self.fee_rate_ceiling) <= Decimal('.01')):
            raise ValueError('Presupuesto REAL fuera de límites')
        return self


class PerpEntryTerms(ExactInput):
    schema_name: Literal['orbis.grvt.perp.entry.terms.v1'] = Field(alias='schema')
    instrument: str = Field(pattern=r'^[A-Z0-9]{1,20}_USDT_Perp$')
    timeframe: str
    profile: PerpProfile
    definition: dict
    budget: PerpRealBudget
    execution_mapping: Literal['BOUNDED_FOK_WITH_NATIVE_LAST_BRACKETS']
    fee_assumption: Literal['USER_CEILING_NOT_VERIFIED_EXCHANGE_FEE']
    reserve_assumption: Literal['SHARED_BOOK_ESTIMATE_NOT_EXCHANGE_APPROVAL']
    execution_scope: Literal['PROTECTED_ENTRY_ONLY_NOT_SIGNAL_OR_EXIT_COORDINATOR']

    @model_validator(mode='after')
    @exact
    def coherent(self) -> Self:
        normalized = validate_strategy(self.definition)
        if self.timeframe not in INTERVALS:
            raise ValueError('Temporalidad REAL no compatible')
        validate_rule_timeframes(normalized,self.timeframe)
        if (normalized != self.definition or content_hash(normalized) != self.profile.strategy_hash
                or number(normalized['fee_rate']) > number(self.budget.fee_rate_ceiling)
                or number(normalized['slippage_rate']) * 10000 > self.budget.max_slippage_bps):
            raise ValueError('Perfil, costes o presupuesto REAL incompatibles')
        return self


class PerpInstrument(ExactInput):
    name: str
    asset_id: str = Field(pattern=r'^[1-9][0-9]{0,77}$')
    base_decimals: int
    tick_size: str
    min_size: str
    min_notional: str

    def instrument(self) -> MainnetInstrument:
        return MainnetInstrument(self.name,int(self.asset_id),self.base_decimals,
            number(self.tick_size,positive=True),number(self.min_size,positive=True),number(self.min_notional,positive=True))

    @model_validator(mode='after')
    def valid(self) -> Self:
        self.instrument()
        return self


def instrument_input(instrument: MainnetInstrument) -> PerpInstrument:
    return PerpInstrument.model_validate({key:str(value) if isinstance(value,Decimal) or key=='asset_id' else value
        for key,value in asdict(instrument).items() if key!='network'})


class PerpMarketInput(ExactInput):
    instrument: PerpInstrument
    specification: ObservedSpecification
    rules: ObservedRiskSnapshot
    quote: QuoteInput
    account_equity: str
    checked_at_ns: str = Field(pattern=r'^[1-9][0-9]{0,18}$')


class PerpPlanInput(PerpMarketInput):
    terms: PerpEntryTerms


def planning_market(source: PerpMarketInput, name: str, budget: PerpRealBudget):
    at = int(source.checked_at_ns)
    if at >= 2**63:
        raise ValueError('Hora de planificación inválida')
    instrument = source.instrument.instrument()
    quote = source.quote.quote()
    spec = _specification(source.specification,at)
    rules = _rules(source.rules,at)
    if (any(not 0 <= at-stamp <= FRESH_NS for stamp in (quote.event_time_ns,spec.fetched_at_ns,rules.fetched_at_ns))
            or any(value != instrument.name for value in (name,quote.instrument,spec.instrument,rules.instrument))
            or (instrument.min_size,instrument.tick_size,instrument.min_notional) != (spec.min_size,spec.tick_size,spec.min_notional)
            or spec.size_step != Decimal(1).scaleb(-instrument.base_decimals)):
        raise ValueError('Mercado actual inconsistente, antiguo o fuera de ámbito')
    cash = number(source.account_equity,positive=True)
    if money(cash)!=cash or cash > number(budget.collateral_limit):
        raise ValueError('Colateral REAL fuera del límite autorizado')
    if (quote.ask-quote.bid)*10000/((quote.ask+quote.bid)/2) > budget.max_spread_bps:
        raise ValueError('Spread fuera de límites REAL')
    return instrument, quote, spec, rules, cash


@exact
def calculate_perp_plan(source: PerpPlanInput) -> dict:
    source = PerpPlanInput.model_validate(source.payload())
    at = int(source.checked_at_ns)
    terms, budget = source.terms, source.terms.budget
    instrument, quote, spec, rules, cash = planning_market(source, terms.instrument, budget)
    allocated = min(money(cash*number(terms.definition['allocation'])),number(budget.entry_cash_limit))
    buying = terms.definition['direction']=='LONG'
    reference = quote.ask if buying else quote.bid
    price = adverse_price(reference,buying,number(terms.definition['slippage_rate']),spec.tick_size)
    if abs(price-reference)*10000/reference > budget.max_slippage_bps:
        raise ValueError('Deslizamiento fuera de límites REAL')
    sign = 1 if buying else -1
    rounding = ROUND_CEILING if buying else ROUND_FLOOR
    stop = (price*(1-sign*number(terms.definition['stop_loss']))/spec.tick_size).to_integral_value(rounding=rounding)*spec.tick_size
    take = (price*(1+sign*number(terms.definition['take_profit']))/spec.tick_size).to_integral_value(rounding=rounding)*spec.tick_size
    # Both native exits have adverse limit bounds. Apply their notional caps
    # before shared sizing, so discontinuous/non-monotone tiers are still tried.
    exits = [adverse_price(level,not buying,Decimal(budget.max_slippage_bps)/10000,spec.tick_size) for level in (stop,take)]
    if min(stop,take,*exits)<=0:
        raise ValueError('Protección REAL no ejecutable')
    size_cap = min(number(budget.entry_notional_limit)/price,number(budget.exit_notional_limit)/max(stop,take,*exits))
    size_cap = (size_cap/spec.size_step).to_integral_value(rounding=ROUND_FLOOR)*spec.size_step
    if size_cap < spec.min_size:
        raise ValueError('Los topes REAL no permiten el tamaño mínimo')
    effective_spec = replace(spec,max_size=min(spec.max_size,size_cap) if spec.max_size is not None else size_cap)
    market = Market(instrument.name,terms.profile.margin_mode,number(terms.profile.leverage),
                    number(budget.fee_rate_ceiling),effective_spec,rules)
    size = opening_size(allocated,Decimal(1),market,price,quote.mark,long=buying)
    if size<=0:
        raise ValueError('Presupuesto, mínimos o apalancamiento REAL no ejecutables')
    draft = MainnetBracketDraft(budget.sub_account_id,instrument.name,terms.definition['direction'],size,price,stop,take,
        number(budget.entry_notional_limit),number(budget.exit_notional_limit),budget.exit_seconds,budget.max_spread_bps,budget.max_slippage_bps)
    unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=str(2**63),nonces=(1,2,3),issued_at_ns=at)
    tier = rules.tier_for(size*quote.mark)
    initial = money(size*quote.mark*max(tier.initial_rate,Decimal(1)/market.leverage),up=True)
    loss_buffer = money(size*max(Decimal(0),price-quote.mark if buying else quote.mark-price),up=True)
    fee = money(size*price*market.fee_rate,up=True)
    required = initial+loss_buffer+fee+2*QUANTUM
    if required>allocated:
        raise ValueError('Reserva REAL superior al efectivo autorizado')
    # A fresh modeled account is calculated, never decoded from a checkpoint.
    account = mark_all(new_account(cash,(market,)),Cursor(at,0),{instrument.name:quote.mark}).account
    account = submit_order(account,Cursor(at,1),identifier='entry',instrument=instrument.name,
        side='BUY' if buying else 'SELL',quantity=size,price_bound=price).account
    account = fill_order(account,Cursor(at,2),'entry',quantity=size,price=price).account
    if len(account.positions)!=1 or any(pocket.breached for pocket in risk_pockets(account)):
        raise ValueError('El modelo de reserva REAL no conserva una posición solvente')
    draft_payload={key:format(value,'f') if isinstance(value,Decimal) else value for key,value in asdict(draft).items()}
    return {'engine':PLAN_ENGINE,'draft':draft_payload,'profile_hash':content_hash(terms.profile.payload()),
        'account_equity':format(cash,'f'),'entry_cash_budget':format(allocated,'f'),
        'initial_margin_estimate':format(initial,'f'),'adverse_mark_buffer':format(loss_buffer,'f'),
        'fee_reserve':format(fee,'f'),'cash_required_estimate':format(required,'f'),
        'entry_notional':format(size*price,'f'),'policy_size_cap':format(size_cap,'f'),
        'model_execution':'BOUNDED_FOK_WITH_NATIVE_LAST_BRACKETS','execution_authority':'NONE'}


class PerpEntryPlan(ExactInput):
    schema_name: Literal['orbis.grvt.perp.entry.plan.v1'] = Field(alias='schema')
    source: PerpPlanInput
    result: dict
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def replay(self) -> Self:
        payload={'schema':self.schema_name,'source':self.source.payload(),'result':self.result}
        if content_hash(payload)!=self.sha256 or calculate_perp_plan(self.source)!=self.result:
            raise ValueError('Plan de entrada REAL no reproducible')
        return self


def build_perp_plan(source: PerpPlanInput) -> PerpEntryPlan:
    source=PerpPlanInput.model_validate(source.payload())
    payload={'schema':'orbis.grvt.perp.entry.plan.v1','source':source.payload(),'result':calculate_perp_plan(source)}
    return PerpEntryPlan.model_validate({**payload,'sha256':content_hash(payload)})


def plan_draft(plan: PerpEntryPlan) -> MainnetBracketDraft:
    plan=PerpEntryPlan.model_validate(plan.payload())
    amounts={'size','entry_price','stop_loss','take_profit','entry_cap','exit_cap'}
    return MainnetBracketDraft(**{key:number(value,positive=True) if key in amounts else value
        for key,value in plan.result['draft'].items()})
