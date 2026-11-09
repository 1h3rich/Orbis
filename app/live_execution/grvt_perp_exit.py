"""Exact bid/ask exit decisions over a verified REAL feed and actual fills.

No order, account, key or historical fill is synthesized here. Price comparisons
use notionals rather than a rounded VWAP. A CLOSE is evidence, never execution
authority; once observed it remains pending until the coordinator reconciles.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from decimal import Decimal
import json
from typing import Literal

from app.lab.engine import IndicatorCache, INTERVALS, evaluate, number, validate_strategy
from app.lab.perp_book_types import exact
from app.lab.store import content_hash
from app.live_execution.grvt_perp_signals import MAX_CONSUMER_DELAY_NS, PerpSignalState


EXIT_ENGINE = 'grvt-perp-exit-1.0'
Reason = Literal['AWAITING_CONFIRMED_QUOTE','POSITION_OPEN','EXIT_RULE','TIME_LIMIT',
    'STOP_OR_TRAILING','TAKE_PROFIT','DATA_GAP','DATA_STALE']


def _stamp(at: int) -> None:
    if type(at) is not int or not 0 < at < 2**63:
        raise ValueError('Instante de salida perpetua inválido')


@dataclass(frozen=True)
class PerpExitCursor:
    seed_hash: str
    definition_hash: str
    instrument: str
    timeframe: str
    size: Decimal
    entry_notional: Decimal
    last_fill_ns: int
    confirmed_at_ns: int
    trailing_notional: Decimal
    last_candle_t: int
    last_feed_sequence: int = -1
    last_quote_ns: int = 0
    action: Literal['HOLD','CLOSE'] = 'HOLD'
    reason: Reason = 'AWAITING_CONFIRMED_QUOTE'
    known_at_ns: int | None = None
    trigger_quote_ns: int | None = None
    trigger_price: Decimal | None = None
    condition_candle_t: int | None = None
    exit_rule_evidence_json: str | None = None
    holding_bars: int = 0


@exact
def start_perp_exit(feed: PerpSignalState, *, size: Decimal, entry_notional: Decimal,
                    last_fill_ns: int, confirmed_at_ns: int) -> PerpExitCursor:
    _stamp(last_fill_ns)
    _stamp(confirmed_at_ns)
    if (confirmed_at_ns < last_fill_ns or last_fill_ns < feed.started_at_ns or feed.timeframe not in INTERVALS
            or any(type(value) is not Decimal or not value.is_finite() or value <= 0
                   for value in (size,entry_notional))):
        raise ValueError('Entrada real no verificable para observar salidas')
    definition = validate_strategy(json.loads(feed.definition_json))
    if definition['direction']=='SHORT' and number(definition['take_profit'])>=1:
        raise ValueError('Objetivo SHORT real no alcanzable')
    step = INTERVALS[feed.timeframe]
    return PerpExitCursor(feed.seed_hash,content_hash(definition),feed.instrument,feed.timeframe,
        size,entry_notional,last_fill_ns,confirmed_at_ns,entry_notional,
        last_fill_ns//10**9//step*step-step)


@exact
def observe_perp_exit(cursor: PerpExitCursor, feed: PerpSignalState, *, now_ns: int) -> PerpExitCursor:
    _stamp(now_ns)
    definition = validate_strategy(json.loads(feed.definition_json))
    if (cursor.seed_hash != feed.seed_hash or cursor.definition_hash != content_hash(definition)
            or (cursor.instrument,cursor.timeframe) != (feed.instrument,feed.timeframe)
            or feed.sequence < cursor.last_feed_sequence or now_ns < cursor.confirmed_at_ns
            or now_ns < feed.last_accepted_ns
            or cursor.known_at_ns is not None and now_ns < cursor.known_at_ns):
        raise ValueError('Fuente, estrategia o tiempo de salida perpetua cambiados')
    if cursor.action == 'CLOSE':
        return cursor

    def close(reason: Reason, *, quote=None, condition=None, evidence=None):
        return replace(cursor,action='CLOSE',reason=reason,known_at_ns=now_ns,
            last_feed_sequence=feed.sequence,trigger_quote_ns=quote.event_time_ns if quote is not None else None,
            trigger_price=(quote.bid if definition['direction']=='LONG' else quote.ask) if quote is not None else None,
            condition_candle_t=condition,exit_rule_evidence_json=json.dumps(evidence,sort_keys=True,separators=(',',':'))
                if evidence is not None else cursor.exit_rule_evidence_json)

    if feed.status != 'RUNNING':
        return close('DATA_GAP')
    if (not 0 <= now_ns-feed.last_observed_ns <= MAX_CONSUMER_DELAY_NS
            or not 0 <= now_ns-feed.last_quote.event_time_ns <= MAX_CONSUMER_DELAY_NS):
        return close('DATA_STALE')
    quote = feed.last_quote
    if quote.instrument != cursor.instrument:
        raise ValueError('Cotización de salida de otro instrumento')
    # Public data received before fills were confirmed is not called live
    # software protection. The native LAST brackets cover that different scope.
    if feed.last_observed_ns < cursor.confirmed_at_ns or quote.event_time_ns < cursor.last_fill_ns:
        return replace(cursor,last_feed_sequence=feed.sequence)
    if feed.sequence == cursor.last_feed_sequence and quote.event_time_ns <= cursor.last_quote_ns:
        return cursor

    step = INTERVALS[feed.timeframe]
    long = definition['direction']=='LONG'
    sign = 1 if long else -1
    first = bisect_right(feed.history,cursor.last_candle_t,key=lambda bar:bar.t)
    indices = range(first,len(feed.history))
    rows = None
    cache = None
    rule_match = None
    extreme = cursor.trailing_notional
    holding = cursor.holding_bars
    last_t = cursor.last_candle_t
    for index in indices:
        bar = feed.history[index]
        if (bar.t+step)*10**9 <= cursor.last_fill_ns:
            continue
        if (bar.t+step)*10**9 > feed.last_observed_ns:
            raise ValueError('Salida usando una vela todavía abierta')
        last_t = bar.t
        holding += 1
        if bar.t*10**9 >= cursor.last_fill_ns:
            candidate = (bar.h if long else bar.l)*cursor.size
            extreme = max(extreme,candidate) if long else min(extreme,candidate)
        if 'exit' in definition and rule_match is None:
            if rows is None:
                rows = list(feed.history)
                cache = IndicatorCache(rows)
            matched,evidence = evaluate(definition['exit'],rows,index,cache,base_timeframe=feed.timeframe)
            if matched:
                rule_match = (bar.t,evidence)
    cursor = replace(cursor,trailing_notional=extreme,last_candle_t=last_t,holding_bars=holding,
        last_feed_sequence=feed.sequence,last_quote_ns=quote.event_time_ns,reason='POSITION_OPEN')

    # PAPER's order at a newly observed close is exit rule, holding count,
    # then stop/trailing/target. Native fills may already have closed remotely;
    # the coordinator must inspect those before interpreting this evidence.
    if rule_match is not None:
        return close('EXIT_RULE',quote=quote,condition=rule_match[0],evidence=rule_match[1])
    if definition.get('max_holding_bars') is not None and holding >= definition['max_holding_bars']:
        return close('TIME_LIMIT',quote=quote,condition=last_t)
    stop = cursor.entry_notional*(1-sign*number(definition['stop_loss']))
    target = cursor.entry_notional*(1+sign*number(definition['take_profit']))
    if 'trailing_stop' in definition:
        trailing = extreme*(1-sign*number(definition['trailing_stop']))
        stop = max(stop,trailing) if long else min(stop,trailing)
    current = (quote.bid if long else quote.ask)*cursor.size
    if current <= stop if long else current >= stop:
        return close('STOP_OR_TRAILING',quote=quote)
    if current >= target if long else current <= target:
        return close('TAKE_PROFIT',quote=quote)
    return cursor


def perp_exit_checkpoint(cursor: PerpExitCursor) -> dict:
    return {'schema':'orbis.grvt.perp.exit.checkpoint.v1','engine':EXIT_ENGINE,
        'seed_hash':cursor.seed_hash,'definition_hash':cursor.definition_hash,'instrument':cursor.instrument,
        'timeframe':cursor.timeframe,'size':format(cursor.size,'f'),'entry_notional':format(cursor.entry_notional,'f'),
        'last_fill_ns':str(cursor.last_fill_ns),'confirmed_at_ns':str(cursor.confirmed_at_ns),
        'trailing_notional':format(cursor.trailing_notional,'f'),'last_candle_t':cursor.last_candle_t,
        'last_feed_sequence':cursor.last_feed_sequence,'last_quote_ns':str(cursor.last_quote_ns),
        'action':cursor.action,'reason':cursor.reason,'known_at_ns':str(cursor.known_at_ns) if cursor.known_at_ns else None,
        'trigger_quote_ns':str(cursor.trigger_quote_ns) if cursor.trigger_quote_ns else None,
        'trigger_price':format(cursor.trigger_price,'f') if cursor.trigger_price is not None else None,
        'condition_candle_t':cursor.condition_candle_t,'exit_rule_evidence':json.loads(cursor.exit_rule_evidence_json)
            if cursor.exit_rule_evidence_json is not None else None,
        'holding_bars':cursor.holding_bars,'execution_authority':'NONE'}
