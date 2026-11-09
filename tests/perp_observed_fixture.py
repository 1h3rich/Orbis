"""Explicit quotes, minute candles and public snapshots; no network or storage."""
from dataclasses import replace
from decimal import Decimal as D

from app.derivatives.funding import FundingRecord
from app.derivatives.paper_math import PerpQuote
from app.derivatives.perp_observed import observe,start_observed
from app.derivatives.perp_observed_types import Observation
from app.lab.engine import Candle
from tests.perp_simulation_fixture import definition,profile
from tests.test_perp_account import market,NOW,BTC


NS=10**9


def bar(stamp,side='LONG',**changes):
    raw={'t':stamp,'o':D('100'),'h':D('102'),'l':D('98'),
         'c':D('101') if side=='LONG' else D('99'),'v':D('10')}
    raw.update({key:D(value) if key!='t' else value for key,value in changes.items()})
    return Candle(**raw)


def initial(side='LONG',mode='ISOLATED',*,start=NOW,fee='0.001',maintenance='0.05',**strategy_changes):
    strategy=definition(side,fee_rate=fee,**strategy_changes)
    configured=profile(strategy,margin_mode=mode)
    selected=market(mode,fee=fee,maintenance=maintenance)
    selected=replace(selected,rules=replace(selected.rules,fetched_at_ns=start),
                     specification=replace(selected.specification,fetched_at_ns=start))
    seed=tuple(bar(start//NS+i*60,side) for i in (-3,-2,-1))
    quote=PerpQuote(BTC,start,D('100'),D('99') if side=='LONG' else D('100'),D('100') if side=='LONG' else D('101'))
    anchor=FundingRecord(BTC,start//(3600*NS)*(3600*NS),D('0'),D('100'),1)
    return start_observed(configured,strategy,'1m',selected,seed,quote,start,(anchor,)).state


def batch(state,seconds,*,bid=None,ask=None,mark='100',bars=(),funding=None,rules=None,quote_time=None):
    now=state.started_at_ns+seconds*NS
    q=state.last_quote
    quote=PerpQuote(BTC,quote_time if quote_time is not None else now,D(mark),D(bid) if bid is not None else q.bid,D(ask) if ask is not None else q.ask)
    return Observation(quote,now,funding or (state.funding_anchor,),
        rules or replace(state.account.markets[0].rules,fetched_at_ns=now),
        replace(state.account.markets[0].specification,fetched_at_ns=now),tuple(bars))


def enter(side='LONG',mode='ISOLATED',**kwargs):
    state=initial(side,mode,**kwargs);transitions=[]
    for second in range(10,61,10):
        candles=(bar(state.started_at_ns//NS,side),) if second==60 else ()
        result=observe(state,batch(state,second,bars=candles));transitions.append(result);state=result.state
    return state,transitions
