"""Salidas desde fills auditados, sin usar precio ni capital PAPER."""

from dataclasses import replace
from decimal import Decimal,localcontext

import pytest

from app.lab.engine import Candle,CURRENT_ENGINE_VERSION
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderObservation
from tests.test_grvt_testnet_fills import FakeClient,fill,SUB,CID


D=Decimal
HOUR=3600
BASE=1_780_000_000//HOUR*HOUR
OPEN_NS=(BASE+HOUR+900)*1_000_000_000
NOW_NS=(BASE+2*HOUR+10)*1_000_000_000


def definition(direction='LONG',**extra):
    return {'direction':direction,'initial_capital':'1000','allocation':'0.1',
        'fee_rate':'0.001','slippage_rate':'0','stop_loss':'0.03','take_profit':'0.08',
        'entry':{'kind':'BEARISH_STREAK','period':3},**extra}


def audited_entry(direction='LONG'):
    order=GrvtTestnetOrderObservation(SUB,CID,'0x123abc','FILLED','BTC_USDT_Perp',
        'BUY' if direction=='LONG' else 'SELL','0.01','0.01','110' if direction=='LONG' else '90',
        str(OPEN_NS),create_time_ns=str(OPEN_NS-1_000_000_000))
    rows=[{**fill(trade_id='actual-1',size='0.004'),'price':'100',
        'event_time':str(OPEN_NS-1),'is_buyer':direction=='LONG'},
        {**fill(trade_id='actual-2',size='0.006'),'price':'102',
        'event_time':str(OPEN_NS),'is_buyer':direction=='LONG'}]
    audit=audit_testnet_fills(FakeClient({'':{'result':rows,'next':''}}),order,now_ns=NOW_NS)
    return order,audit


def candles(*,end=BASE+2*HOUR):
    return [Candle(t,D('101'),D('102'),D('100'),D('101'),D('10'))
        for t in range(BASE-2*HOUR,end,HOUR)]


def review(direction='LONG',*,mark='98.1',bars=None,now_ns=NOW_NS,config=None,order=None,audit=None):
    from app.live_execution.grvt_filled_strategy_exit import evaluate_filled_strategy_exit,GrvtExitMark
    original,original_audit=audited_entry(direction)
    return evaluate_filled_strategy_exit(config or definition(direction),timeframe='1h',
        engine_version=CURRENT_ENGINE_VERSION,order=order or original,audit=audit or original_audit,
        candles=candles() if bars is None else bars,
        mark=GrvtExitMark('BTC_USDT_Perp',now_ns,now_ns,D(mark)),now_ns=now_ns)


def test_stop_uses_weighted_actual_fills_instead_of_limit_or_paper_entry():
    result=review()
    assert result.action=='CLOSE' and result.reason=='STOP'
    assert result.entry_price==D('101.2')
    assert result.stop_price==D('98.164') and result.target_price==D('109.296')
    assert result.size==D('0.01') and result.last_fill_at_ns==OPEN_NS
    assert result.trigger_basis=='TESTNET_MARK' and result.execution_authorized is False


@pytest.mark.parametrize('direction,mark,reason',[
    ('LONG','98.164','STOP'),('LONG','109.296','TARGET'),('LONG','101.2','POSITION_OPEN'),
    ('SHORT','104.236','STOP'),('SHORT','93.104','TARGET'),('SHORT','101.2','POSITION_OPEN'),
])
def test_long_and_short_thresholds_include_the_boundary(direction,mark,reason):
    result=review(direction,mark=mark)
    assert result.reason==reason
    assert result.action==('HOLD' if reason=='POSITION_OPEN' else 'CLOSE')


@pytest.mark.parametrize('direction,extreme,mark,expected',[
    ('LONG','110','106.7','106.7'),('SHORT','90','92.7','92.7'),
])
def test_trailing_excludes_the_fill_candle_and_uses_only_later_complete_bars(direction,extreme,mark,expected):
    config=definition(direction,trailing_stop='0.03')
    bars=candles()
    bars[-1]=replace(bars[-1],h=D('900'),l=D('1'))
    first=review(direction,config=config,bars=bars,mark='101.2')
    assert first.reason=='POSITION_OPEN' and first.trailing_extreme==D('101.2')
    now=(BASE+3*HOUR+10)*1_000_000_000
    after=Candle(BASE+2*HOUR,D('101'),D(extreme) if direction=='LONG' else D('102'),
        D(extreme) if direction=='SHORT' else D('100'),D('101'),D('10'))
    result=review(direction,config=config,bars=bars+[after],mark=mark,now_ns=now)
    assert result.reason=='TRAILING_STOP' and result.stop_price==D(expected)
    assert result.trailing_extreme==D(extreme)


