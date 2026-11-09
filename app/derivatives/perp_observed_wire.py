"""Strict observation inputs and deterministic, bounded hot-state evidence.

Checkpoints are evidence, never deserialized into an account. A cold account is
reconstructed from the seed and source events. The hot checkpoint binds the
candle chain and live orders without reserializing growing historical arrays.
"""
from __future__ import annotations

from dataclasses import fields,is_dataclass,replace
from decimal import Decimal
import json
from typing import Annotated,Literal

from pydantic import BaseModel,ConfigDict,Field,TypeAdapter,model_validator

from app.derivatives.funding import FundingRecord
from app.derivatives.paper_math import InstrumentSpec,MarginRules,PerpQuote
from app.derivatives.perp_observed import (
    observe,observed_gap,request_observed_close,start_observed,stop_observed,_quote,_metadata,_funding,
)
from app.derivatives.perp_observed_types import Observation,ObservedState,ObservedTransition,ObservedError,observed_version
from app.lab.engine import Candle,number,validate_strategy,INTERVALS
from app.lab.perp_account import reservations
from app.lab.perp_book_types import Market,exact
from app.lab.perp_dataset import FundingPoint,RiskSnapshot,RiskTier,TradeBar
from app.lab.perp_profile import PerpLimitProfile, PerpPartialProfile, PerpProfile
from app.lab.store import content_hash,encoded


Nanoseconds=Annotated[str,Field(pattern=r'^[1-9][0-9]{0,19}$')]


class ExactInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True,populate_by_name=True)

    def payload(self) -> dict:
        return self.model_dump(mode='json',by_alias=True)


class QuoteInput(ExactInput):
    instrument: str=Field(pattern=r'^[A-Z0-9]{1,20}_USDT_Perp$')
    event_time_ns: Nanoseconds
    mark_price: str
    bid: str
    ask: str

    def quote(self) -> PerpQuote:
        return PerpQuote(self.instrument,int(self.event_time_ns),number(self.mark_price,positive=True),
                         number(self.bid,positive=True),number(self.ask,positive=True))

    @model_validator(mode='after')
    def valid(self):
        self.quote();return self


def quote_input(quote: PerpQuote) -> QuoteInput:
    return QuoteInput(instrument=quote.instrument,event_time_ns=str(quote.event_time_ns),
        mark_price=format(quote.mark,'f'),bid=format(quote.bid,'f'),ask=format(quote.ask,'f'))


def funding_input(record: FundingRecord) -> FundingPoint:
    return FundingPoint(instrument=record.instrument,funding_time=str(record.time_ns),
        funding_rate=format(record.rate_percent,'f'),mark_price=format(record.mark,'f'),
        funding_interval_hours=record.interval_hours)


class ObservedRiskTier(RiskTier):
    # The existing rule codec emits integer maxima as Decimal text. Preserve
    # that representation and reject fractional maxima instead of truncating.
    max_leverage: str=Field(pattern=r'^[1-9][0-9]{0,4}$')

    @model_validator(mode='after')
    def valid_leverage(self):
        if not 1<=number(self.max_leverage)<=10000:
            raise ValueError('Límite de apalancamiento fuera de rango')
        return self


class ObservedRiskSnapshot(RiskSnapshot):
    risk_brackets: list[ObservedRiskTier]=Field(min_length=1,max_length=100)


def rules_input(rules: MarginRules) -> ObservedRiskSnapshot:
    return ObservedRiskSnapshot.model_validate({**rules.json(),'fetched_at_ns':str(rules.fetched_at_ns)})


class ObservedSpecification(ExactInput):
    instrument: str=Field(pattern=r'^[A-Z0-9]{1,20}_USDT_Perp$')
    fetched_at_ns: Nanoseconds
    min_size: str
    size_step: str
    min_notional: str
    tick_size: str
    max_position_size: str|None


def specification_input(spec: InstrumentSpec) -> ObservedSpecification:
    # Serialize the validated execution specification, not a fabricated copy of
    # the provider's raw catalogue (which may contain additional venues/fields).
    return ObservedSpecification.model_validate({**spec.json(),'fetched_at_ns':str(spec.fetched_at_ns)})


def _rules(raw: ObservedRiskSnapshot,now_ns: int) -> MarginRules:
    return MarginRules.from_payload({**raw.model_dump(),'fetched_at_ns':int(raw.fetched_at_ns)},raw.instrument,now_ns)


def _specification(raw: ObservedSpecification,now_ns: int) -> InstrumentSpec:
    return InstrumentSpec(raw.instrument,int(raw.fetched_at_ns),number(raw.min_size,positive=True),
        number(raw.size_step,positive=True),number(raw.min_notional,positive=True),number(raw.tick_size,positive=True),
        number(raw.max_position_size,positive=True) if raw.max_position_size is not None else None)


def _candles(rows: list[TradeBar]) -> tuple[Candle,...]:
    return tuple(Candle(row.t,*(number(getattr(row,key)) for key in ('o','h','l','c','v'))) for row in rows)


