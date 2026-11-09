"""Pure current-observation consumer; no HTTP, storage, workers or live authority.

Unlike OHLC research this consumer never interpolates between observations or
closes at an artificial end. Bid/ask fills happen at verified receipt time;
provider timestamps and unavailable intervals remain separate evidence.
"""
from __future__ import annotations

from dataclasses import replace
import json
from typing import Literal

from app.derivatives.funding import FundingRecord, HOUR_NS
from app.derivatives.paper_math import InstrumentSpec, MarginRules, PerpQuote
from app.derivatives.perp_observed_types import (
    MAX_HISTORY_BARS, MAX_QUOTE_AGE_NS, MAX_QUOTE_GAP_NS, MAX_SIGNAL_DELAY_NS,
    Observation, ObservedError, ObservedState, ObservedTransition, OpenPosition,
)
from app.lab.coverage import validate_paper_timeframe_capacity
from app.lab.engine import (
    Candle, IndicatorCache, INTERVALS, evaluate, number, supports_engine_version,
    validate_candle_values, validate_strategy,
)
from app.lab.perp_account import (
    cancel_order, fill_order, mark_all, mark_with_rules, new_account, settle_funding, submit_order,
)
from app.lab.perp_book_types import Cursor, D, Market, Transition, exact, value
from app.lab.perp_execution_math import adverse_price, opening_size
from app.lab.perp_profile import ExecutionProfile, PerpLimitProfile, PerpPartialProfile, read_profile, validate_partial_targets
from app.lab.store import content_hash, encoded


def _stamp(stamp: int) -> int:
    if type(stamp) is not int or not 0 < stamp < 10**20:
        raise ObservedError('Instante observado no válido')
    return stamp


def _quote(quote: PerpQuote, instrument: str, received: int) -> None:
    _stamp(received)
    if (not isinstance(quote,PerpQuote) or quote.instrument!=instrument
            or not 0<=received-quote.event_time_ns<=MAX_QUOTE_AGE_NS):
        raise ObservedError('Cotización futura, antigua o de otro instrumento')


def _metadata(rules: MarginRules, spec: InstrumentSpec, instrument: str, received: int) -> None:
    if not isinstance(rules,MarginRules) or not isinstance(spec,InstrumentSpec):
        raise ObservedError('Faltan reglas o especificación actuales')
    if rules.fetched_at_ns>received or spec.fetched_at_ns>received:
        raise ObservedError('Metadatos todavía no conocidos')
    if (MarginRules.from_payload(rules.json(),instrument,received)!=rules
            or spec.instrument!=instrument or not 0<=received-spec.fetched_at_ns<=60_000_000_000):
        raise ObservedError('Metadatos no verificables o antiguos')


def _funding(records: tuple[FundingRecord,...], instrument: str, through: int) -> tuple[FundingRecord,...]:
    if type(records) is not tuple or not records:
        raise ObservedError('Funding sin ancla ni cobertura')
    previous=None
    for row in records:
        if (not isinstance(row,FundingRecord) or row.instrument!=instrument
                or type(row.time_ns) is not int or not 0<row.time_ns<=through
                or type(row.interval_hours) is not int or row.interval_hours not in (1,4,8)):
            raise ObservedError('Registro de funding no válido')
        value(row.rate_percent);value(row.mark,positive=True)
        if abs(row.rate_percent)>100:
            raise ObservedError('Funding fuera de rango')
        if previous is not None and row.time_ns-previous.time_ns!=row.interval_hours*HOUR_NS:
            raise ObservedError('Funding duplicado, desordenado o con huecos')
        previous=row
    if through>=records[-1].time_ns+records[-1].interval_hours*HOUR_NS:
        raise ObservedError('Falta una liquidación esperada de funding')
    return records


def _history(bars: tuple[Candle,...], timeframe: str, through: int) -> None:
    if type(bars) is not tuple or not bars or len(bars)>MAX_HISTORY_BARS:
        raise ObservedError('Histórico observado vacío o excesivo')
    validate_candle_values(list(bars),timeframe)
    step=INTERVALS[timeframe]
    if (any(b.t-a.t!=step for a,b in zip(bars,bars[1:]))
            or (bars[-1].t+step)*10**9>through):
        raise ObservedError('Velas con huecos o todavía abiertas')


