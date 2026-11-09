"""Public GRVT REST observations for the shared PAPER book, without credentials.

Only actual received points are observed. HTTP happens outside archive/SQL locks.
An incomplete response fails the collection; no interpolated prices, zero funding
fallback, historical order or private exchange action is produced here.
"""
from __future__ import annotations

import json
import time

from app.derivatives.funding import FundingRecord,HOUR_NS
from app.derivatives.paper_math import InstrumentSpec,MarginRules,PerpQuote
from app.derivatives.perp_observed_origin import VerifiedObservedOrigin
from app.derivatives.perp_observed_types import (
    MAX_HISTORY_BARS, MAX_QUOTE_GAP_NS, MAX_SIGNAL_DELAY_NS, ObservedError, ObservedState, observed_version,
)
from app.derivatives.perp_observed_wire import (
    ObservedSeed,QuoteEvent,funding_input,quote_input,read_observed_seed,rules_input,specification_input,
)
from app.lab.engine import INTERVALS,number
from app.lab.perp_dataset import SYMBOLS,TradeBar
from app.marketdata.adapters.grvt import GrvtAdapter


def can_retry_grvt_candle(closes_at_ns: int, now_ns: int, *,
                          last_observed_ns: int, last_quote_ns: int) -> bool:
    deadline = min(closes_at_ns + MAX_SIGNAL_DELAY_NS, last_observed_ns + MAX_QUOTE_GAP_NS,
                   last_quote_ns + MAX_QUOTE_GAP_NS)
    return closes_at_ns <= now_ns < deadline


def _bar_key(bar: TradeBar):
    return bar.t,*(number(getattr(bar,key)) for key in ('o','h','l','c','v'))


