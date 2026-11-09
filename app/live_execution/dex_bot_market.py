"""Public MEXC inputs for the DEX worker; no balances, credentials or orders."""
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation

from app.lab.engine import Candle,INTERVALS,validate_candles
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexReferenceQuote
from app.marketdata.adapters.mexc import MEXC_HISTORICAL_PAGE_SIZE
from app.portfolio.wallet_funding import _time


@dataclass(frozen=True,slots=True)
class MarketAdvance:
    candles:list[Candle]
    ready:bool


def market_overlap(definition:dict)->int:
    return definition.get('volume_gate',{}).get('period',2)


def reference_quote(raw:object,*,observed_at:datetime)->DexReferenceQuote:
    observed_at=_time(observed_at)
    try:
        if not isinstance(raw,dict) or raw['symbol']!='BNB/USDT' or raw['source']!='mexc-spot-public':raise ValueError('market')
        ns=raw['event_time_ns']
        if type(ns) is not int or not 0<ns<2**63:raise ValueError('event time')
        event=datetime.fromtimestamp(ns//10**9,timezone.utc)+timedelta(microseconds=ns%10**9//1000)
        values=[]
        for key in ('bid','ask'):
            text=raw[key]
            if type(text) is not str or not 1<=len(text)<=100:raise ValueError('price format')
            value=Decimal(text)
            if not value.is_finite() or value<=0 or abs(value.as_tuple().exponent)>100:raise ValueError('price')
            values.append(value)
        bid,ask=values
        if ask<=bid or not 0<=(observed_at-event).total_seconds()<=15:raise ValueError('quote age/spread')
        return DexReferenceQuote(bid,ask,event,observed_at)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,InvalidOperation):
        raise DexExecutorError('Referencia MEXC no disponible, caducada o de otro mercado') from None


def advance_market(history:list[Candle],raw:object,*,timeframe:str,now:datetime,overlap_count:int=2)->MarketAdvance:
    now=_time(now)
    try:
        if timeframe not in INTERVALS or type(overlap_count) is not int or not 2<=overlap_count<=120 or len(history)<=overlap_count:
            raise ValueError('history')
        step=INTERVALS[timeframe];due=int(now.timestamp())//step*step-step
        if history[-1].t==due:return MarketAdvance(list(history),True)
        if history[-1].t+step!=due:raise ValueError('coverage gap')
        if not isinstance(raw,list) or len(raw)>overlap_count+2:raise ValueError('market window')
        if any(not isinstance(row,dict) or type(row.get('t')) is not int or row['t']>due+step for row in raw):raise ValueError('forming bar')
        closed=[row for row in raw if row['t']+step<=int(now.timestamp())]
        if len(closed)==overlap_count:
            overlap,gaps=validate_candles([history[-overlap_count-1].json(),*closed],timeframe,now=int(now.timestamp()))
            if not gaps and overlap==history[-overlap_count-1:] and 0<=now.timestamp()-(due+step)<=15:
                return MarketAdvance(list(history),False)
        downloaded,gaps=validate_candles(closed,timeframe,now=int(now.timestamp()))
        if (gaps or len(downloaded)!=overlap_count+1 or downloaded[:-1]!=history[-overlap_count:]
                or downloaded[-1].t!=due):raise ValueError('revised/missing bars')
        return MarketAdvance([*history,downloaded[-1]],True)
    except (ValueError,TypeError,KeyError,AttributeError,IndexError,OverflowError):
        raise DexExecutorError('Velas MEXC incompletas, revisadas o sin continuidad; revisa el bot') from None


def warmup_market(seed:list[dict],market,*,timeframe:str,clock,require_active=lambda:None,overlap_count:int=2)->list[dict]:
    """Extend a verified seed before activation; this function never trades."""
    try:
        history,gaps=validate_candles(seed,timeframe,now=int(_time(clock()).timestamp()))
        if (gaps or seed!=[bar.json() for bar in history]
                or type(overlap_count) is not int or not 2<=overlap_count<=120 or len(history)<=overlap_count):raise ValueError('seed')
        step=INTERVALS[timeframe]
        # Cap the number of pages, including the final check after extension.
        page_capacity=MEXC_HISTORICAL_PAGE_SIZE-overlap_count
        for _ in range((20000+page_capacity-1)//page_capacity+1):
            require_active();now=_time(clock());due=int(now.timestamp())//step*step-step
            if history[-1].t==due:return [bar.json() for bar in history]
            if history[-1].t>due:raise ValueError('future seed')
            start=history[-overlap_count].t
            limit=min(MEXC_HISTORICAL_PAGE_SIZE,(due-start)//step+1)
            before=start+limit*step
            raw=market.get_candles('BNB/USDT',timeframe=timeframe,since=start,before=before,limit=limit)
            require_active();now=_time(clock());due=int(now.timestamp())//step*step-step
            if not isinstance(raw,list) or not overlap_count<len(raw)<=limit:raise ValueError('window')
            if any(not isinstance(row,dict) or type(row.get('t')) is not int or not start<=row['t']<before for row in raw):raise ValueError('window')
            bars,gaps=validate_candles(raw,timeframe,now=int(now.timestamp()))
            if gaps or bars[:overlap_count]!=history[-overlap_count:] or bars[-1].t<=history[-1].t:raise ValueError('revised/incomplete')
            history.extend(bars[overlap_count:])
            if len(history)>20000:raise ValueError('capacity')
    except (ValueError,TypeError,KeyError,AttributeError,IndexError,OverflowError):
        raise DexExecutorError('No se pudo completar un histórico inicial continuo de MEXC') from None
    raise DexExecutorError('El histórico inicial supera el límite de descarga; actualiza el origen')