@exact
def start_observed(profile: ExecutionProfile, definition: dict, timeframe: str,
                   market: Market, seed: tuple[Candle,...], quote: PerpQuote,
                   received_at_ns: int, funding: tuple[FundingRecord,...]) -> ObservedTransition:
    definition=validate_strategy(definition);profile=read_profile(profile.payload())
    validate_partial_targets(profile,definition)
    if (not supports_engine_version(profile.signal_engine,definition)
            or content_hash(definition)!=profile.strategy_hash
            or market.mode!=profile.margin_mode or market.leverage!=number(profile.leverage)
            or market.fee_rate!=number(definition['fee_rate'])):
        raise ObservedError('Perfil, estrategia y mercado no coinciden')
    _quote(quote,market.instrument,received_at_ns)
    _metadata(market.rules,market.specification,market.instrument,received_at_ns)
    _history(seed,timeframe,received_at_ns)
    step=INTERVALS[timeframe]
    if seed[-1].t != (received_at_ns//10**9//step-1)*step:
        raise ObservedError('El seed no termina en la última vela cerrada')
    validate_paper_timeframe_capacity(definition,timeframe,seed[0].t,max_base_bars=MAX_HISTORY_BARS)
    records=_funding(funding,market.instrument,received_at_ns)
    transition=mark_all(new_account(number(definition['initial_capital']),(market,)),
        Cursor(received_at_ns,0),{market.instrument:quote.mark})
    state=ObservedState(profile,encoded(definition),timeframe,transition.account,seed,
        records[-1],received_at_ns,received_at_ns,quote)
    return ObservedTransition(state,transition.entries,events_json=(encoded({
        'kind':'STARTED_FICTITIOUS','received_at_ns':str(received_at_ns),
        'quote_time_ns':str(quote.event_time_ns),'seed_trades':False,'execution_authority':'NONE'}),))


def _control(state: ObservedState, now_ns: int) -> None:
    _stamp(now_ns)
    if now_ns<max(state.last_received_ns,state.last_control_ns):
        raise ObservedError('Control anterior al estado conocido')


def cancel_resting_limit(state: ObservedState, book_time_ns: int, reason: Literal[
        'USER_CANCELLED', 'TIME_IN_FORCE_EXPIRED', 'OBSERVATION_INTERRUPTED'],
        *, received_at_ns: int) -> ObservedTransition:
    resting = state.resting
    if resting is None:
        return ObservedTransition(state)
    cursor = state.account.cursor
    ordinal = cursor.ordinal + 1 if cursor is not None and cursor.time_ns == book_time_ns else 0
    cancelled = cancel_order(state.account, Cursor(book_time_ns, ordinal), resting.order_id, reason=reason)
    event = encoded({'kind': 'LIMIT_CANCELLED', 'order_id': resting.order_id,
                     'book_time_ns': str(book_time_ns), 'received_at_ns': str(received_at_ns),
                     'reason': reason})
    return ObservedTransition(replace(state, account=cancelled.account, resting=None),
                              cancelled.entries, events_json=(event,))


@exact
def stop_observed(state: ObservedState, now_ns: int) -> ObservedTransition:
    _control(state,now_ns)
    status=('CLOSE_ONLY' if state.opened is not None else 'STOPPED')
    if state.status in ('GAP','HALTED','LIQUIDATED'):
        status=state.status
    stopped = cancel_resting_limit(state, now_ns, 'USER_CANCELLED', received_at_ns=now_ns)
    updated=replace(stopped.state,status=status,last_control_ns=now_ns)
    return replace(stopped, state=updated, events_json=(*stopped.events_json, encoded({'kind':'STOP_REQUESTED',
        'received_at_ns':str(now_ns),'status':status,'position_closed':False})))


@exact
def request_observed_close(state: ObservedState, now_ns: int) -> ObservedTransition:
    _control(state,now_ns)
    if state.opened is None or state.status not in ('RUNNING','CLOSE_ONLY'):
        raise ObservedError('No hay posición observada con cobertura para solicitar cierre')
    return ObservedTransition(replace(state,status='CLOSE_ONLY',close_requested=True,last_control_ns=now_ns),
        events_json=(encoded({'kind':'CLOSE_REQUESTED','received_at_ns':str(now_ns),
                              'fill_created':False}),))


def observed_gap(state: ObservedState, now_ns: int, reason: str) -> ObservedTransition:
    _control(state,now_ns)
    if type(reason) is not str or not reason or len(reason)>160:
        raise ObservedError('Motivo de hueco inválido')
    interrupted = cancel_resting_limit(state, now_ns, 'OBSERVATION_INTERRUPTED', received_at_ns=now_ns)
    return replace(interrupted, state=replace(interrupted.state,status='GAP',reason=reason,last_control_ns=now_ns),
        events_json=(*interrupted.events_json, encoded({'kind':'OBSERVATION_GAP','from_ns':str(state.last_received_ns),
            'through_ns':str(now_ns),'reason':reason,'reconstructed':False,'fill_created':False})))


class _Observed:
    position_type: type[OpenPosition] = OpenPosition

    def __init__(self,state: ObservedState,batch: Observation):
        self.state,self.batch=state,batch
        self.definition=json.loads(state.definition_json)
        self.instrument=state.account.markets[0].instrument
        self.long=self.definition['direction']=='LONG'
        self.journal=[];self.signals=[];self.trades=[];self.events=[]

    def cursor(self,stamp: int) -> Cursor:
        old=self.state.account.cursor
        return Cursor(stamp,old.ordinal+1 if old is not None and old.time_ns==stamp else 0)

    def record(self,transition: Transition) -> None:
        self.state=replace(self.state,account=transition.account)
        self.journal.extend(transition.entries)
        if any(e.kind in ('ISOLATED_LIQUIDATION','CROSS_ACCOUNT_LIQUIDATION') for e in transition.entries):
            if self.state.opened is not None:self.finish('LIQUIDATION',None)
            self.state=replace(self.state,status='LIQUIDATED',reason='OBSERVED_MAINTENANCE_BREACH')

    def finish(self,reason: str,price) -> None:
        opened=self.state.opened;account=self.state.account
        assert opened is not None and account.cursor is not None
        self.trades.append(encoded({'source_candle_t':opened.source_candle_t,
            'signal_known_at_ns':str(opened.signal_known_at_ns),'entry_time_ns':str(opened.entry_time_ns),
            'entry_quote_time_ns':str(opened.entry_quote_time_ns),'entry_price':format(opened.entry_price,'f'),
            'quantity':format(opened.quantity,'f'),'direction':self.definition['direction'],
            'exit_time_ns':str(account.cursor.time_ns),'exit_known_at_ns':str(self.batch.received_at_ns),
            'exit_price':format(price,'f') if price is not None else None,
            'exit_reason':reason,'net_profit':format(account.cash-opened.cash_before,'f'),
            'realized_pnl':format(account.realized_pnl-opened.realized_before,'f'),
            'fees':format(account.fees_paid-opened.fees_before,'f'),
            'funding_paid':format(account.funding_paid-opened.funding_before,'f'),
            'insurance_transfer':format(account.insurance_transferred-opened.insurance_before,'f'),
            'insurance_cover':format(account.insurance_covered-opened.covered_before,'f')}))
        self.state=replace(self.state,opened=None,close_requested=False,closed_trades=self.state.closed_trades+1,
            status='STOPPED' if self.state.status=='CLOSE_ONLY' else self.state.status)

    def execution_price(self, *, buying: bool) -> D:
        quote=self.batch.quote
        market=self.state.account.market(self.instrument)
        return adverse_price(quote.ask if buying else quote.bid,buying,
                             number(self.definition['slippage_rate']),market.specification.tick_size)

    def execute(self,size,*,opening: bool,reason: str):
        quote=self.batch.quote;buying=self.long if opening else not self.long
        price=self.execution_price(buying=buying)
        if price<=0:
            self.state=replace(self.state,status='HALTED',reason='EXECUTION_PRICE_BELOW_TICK')
            return None
        count=self.state.order_count+1;identifier='observed-'+str(count)
        self.state=replace(self.state,order_count=count)
        self.events.append(encoded({'kind':'ORDER_INTENT','order_id':identifier,'reason':reason,
            'side':'BUY' if buying else 'SELL','quantity':format(size,'f'),'price_bound':format(price,'f'),
            'received_at_ns':str(self.batch.received_at_ns),'quote_time_ns':str(quote.event_time_ns)}))
        self.record(submit_order(self.state.account,self.cursor(self.batch.received_at_ns),identifier=identifier,
            instrument=self.instrument,side='BUY' if buying else 'SELL',quantity=size,
            price_bound=price,reduce_only=not opening))
        order=self.state.account.order(identifier)
        if order.active:
            self.record(fill_order(self.state.account,self.cursor(self.batch.received_at_ns),identifier,quantity=size,price=price))
        if self.state.account.order(identifier).status!='FILLED':
            self.events.append(encoded({'kind':'ENTRY_REJECTED' if opening else 'EXIT_REJECTED',
                'reason':self.state.account.order(identifier).reason,'order_id':identifier}))
            if not opening:self.state=replace(self.state,status='HALTED',reason='EXIT_NOT_EXECUTABLE')
            return None
        return price

    def exit(self,reason: str) -> None:
        position=self.state.account.position(self.instrument)
        if position is None:return
        price=self.execute(abs(position.size),opening=False,reason=reason)
        if price is not None and self.state.opened is not None:self.finish(reason,price)

    def opening_position(self, source_candle_t: int, known_at_ns: int, price: D, size: D) -> OpenPosition:
        before = self.state.account
        return self.position_type(source_candle_t, known_at_ns, self.batch.received_at_ns,
            self.batch.quote.event_time_ns, price, size, len(self.state.history),
            before.cash, before.realized_pnl, before.fees_paid, before.funding_paid,
            before.insurance_transferred, before.insurance_covered, price)

    def enter(self,signal: dict) -> None:
        before=self.state.account;market=before.market(self.instrument);quote=self.batch.quote
        price=adverse_price(quote.ask if self.long else quote.bid,self.long,
                            number(self.definition['slippage_rate']),market.specification.tick_size)
        if price<=0:
            signal['action']='SKIPPED_PRICE_BELOW_TICK';return
        size=opening_size(before.cash,number(self.definition['allocation']),market,price,quote.mark,long=self.long)
        if not size:
            signal['action']='SKIPPED_SIZE_OR_MARGIN';return
        opened=self.opening_position(signal['source_candle_t'],self.batch.received_at_ns,price,size)
        self.state=replace(self.state,opened=opened)
        if self.execute(size,opening=True,reason='ENTRY_SIGNAL') is None:
            self.state=replace(self.state,opened=None);signal['action']='ENTRY_REJECTED'
        else:
            signal['action']='ENTERED_OBSERVED_QUOTE'
            signal['entry_time_ns']=str(self.batch.received_at_ns);signal['entry_price']=format(price,'f')

    def protection_levels(self, opened: OpenPosition) -> tuple[D,D]:
        sign=D(1) if self.long else D(-1)
        stop=opened.entry_price*(D(1)-sign*number(self.definition['stop_loss']))
        target=opened.entry_price*(D(1)+sign*number(self.definition['take_profit']))
        if self.definition.get('trailing_stop') is not None:
            trailing=opened.trailing*(D(1)-sign*number(self.definition['trailing_stop']))
            stop=max(stop,trailing) if self.long else min(stop,trailing)
        return stop,target

    def trigger(self) -> None:
        opened=self.state.opened
        if opened is None or self.state.status not in ('RUNNING','CLOSE_ONLY'):return
        if self.state.close_requested:
            self.exit('MANUAL_CLOSE_OBSERVED');return
        stop,target=self.protection_levels(opened)
        price=self.batch.quote.bid if self.long else self.batch.quote.ask
        stop_hit=price<=stop if self.long else price>=stop
        target_hit=price>=target if self.long else price<=target
        if stop_hit:self.exit('STOP_OR_TRAILING')
        elif target_hit:self.exit('TAKE_PROFIT')

    def before_mark(self, stamp: int) -> None:
        """Observe order time limits before applying the next market input."""

    def match_resting(self) -> None:
        """Evaluate resting entries against the accepted quote."""

    def run(self) -> ObservedTransition:
        received=self.batch.received_at_ns
        records=_funding(self.batch.funding,self.instrument,received)
        anchor_index=next((i for i,r in enumerate(records) if r.time_ns==self.state.funding_anchor.time_ns),None)
        if anchor_index is None or records[anchor_index]!=self.state.funding_anchor:
            raise ObservedError('El ancla de funding consumida cambió o falta')
        for row in records[anchor_index+1:]:
            if row.time_ns<=self.state.last_received_ns:
                raise ObservedError('Nueva financiación retroactiva al periodo ya consumido')
            self.before_mark(row.time_ns)
            if row.time_ns==received:
                self.record(mark_with_rules(self.state.account,self.cursor(row.time_ns),
                    {self.instrument:row.mark},{self.instrument:self.batch.rules}))
            else:
                self.record(mark_all(self.state.account,self.cursor(row.time_ns),{self.instrument:row.mark}))
            self.record(settle_funding(self.state.account,self.cursor(row.time_ns),instrument=self.instrument,
                rate_percent=row.rate_percent,settlement_mark=row.mark))
        self.state=replace(self.state,funding_anchor=records[-1])
        self.before_mark(received)
        self.record(mark_with_rules(self.state.account,self.cursor(received),
            {self.instrument:self.batch.quote.mark},{self.instrument:self.batch.rules}))
        signal=None;exit_signal=False
        history=self.state.history
        for candle in self.batch.closed_candles:
            history=(*history,candle);self.state=replace(self.state,history=history)
            cache=IndicatorCache(list(history))
            matched,explanation=evaluate(self.definition['entry'],list(history),len(history)-1,cache,base_timeframe=self.state.timeframe)
            if self.state.opened is not None:
                opened=self.state.opened
                if candle.t*10**9>=opened.entry_time_ns:
                    trail=max(opened.trailing,candle.h) if self.long else min(opened.trailing,candle.l)
                    self.state=replace(self.state,opened=replace(opened,trailing=trail))
                else:
                    self.events.append(encoded({'kind':'PARTIAL_ENTRY_BAR_NOT_USED_FOR_TRAILING',
                        'source_candle_t':candle.t,'known_at_ns':str(received)}))
                if self.definition.get('exit') is not None:
                    exit_signal,explanation_exit=evaluate(self.definition['exit'],list(history),len(history)-1,cache,base_timeframe=self.state.timeframe)
                    if exit_signal:self.events.append(encoded({'kind':'EXIT_SIGNAL','source_candle_t':candle.t,
                        'known_at_ns':str(received),'explanation':explanation_exit}))
            if matched:
                signal={'source_candle_t':candle.t,'known_at_ns':str(received),
                    'source_close_ns':str((candle.t+INTERVALS[self.state.timeframe])*10**9),'explanation':explanation,
                    'action':'SKIPPED_POSITION_OPEN' if self.state.opened is not None else
                             'PLANNED_OBSERVED_QUOTE' if self.state.status=='RUNNING' else 'SKIPPED_'+self.state.status}
        if self.state.opened is not None and self.state.status in ('RUNNING','CLOSE_ONLY'):
            holding=self.definition.get('max_holding_bars')
            if self.state.close_requested:self.exit('MANUAL_CLOSE_OBSERVED')
            elif exit_signal:self.exit('EXIT_RULE_OBSERVED_QUOTE')
            elif holding is not None and len(history)-self.state.opened.entry_bar_index>=holding:self.exit('TIME_LIMIT')
            else:self.trigger()
        if signal is not None:
            if signal['action']=='PLANNED_OBSERVED_QUOTE' and self.state.status=='RUNNING':self.enter(signal)
            self.signals.append(encoded(signal))
        self.match_resting()
        self.trigger()  # A wide spread can trigger protection immediately after entry.
        self.state=replace(self.state,last_received_ns=received,last_quote=self.batch.quote)
        return ObservedTransition(self.state,tuple(self.journal),tuple(self.signals),tuple(self.trades),tuple(self.events))


@exact
def observe(state: ObservedState,batch: Observation) -> ObservedTransition:
    if not isinstance(batch,Observation):raise ObservedError('Lote de observaciones requerido')
    if state.status not in ('RUNNING','CLOSE_ONLY'):
        return ObservedTransition(state)
    _stamp(batch.received_at_ns)
    if batch.received_at_ns<max(state.last_received_ns,state.last_control_ns):
        raise ObservedError('Recepción anterior al estado/control conocido')
    instrument=state.account.markets[0].instrument
    _quote(batch.quote,instrument,batch.received_at_ns)
    if batch.quote.event_time_ns<=state.last_quote.event_time_ns:
        if batch.quote.event_time_ns==state.last_quote.event_time_ns and batch.quote!=state.last_quote:
            raise ObservedError('Cotizaciones contradictorias en el mismo instante')
        return ObservedTransition(state,events_json=(encoded({'kind':'IGNORED_OLD_OR_DUPLICATE_QUOTE',
            'quote_time_ns':str(batch.quote.event_time_ns),'received_at_ns':str(batch.received_at_ns)}),))
    if (batch.received_at_ns-state.last_received_ns>MAX_QUOTE_GAP_NS
            or batch.quote.event_time_ns-state.last_quote.event_time_ns>MAX_QUOTE_GAP_NS):
        return observed_gap(state,batch.received_at_ns,'QUOTE_COVERAGE_GAP')
    _metadata(batch.rules,batch.specification,instrument,batch.received_at_ns)
    previous_rules=state.account.markets[0].rules
    if (batch.rules.fetched_at_ns<previous_rules.fetched_at_ns
            or batch.rules.fetched_at_ns==previous_rules.fetched_at_ns and batch.rules!=previous_rules):
        raise ObservedError('Reglas anteriores o contradictorias respecto a las ya conocidas')
    old_spec=state.account.markets[0].specification
    if replace(batch.specification,fetched_at_ns=old_spec.fetched_at_ns)!=old_spec:
        return observed_gap(state,batch.received_at_ns,'INSTRUMENT_SPECIFICATION_CHANGED')
    if type(batch.closed_candles) is not tuple:
        raise ObservedError('Las velas observadas deben ser una secuencia inmutable')
    combined=(*state.history,*batch.closed_candles)
    _history(combined,state.timeframe,batch.received_at_ns)
    step=INTERVALS[state.timeframe]
    if batch.closed_candles and any(batch.received_at_ns-(bar.t+step)*10**9>MAX_SIGNAL_DELAY_NS for bar in batch.closed_candles):
        return observed_gap(state,batch.received_at_ns,'CLOSED_CANDLE_RECEIVED_LATE')
    if batch.received_at_ns-(combined[-1].t+2*step)*10**9>MAX_SIGNAL_DELAY_NS:
        return observed_gap(state,batch.received_at_ns,'MISSING_CLOSED_CANDLE')
    if isinstance(state.profile, PerpPartialProfile):
        from app.derivatives.perp_observed_partial import PartialLimitObserved, PartialObserved
        consumer = PartialLimitObserved if state.profile.entry_order is not None else PartialObserved
        return consumer(state,batch).run()
    if isinstance(state.profile, PerpLimitProfile):
        from app.derivatives.perp_observed_limit import LimitObserved
        return LimitObserved(state, batch).run()
    return _Observed(state,batch).run()