def test_time_limit_starts_at_last_fill_and_does_not_round_to_the_paper_candle():
    config=definition(max_holding_bars=1)
    at=OPEN_NS+HOUR*1_000_000_000
    bars=candles(end=BASE+2*HOUR)
    assert review(config=config,mark='101.2',now_ns=at-1,bars=bars).action=='HOLD'
    result=review(config=config,mark='101.2',now_ns=at,bars=bars)
    assert result.reason=='TIME_LIMIT' and result.time_limit_at_ns==at


def test_exit_rules_reuse_lab_semantics_but_ignore_closes_from_before_the_fill():
    config=definition(exit={'kind':'VOLUME_RISING_STREAK','period':3})
    bars=[replace(bar,v=D(index+1)) for index,bar in enumerate(candles())]
    assert review(config=config,mark='101.2',bars=bars).action=='HOLD'
    bars.append(replace(bars[-1],t=BASE+2*HOUR,v=D('9')))
    result=review(config=config,mark='101.2',bars=bars,now_ns=(BASE+3*HOUR+10)*1_000_000_000)
    assert result.reason=='EXIT_RULE'


def test_missing_indicator_warmup_is_not_reported_as_a_valid_hold():
    from app.live_execution.grvt_filled_strategy_exit import GrvtStrategyExitError
    config=definition(exit={'kind':'RSI_LT','period':200,'threshold':'30'})
    with pytest.raises(GrvtStrategyExitError):
        review(config=config,mark='101.2',bars=candles(end=BASE+3*HOUR),
            now_ns=(BASE+3*HOUR+10)*1_000_000_000)


@pytest.mark.parametrize('bad_bars',[
    candles()[1:-1],candles()[:-1],candles()+[candles()[-1]],
    candles()+[replace(candles()[-1],t=BASE+2*HOUR)],
    [bar for index,bar in enumerate(candles()) if index!=1],
    [replace(bar,h=D('NaN')) if index==0 else bar for index,bar in enumerate(candles())],
])
def test_gaps_open_duplicate_or_invalid_candles_do_not_become_hold(bad_bars):
    from app.live_execution.grvt_filled_strategy_exit import GrvtStrategyExitError
    with pytest.raises(GrvtStrategyExitError): review(mark='101.2',bars=bad_bars)


@pytest.mark.parametrize('change',[
    {'sizes_match':False},{'gross_notional':D('1')},{'latest_fill_time_ns':OPEN_NS+1},
    {'filled_size':D('0.02')},{'client_order_id':'999'},{'fills':()},
])
def test_unverified_entry_is_rejected(change):
    from app.live_execution.grvt_filled_strategy_exit import GrvtStrategyExitError
    _,audit=audited_entry()
    with pytest.raises(GrvtStrategyExitError): review(audit=replace(audit,**change))


def test_actual_entry_calculations_are_independent_of_ambient_decimal_precision():
    with localcontext() as context:
        context.prec=6
        result=review()
    assert result.entry_notional==D('1.012') and result.stop_price==D('98.164')


@pytest.mark.parametrize('kind',['future','stale','pre_fill','different_market'])
def test_mark_must_be_fresh_and_after_the_actual_fill(kind):
    from app.live_execution.grvt_filled_strategy_exit import (
        GrvtExitMark,GrvtStrategyExitError,evaluate_filled_strategy_exit,
    )
    order,audit=audited_entry()
    mark=GrvtExitMark('ETH_USDT_Perp' if kind=='different_market' else order.instrument,
        NOW_NS+1 if kind=='future' else NOW_NS-5_000_000_001 if kind=='stale' else
        OPEN_NS-1 if kind=='pre_fill' else NOW_NS,NOW_NS,D('100'))
    with pytest.raises(GrvtStrategyExitError):
        evaluate_filled_strategy_exit(definition(),timeframe='1h',engine_version=CURRENT_ENGINE_VERSION,
            order=order,audit=audit,candles=candles(),mark=mark,now_ns=NOW_NS)