class ObservedSeed(ExactInput):
    schema_name: Literal['orbis.paper.perp.observed.seed.v1']=Field(alias='schema')
    engine: Literal['perp-observed-1.0']
    profile: PerpProfile
    definition: dict[str,object]
    timeframe: Literal['1m','5m','15m','1h','4h','1d']
    received_at_ns: Nanoseconds
    quote: QuoteInput
    rules: ObservedRiskSnapshot
    specification: ObservedSpecification
    history: list[TradeBar]=Field(min_length=1,max_length=100_000)
    funding: list[FundingPoint]=Field(min_length=1,max_length=10_000)

    @model_validator(mode='after')
    def canonical_definition(self):
        if validate_strategy(self.definition)!=self.definition:
            raise ValueError('La definición del seed no está normalizada')
        return self

    @exact
    def start(self) -> ObservedTransition:
        # Reparse mutable nested lists/models before using a saved input.
        checked=read_observed_seed(self.payload())
        now=int(checked.received_at_ns);instrument=checked.quote.instrument
        market=Market(instrument,checked.profile.margin_mode,number(checked.profile.leverage),
            number(checked.definition['fee_rate']),_specification(checked.specification,now),_rules(checked.rules,now))
        return start_observed(checked.profile,checked.definition,checked.timeframe,market,
            _candles(checked.history),checked.quote.quote(),now,
            tuple(FundingRecord.from_payload(row.model_dump(),instrument) for row in checked.funding))


class ObservedLimitSeed(ObservedSeed):
    schema_name: Literal['orbis.paper.perp.observed.seed.v2'] = Field(alias='schema')
    engine: Literal['perp-observed-2.0']
    profile: PerpLimitProfile


class ObservedPartialSeed(ObservedSeed):
    schema_name: Literal['orbis.paper.perp.observed.seed.v3'] = Field(alias='schema')
    engine: Literal['perp-observed-3.0']
    profile: PerpPartialProfile


_SEED = TypeAdapter(Annotated[ObservedSeed | ObservedLimitSeed | ObservedPartialSeed,
                              Field(discriminator='schema_name')])


def read_observed_seed(payload: dict) -> ObservedSeed | ObservedLimitSeed | ObservedPartialSeed:
    return _SEED.validate_python(payload)


class EventInput(ExactInput):
    schema_name: Literal['orbis.paper.perp.observed.input.v1']=Field(alias='schema')
    received_at_ns: Nanoseconds


class QuoteEvent(EventInput):
    kind: Literal['OBSERVATION']
    collected_at_ns: Nanoseconds|None=None
    quote: QuoteInput
    rules: ObservedRiskSnapshot
    specification: ObservedSpecification
    funding: list[FundingPoint]=Field(min_length=1,max_length=10_000)
    closed_candles: list[TradeBar]=Field(default_factory=list,max_length=100_000)

    @model_validator(mode='after')
    def collection_precedes_acceptance(self):
        if self.collected_at_ns is not None and not 0<=int(self.received_at_ns)-int(self.collected_at_ns)<=15_000_000_000:
            raise ValueError('Recogida posterior o demasiado antigua respecto a la aceptación')
        return self

    def payload(self) -> dict:
        value=super().payload()
        # Old valid source streams omitted this field. Preserve their exact
        # canonical payload and their existing evidence, without reinterpretation.
        if self.collected_at_ns is None:value.pop('collected_at_ns')
        return value

    def batch(self) -> Observation:
        now=int(self.received_at_ns);instrument=self.quote.instrument
        return Observation(self.quote.quote(),now,
            tuple(FundingRecord.from_payload(row.model_dump(),instrument) for row in self.funding),
            _rules(self.rules,now),_specification(self.specification,now),_candles(self.closed_candles))


class StopEvent(EventInput):
    kind: Literal['STOP']


class CloseEvent(EventInput):
    kind: Literal['CLOSE_REQUEST']


class GapEvent(EventInput):
    kind: Literal['GAP']
    reason: str=Field(min_length=1,max_length=160,pattern=r'^[A-Z0-9_]+$')


ObservedInput=Annotated[QuoteEvent|StopEvent|CloseEvent|GapEvent,Field(discriminator='kind')]
INPUT=TypeAdapter(ObservedInput)


def observation_input(batch: Observation) -> QuoteEvent:
    return QuoteEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='OBSERVATION',
        received_at_ns=str(batch.received_at_ns),quote=quote_input(batch.quote),rules=rules_input(batch.rules),
        specification=specification_input(batch.specification),funding=[funding_input(row) for row in batch.funding],
        closed_candles=[TradeBar.model_validate(bar.json()) for bar in batch.closed_candles])