class GrvtObservedSource:
    def __init__(self,*,adapter=None,clock_ns=time.time_ns):
        self.adapter=adapter if adapter is not None else GrvtAdapter()
        self.clock_ns=clock_ns
        self._metadata_cache: dict[str,tuple[MarginRules,InstrumentSpec]]={}

    def _instrument(self,symbol: str) -> str:
        if symbol not in SYMBOLS:raise ObservedError('Perpetuo observado no admitido')
        return symbol.replace('/','_').replace(':PERP','_Perp')

    def _metadata(self,symbol: str):
        instrument=self._instrument(symbol);now=self.clock_ns()
        cached=self._metadata_cache.get(symbol)
        if cached is None or any(not 0<=now-item.fetched_at_ns<30_000_000_000 for item in cached):
            raw_rules=self.adapter.get_margin_rules(symbol)
            rules=MarginRules.from_payload(raw_rules,instrument,self.clock_ns())
            raw_spec=self.adapter.get_instrument_spec(symbol)
            spec=InstrumentSpec.from_payload(raw_spec,instrument,self.clock_ns())
            cached=rules,spec
            # Preserve their actual fetch times, including after an HTTP error.
            self._metadata_cache[symbol]=cached
        return cached

    def _extend(self,symbol: str,timeframe: str,history: list[TradeBar],end: int):
        step=INTERVALS[timeframe]
        if len(history)<3:raise ObservedError('Falta el prefijo histórico de investigación')
        needed=(end-history[-1].t)//step-1
        if needed<0 or len(history)+needed>MAX_HISTORY_BARS:
            raise ObservedError('Continuación histórica fuera de la capacidad PAPER')
        result=list(history)
        while result[-1].t+step<end:
            # The provider's strict historical method requires >=3 bars. Two
            # overlap bars also prove continuity with the already-known prefix.
            start=result[-2].t;stop=min(end,start+5000*step)
            raw=self.adapter.get_historical_candles(symbol,timeframe,start,stop,now=self.clock_ns()//10**9)
            if type(raw) is not list:raise ObservedError('Velas públicas ausentes')
            rows=[TradeBar.model_validate(row) for row in raw]
            expected=list(range(start,stop,step))
            if [bar.t for bar in rows]!=expected:
                raise ObservedError('Velas públicas incompletas, duplicadas o fuera de rango')
            if any(_bar_key(old)!=_bar_key(new) for old,new in zip(result[-2:],rows[:2])):
                raise ObservedError('El histórico cerrado cambió respecto al prefijo conocido')
            result.extend(rows[2:])
        return result

    def _funding(self,symbol: str,through: int,anchor: FundingRecord|None):
        if anchor is not None and through<anchor.time_ns+anchor.interval_hours*HOUR_NS:
            return (anchor,)
        opened=anchor.time_ns+1 if anchor is not None else through-1
        rows=self.adapter.get_funding_history(symbol,opened,through)
        if type(rows) is not list or not rows or len(rows)>10_000:
            raise ObservedError('Financiación pública ausente o excesiva')
        instrument=self._instrument(symbol)
        records=tuple(sorted((FundingRecord.from_payload(row,instrument) for row in rows),key=lambda row:row.time_ns))
        if any(row.time_ns>through for row in records) or len({row.time_ns for row in records})!=len(records):
            raise ObservedError('Financiación pública futura o duplicada')
        if anchor is not None:
            records=tuple(row for row in records if row.time_ns>=anchor.time_ns)
            if not records or records[0]!=anchor:
                raise ObservedError('Cambió o falta el ancla de financiación ya consumida')
        return records

    def _point(self,symbol: str,anchor: FundingRecord|None):
        rules,spec=self._metadata(symbol)
        records=self._funding(symbol,self.clock_ns(),anchor)
        raw=self.adapter.get_ticker(symbol);received=self.clock_ns()
        quote=PerpQuote.from_ticker(raw,self._instrument(symbol),received)
        # If HTTP crossed a funding boundary, obtain that actual settlement and
        # a new quote. Never pay a missing settlement as zero.
        if received>=records[-1].time_ns+records[-1].interval_hours*HOUR_NS:
            records=self._funding(symbol,received,anchor)
            raw=self.adapter.get_ticker(symbol);received=self.clock_ns()
            quote=PerpQuote.from_ticker(raw,self._instrument(symbol),received)
        return quote,received,rules,spec,records

    def prepare(self,origin: VerifiedObservedOrigin) -> ObservedSeed:
        origin.check_session();reference=origin.reference()
        history=[TradeBar.model_validate(row) for row in json.loads(origin.trade_prefix_json)]
        for _ in range(3):
            end=self.clock_ns()//10**9//INTERVALS[reference.timeframe]*INTERVALS[reference.timeframe]
            history=self._extend(reference.symbol,reference.timeframe,history,end)
            quote,received,rules,spec,records=self._point(reference.symbol,None)
            if received//10**9//INTERVALS[reference.timeframe]*INTERVALS[reference.timeframe]!=end:
                continue
            version, engine = observed_version(reference.profile)
            seed=read_observed_seed({'schema':f'orbis.paper.perp.observed.seed.v{version}','engine':engine,
                'profile':reference.profile.payload(),'definition':json.loads(origin.definition_json),
                'timeframe':reference.timeframe,'received_at_ns':str(received),'quote':quote_input(quote).payload(),
                'rules':rules_input(rules).model_dump(mode='json'),'specification':specification_input(spec).payload(),
                'history':[bar.model_dump() for bar in history],
                'funding':[funding_input(row).model_dump() for row in records]})
            seed.start();origin.check_session()
            return seed
        raise ObservedError('La preparación no alcanzó una última vela cerrada estable')

    def poll(self,state: ObservedState) -> QuoteEvent:
        instrument=state.account.markets[0].instrument
        symbol=instrument.replace('_USDT_Perp','/USDT:PERP')
        # Only the overlapping tail is needed for a new collection. Do not
        # reparse a growing 100k-bar history on every quote.
        history=[TradeBar.model_validate(bar.json()) for bar in state.history[-3:]]
        step=INTERVALS[state.timeframe];end=self.clock_ns()//10**9//step*step
        combined=self._extend(symbol,state.timeframe,history,end)
        quote,received,rules,spec,records=self._point(symbol,state.funding_anchor)
        # Crossing a candle boundary during other requests requires one last
        # fetch and a quote known after it. A second crossing fails in the core.
        latest=received//10**9//step*step
        if latest!=end:
            combined=self._extend(symbol,state.timeframe,combined,latest)
            quote,received,rules,spec,records=self._point(symbol,state.funding_anchor)
        if len(state.history)+len(combined)-len(history)>MAX_HISTORY_BARS:
            raise ObservedError('Continuación histórica fuera de la capacidad PAPER')
        event=QuoteEvent.model_validate({'schema':'orbis.paper.perp.observed.input.v1','kind':'OBSERVATION',
            'received_at_ns':str(received),'quote':quote_input(quote).payload(),'rules':rules_input(rules).model_dump(mode='json'),
            'specification':specification_input(spec).payload(),
            'funding':[funding_input(row).model_dump() for row in records],
            'closed_candles':[bar.model_dump() for bar in combined[len(history):]]})
        return event
