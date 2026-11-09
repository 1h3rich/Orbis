"""Causal perpetual rules for a REAL coordinator, independent of PAPER capital.

This module only produces evidence. It has no orders, keys, positions, balance
or execution authority. Saved checkpoints cannot be loaded as runtime state;
cold readers replay the original seed and every input through this evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Literal

from pydantic import Field, model_validator

from app.derivatives.paper_math import PerpQuote
from app.derivatives.perp_observed_types import (
    MAX_HISTORY_BARS, MAX_QUOTE_AGE_NS, MAX_QUOTE_GAP_NS, MAX_SIGNAL_DELAY_NS,
)
from app.derivatives.perp_observed_wire import ExactInput, Nanoseconds, QuoteInput, _candles
from app.lab.coverage import validate_paper_timeframe_capacity
from app.lab.engine import (
    Candle, IndicatorCache, INTERVALS, evaluate, supports_engine_version,
    validate_candle_values, validate_strategy,
)
from app.lab.perp_book_types import exact
from app.lab.perp_dataset import TradeBar
from app.lab.perp_profile import PerpLimitProfile, PerpPartialProfile, PerpProfile, validate_partial_targets
from app.lab.store import content_hash, encoded


SIGNAL_ENGINE = 'grvt-perp-signals-1.0'
MAX_CONSUMER_DELAY_NS = 5_000_000_000
Timeframe = Literal['1m', '5m', '15m', '1h', '4h', '1d']


def _time(value: str) -> int:
    at = int(value)
    if value != str(at) or not 0 < at < 2**63:
        raise ValueError('Instante de señales no canónico')
    return at


def _quote(quote: PerpQuote, instrument: str, at: int) -> None:
    if quote.instrument != instrument or not 0 <= at-quote.event_time_ns <= MAX_QUOTE_AGE_NS:
        raise ValueError('Cotización de señales futura, antigua o de otro instrumento')


def _history(bars: tuple[Candle, ...], timeframe: str, through: int) -> None:
    if not 3 <= len(bars) <= MAX_HISTORY_BARS:
        raise ValueError('Histórico de señales vacío o excesivo')
    validate_candle_values(list(bars), timeframe)
    step = INTERVALS[timeframe]
    if (any(b.t-a.t != step for a,b in zip(bars,bars[1:]))
            or (bars[-1].t+step)*10**9 > through):
        raise ValueError('Histórico de señales con huecos o velas abiertas')


def _append_bar(head: str | None, bar: Candle) -> str:
    return content_hash({'previous': head, 'candle': bar.json()})


class PerpSignalSeed(ExactInput):
    schema_name: Literal['orbis.grvt.perp.signals.seed.v1'] = Field(alias='schema')
    engine: Literal['grvt-perp-signals-1.0']
    origin_reference_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    profile: PerpProfile
    definition: dict[str, object]
    instrument: str = Field(pattern=r'^[A-Z0-9]{1,20}_USDT_Perp$')
    timeframe: Timeframe
    requested_at_ns: Nanoseconds
    prepared_at_ns: Nanoseconds
    quote: QuoteInput
    history: list[TradeBar] = Field(min_length=3, max_length=MAX_HISTORY_BARS)
    execution_authority: Literal['NONE']

    @model_validator(mode='after')
    @exact
    def coherent(self):
        definition = validate_strategy(self.definition)
        at, requested = _time(self.prepared_at_ns), _time(self.requested_at_ns)
        if (definition != self.definition or content_hash(definition) != self.profile.strategy_hash
                or not supports_engine_version(self.profile.signal_engine, definition)
                or not 0 <= at-requested <= 120_000_000_000):
            raise ValueError('Definición, perfil o preparación de señales incompatible')
        _quote(self.quote.quote(), self.instrument, at)
        history = _candles(self.history)
        _history(history, self.timeframe, at)
        step = INTERVALS[self.timeframe]
        if history[-1].t != (at//10**9//step-1)*step:
            raise ValueError('El calentamiento no termina en la última vela cerrada')
        validate_paper_timeframe_capacity(definition, self.timeframe, history[0].t,
                                          max_base_bars=MAX_HISTORY_BARS)
        return self

    def start(self) -> PerpSignalState:
        checked = type(self).model_validate(self.payload())
        history = _candles(checked.history)
        head = None
        for bar in history:
            head = _append_bar(head, bar)
        assert head is not None
        return PerpSignalState(content_hash(checked.payload()), checked.origin_reference_hash,
            encoded(checked.profile.payload()), encoded(checked.definition), checked.instrument,
            checked.timeframe, int(checked.prepared_at_ns), history, head,
            int(checked.prepared_at_ns), int(checked.prepared_at_ns), checked.quote.quote())


class PerpLimitSignalSeed(PerpSignalSeed):
    schema_name: Literal['orbis.grvt.perp.signals.seed.v2'] = Field(alias='schema')
    profile: PerpLimitProfile


class PerpPartialSignalSeed(PerpSignalSeed):
    schema_name: Literal['orbis.grvt.perp.signals.seed.v3'] = Field(alias='schema')
    profile: PerpPartialProfile

    @model_validator(mode='after')
    @exact
    def targets(self):
        validate_partial_targets(self.profile, self.definition)
        return self


class PerpSignalInput(ExactInput):
    schema_name: Literal['orbis.grvt.perp.signals.input.v1'] = Field(alias='schema')
    collected_at_ns: Nanoseconds
    accepted_at_ns: Nanoseconds
    quote: QuoteInput
    closed_candles: list[TradeBar] = Field(default_factory=list, max_length=MAX_HISTORY_BARS)

    @model_validator(mode='after')
    def times(self):
        collected, accepted = _time(self.collected_at_ns), _time(self.accepted_at_ns)
        if not 0 <= accepted-collected <= MAX_CONSUMER_DELAY_NS:
            raise ValueError('Lote de señales antiguo o aceptado antes de su recogida')
        # Collection time is retained, never rewritten to disguise an old quote.
        _quote(self.quote.quote(), self.quote.instrument, collected)
        return self


@dataclass(frozen=True)
class PerpSignalState:
    seed_hash: str
    origin_reference_hash: str
    profile_json: str
    definition_json: str
    instrument: str
    timeframe: str
    started_at_ns: int
    history: tuple[Candle, ...]
    history_hash: str
    last_accepted_ns: int
    last_observed_ns: int
    last_quote: PerpQuote
    sequence: int = 0
    head_hash: str | None = None
    evaluated_bars: int = 0
    matched_entries: int = 0
    status: Literal['RUNNING', 'STOPPED', 'GAP'] = 'RUNNING'
    reason: str | None = None


@dataclass(frozen=True)
class PerpSignalTransition:
    state: PerpSignalState
    decisions_json: tuple[str, ...] = ()
    outcome: str = 'OBSERVED'


def signal_checkpoint(state: PerpSignalState) -> dict:
    """Bounded evidence, not a serialized state constructor or permission."""
    return {'schema':'orbis.grvt.perp.signals.checkpoint.v1', 'engine':SIGNAL_ENGINE,
        'seed_hash':state.seed_hash, 'origin_reference_hash':state.origin_reference_hash,
        'sequence':state.sequence, 'head_hash':state.head_hash, 'history_hash':state.history_hash,
        'history_bars':len(state.history), 'last_candle_t':state.history[-1].t,
        'started_at_ns':str(state.started_at_ns), 'last_accepted_ns':str(state.last_accepted_ns),
        'last_observed_ns':str(state.last_observed_ns),
        'quote_time_ns':str(state.last_quote.event_time_ns), 'evaluated_bars':state.evaluated_bars,
        'matched_entries':state.matched_entries, 'status':state.status, 'reason':state.reason,
        'execution_authority':'NONE'}


@exact
def advance_signals(state: PerpSignalState, raw: PerpSignalInput) -> PerpSignalTransition:
    if state.status != 'RUNNING':
        raise ValueError('La fuente de señales está detenida; requiere un inicio explícito nuevo')
    batch = PerpSignalInput.model_validate(raw.payload())
    collected, at = int(batch.collected_at_ns), int(batch.accepted_at_ns)
    if not state.last_accepted_ns <= collected <= at:
        raise ValueError('Recogida de señales anterior al estado conocido')
    quote = batch.quote.quote()
    _quote(quote, state.instrument, at)
    updated = replace(state, last_accepted_ns=at, sequence=state.sequence+1,
        head_hash=content_hash({'seed_hash':state.seed_hash, 'sequence':state.sequence+1,
            'previous':state.head_hash, 'input':batch.payload()}))

    def gap(reason):
        return PerpSignalTransition(replace(updated, status='GAP', reason=reason), outcome=reason)

    if (at-state.last_observed_ns > MAX_QUOTE_GAP_NS
            or quote.event_time_ns-state.last_quote.event_time_ns > MAX_QUOTE_GAP_NS):
        return gap('QUOTE_COVERAGE_GAP')
    if quote.event_time_ns <= state.last_quote.event_time_ns:
        if quote.event_time_ns == state.last_quote.event_time_ns and quote != state.last_quote:
            raise ValueError('Cotización contradictoria en el mismo instante')
        if batch.closed_candles:
            raise ValueError('Una cotización repetida o anterior no acredita velas nuevas')
        return PerpSignalTransition(updated, outcome='IGNORED_OLD_OR_DUPLICATE_QUOTE')

    bars = _candles(batch.closed_candles)
    step = INTERVALS[state.timeframe]
    if len(state.history)+len(bars) > MAX_HISTORY_BARS:
        return gap('HISTORY_CAPACITY')
    # Validate only the new suffix. The immutable prefix was validated at seed
    # or append; scanning/re-encoding 100k bars on every REST quote is avoided.
    validate_candle_values(list(bars), state.timeframe)
    previous = state.history[-1].t
    for bar in bars:
        if bar.t != previous+step or (bar.t+step)*10**9 > collected:
            raise ValueError('Velas de señales abiertas, repetidas, desordenadas o con huecos')
        previous = bar.t
    if any(at-(bar.t+step)*10**9 > MAX_SIGNAL_DELAY_NS for bar in bars):
        return gap('CLOSED_CANDLE_RECEIVED_LATE')
    if at-(previous+2*step)*10**9 > MAX_SIGNAL_DELAY_NS:
        return gap('MISSING_CLOSED_CANDLE')

    definition = json.loads(state.definition_json)
    history, history_hash = state.history, state.history_hash
    decisions = []
    matched_count = 0
    for bar in bars:
        history = (*history, bar)
        history_hash = _append_bar(history_hash, bar)
        rows = list(history)
        cache = IndicatorCache(rows)
        entry, explanation = evaluate(definition['entry'], rows, len(rows)-1, cache,
                                       base_timeframe=state.timeframe)
        exit_match, exit_explanation = (evaluate(definition['exit'], rows, len(rows)-1, cache,
            base_timeframe=state.timeframe) if 'exit' in definition else (False, None))
        decision = {'schema':'orbis.grvt.perp.signals.decision.v1', 'engine':SIGNAL_ENGINE,
            'seed_hash':state.seed_hash, 'input_sequence':updated.sequence, 'input_hash':updated.head_hash,
            'source_candle_t':bar.t, 'source_close_ns':str((bar.t+step)*10**9),
            'collected_at_ns':str(collected), 'known_at_ns':str(at),
            'entry_matched':entry, 'entry_explanation':explanation,
            'exit_matched':exit_match, 'exit_explanation':exit_explanation, 'execution_authority':'NONE'}
        decisions.append(encoded({**decision, 'sha256':content_hash(decision)}))
        matched_count += int(entry)
    return PerpSignalTransition(replace(updated, history=history, history_hash=history_hash,
        last_observed_ns=at, last_quote=quote, evaluated_bars=state.evaluated_bars+len(bars),
        matched_entries=state.matched_entries+matched_count), tuple(decisions))


def stop_signals(state: PerpSignalState, now_ns: int) -> PerpSignalState:
    if type(now_ns) is not int or not state.last_accepted_ns <= now_ns < 2**63:
        raise ValueError('Parada de señales anterior al estado conocido')
    return replace(state, last_accepted_ns=now_ns, status='STOPPED' if state.status=='RUNNING' else state.status)


def replay_signals(seed: PerpSignalSeed, inputs: list[PerpSignalInput]) -> PerpSignalTransition:
    state = seed.start()
    decisions = []
    for raw in inputs:
        result = advance_signals(state, raw)
        state = result.state
        decisions.extend(result.decisions_json)
    return PerpSignalTransition(state, tuple(decisions), outcome='REPLAYED_WITHOUT_AUTHORITY')


def record_signal_input(state: PerpSignalState, raw: PerpSignalInput) -> tuple[PerpSignalTransition, dict]:
    """Produce a bounded event for an encrypted grant journal, without SQL/I/O."""
    raw = PerpSignalInput.model_validate(raw.payload())
    result = advance_signals(state, raw)
    payload = {'schema':'orbis.grvt.perp.signals.record.v1', 'input':raw.payload(),
        'outcome':result.outcome, 'checkpoint':signal_checkpoint(result.state),
        'decisions':[json.loads(value) for value in result.decisions_json], 'execution_authority':'NONE'}
    return result, {**payload, 'sha256':content_hash(payload)}


def verify_signal_record(state: PerpSignalState, record: dict) -> PerpSignalTransition:
    """Replay calculations; a rehashed edited decision/checkpoint is insufficient."""
    if type(record) is not dict or set(record) != {
            'schema', 'input', 'outcome', 'checkpoint', 'decisions', 'execution_authority', 'sha256'}:
        raise ValueError('Registro de señales inválido')
    result, expected = record_signal_input(state, PerpSignalInput.model_validate(record['input']))
    if encoded(record) != encoded(expected):
        raise ValueError('Registro de señales no coincide con el replay causal')
    return result


def replay_signal_records(seed: PerpSignalSeed, records: list[dict]) -> PerpSignalTransition:
    state = seed.start()
    decisions = []
    for record in records:
        result = verify_signal_record(state, record)
        state = result.state
        decisions.extend(result.decisions_json)
    return PerpSignalTransition(state, tuple(decisions), outcome='REPLAYED_WITHOUT_AUTHORITY')