@exact
def apply_input(state: ObservedState,event: ObservedInput) -> ObservedTransition:
    checked=INPUT.validate_python(event.payload())
    now=int(checked.received_at_ns)
    if isinstance(checked,QuoteEvent):
        if checked.collected_at_ns is not None:
            collected=int(checked.collected_at_ns)
            if collected<state.last_received_ns:raise ObservedError('Recogida anterior al estado observado')
            original=checked.model_copy(update={'received_at_ns':checked.collected_at_ns}).batch()
            instrument=state.account.markets[0].instrument
            _quote(original.quote,instrument,collected);_metadata(original.rules,original.specification,instrument,collected)
            _funding(original.funding,instrument,collected)
            if any((bar.t+INTERVALS[state.timeframe])*10**9>collected for bar in original.closed_candles):
                raise ObservedError('La fuente recogió una vela todavía abierta')
        result=observe(state,checked.batch())
        if checked.collected_at_ns is not None:
            result=replace(result,events_json=(encoded({'kind':'QUOTE_ACCEPTED',
                'collected_at_ns':checked.collected_at_ns,'received_at_ns':checked.received_at_ns,
                'quote_time_ns':checked.quote.event_time_ns}),*result.events_json))
        return result
    if isinstance(checked,StopEvent):return stop_observed(state,now)
    if isinstance(checked,CloseEvent):return request_observed_close(state,now)
    return observed_gap(state,now,checked.reason)


def seed_history_hash(state: ObservedState) -> str:
    return content_hash([bar.json() for bar in state.history])


def next_history_hash(before: ObservedState,after: ObservedState,previous: str) -> str:
    if len(after.history)<len(before.history):raise ValueError('El archivo de velas retrocede')
    for bar in after.history[len(before.history):]:
        previous=content_hash({'previous':previous,'candle':bar.json()})
    return previous


def _exact_json(value):
    if isinstance(value,Decimal):return format(value,'f')
    if isinstance(value,BaseModel):return value.model_dump(mode='json',by_alias=True)
    if is_dataclass(value):return {field.name:(str(getattr(value,field.name))
        if field.name.endswith('_ns') and type(getattr(value,field.name)) is int
        else _exact_json(getattr(value,field.name))) for field in fields(value)}
    if type(value) is int:return str(value) if abs(value)>2**53-1 else value
    if isinstance(value,(list,tuple)):return [_exact_json(item) for item in value]
    if isinstance(value,dict):return {key:_exact_json(item) for key,item in value.items()}
    if value is None or type(value) in (str,bool):return value
    raise TypeError('Tipo no permitido en el checkpoint observado')


@exact
def checkpoint(state: ObservedState,history_hash: str) -> dict:
    """Small comparison-only snapshot. Never use as an account loader."""
    account=replace(state.account,orders=tuple(order for order in state.account.orders if order.active))
    version, engine = observed_version(state.profile)
    result = {'schema':f'orbis.paper.perp.observed.checkpoint.v{version}','engine':engine,'environment':'PAPER',
        'profile_hash':content_hash(state.profile.payload()),'definition_hash':content_hash(json.loads(state.definition_json)),
        'timeframe':state.timeframe,'account':_exact_json(account),
        'history':{'count':len(state.history),'first_t':state.history[0].t,'last_t':state.history[-1].t,
                   'hash':history_hash,'method':'SEED_THEN_APPEND_V1'},
        'funding_anchor':_exact_json(state.funding_anchor),'started_at_ns':str(state.started_at_ns),
        'last_received_ns':str(state.last_received_ns),'last_control_ns':str(state.last_control_ns),
        'last_quote':_exact_json(state.last_quote),'status':state.status,'reason':state.reason,
        'opened':_exact_json(state.opened),'close_requested':state.close_requested,
        'order_count':state.order_count,'retained_orders':len(state.account.orders),'closed_trades':state.closed_trades}
    if version >= 2:
        result['resting'] = _exact_json(state.resting)
        if state.resting is not None:
            reserved = reservations(state.account)
            result['order_reservation'] = {
                'margin': format(reserved.cross_initial + reserved.isolated_pending, 'f'),
                'fees': format(reserved.fees, 'f'),
                'available': format(reserved.available, 'f'),
            }
    if version == 3:
        assert isinstance(state.profile, PerpPartialProfile)
        result['entry_kind'] = 'LIMIT' if state.profile.entry_order is not None else 'MARKET'
        if result['opened'] is not None:
            result['opened']['partial_exits'] = [json.loads(row) for row in result['opened'].pop('partial_exits_json')]
    return result


def transition_evidence(before: ObservedState|None,transition: ObservedTransition,previous_history_hash: str|None) -> tuple[str,dict]:
    history=(seed_history_hash(transition.state) if before is None else
             next_history_hash(before,transition.state,previous_history_hash))
    return history,{'checkpoint':checkpoint(transition.state,history),
        'journal':_exact_json(transition.journal),'signals':[json.loads(raw) for raw in transition.signals_json],
        'trades':[json.loads(raw) for raw in transition.trades_json],
        'events':[json.loads(raw) for raw in transition.events_json]}
