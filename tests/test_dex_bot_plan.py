"""DEX bot decisions share LAB rules and use real integer token positions."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal

import pytest

from app.lab.engine import validate_candles,validate_strategy,IndicatorCache,evaluate
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexBotCursor,DexReferenceQuote,plan_strategy
from app.live_execution.dex_policy import PolicyLimits
from app.live_execution.dex_state import ExecutorState
from tests.test_dex_policy import policy
from tests.test_spot_strategy_paper import definition

NOW=datetime(2025,1,1,0,5,1,tzinfo=timezone.utc)
UNIT=10**18


def bars(closes=(100,99,98,97,96)):
    start=int(NOW.timestamp())//60*60-len(closes)*60
    rows=[{'t':start+i*60,'o':str(c+1),'h':str(c+2),'l':str(c-1),'c':str(c),'v':str(i+10)} for i,c in enumerate(closes)]
    return validate_candles(rows,'1m',now=int(NOW.timestamp()))[0]


def state(**changes):
    values=dict(owner='0x'+'11'*20,executor_address='0x'+'33'*20,pool='0x'+'44'*20,
        operator='0x'+'22'*20,candidate_operator=None,fee=500,finalized=True,block_number=10,block_hash='0x'+'aa'*32,
        block_at=NOW-timedelta(seconds=1),observed_at=NOW,owner_nonce=1,pending_nonce=1,native_balance=UNIT,
        balance_usdt=1000*UNIT,balance_wbnb=UNIT,allowance_usdt=1000*UNIT,allowance_wbnb=UNIT,
        policy_id=1,policy_limits=PolicyLimits.parse({**policy(int(NOW.timestamp())),'max_wbnb_acquired':str(10*UNIT)}),next_nonce=0,buy_spent=0,entries=0,
        owned_wbnb=0,cost_usdt=0,realized_loss=0,last_entry_at=0,acquired_wbnb=0,current_operator_code_empty=True)
    values.update(changes);return ExecutorState(**values)


def cursor(**changes):
    values=dict(last_candle_t=bars()[-2].t,last_checked_at=NOW-timedelta(seconds=2),entry_candle_t=None,
        trailing_high=None,closing_reason=None)
    values.update(changes);return DexBotCursor(**values)


def quote(bid='100',**changes):
    values=dict(bid=Decimal(bid),ask=Decimal(bid)+Decimal('0.01'),event_at=NOW,observed_at=NOW)
    values.update(changes);return DexReferenceQuote(**values)


def plan(**changes):
    values=dict(definition=definition(),timeframe='1m',candles=bars(),cursor=cursor(),snapshot=state(),
        quote=quote(),entry_units=12*UNIT,exit_cap_units=UNIT,now=NOW)
    values.update(changes);return plan_strategy(**values)


def test_entry_requires_all_three_bearish_candles_not_just_two_similar_ones():
    assert plan().action=='BUY'
    two=bars((100,99,98,97,96));two[-3]=replace(two[-3],o=two[-3].c-1)
    result=plan(candles=two)
    assert result.action=='WAIT' and result.reason=='WAITING_ENTRY'
    expected=evaluate(validate_strategy(definition())['entry'],two,len(two)-1,IndicatorCache(two),base_timeframe='1m')
    assert result.evidence['entry']==expected[1] and result.evidence['entry_match'] is expected[0]


def test_real_entry_cap_ignores_the_paper_initial_capital_and_allocation():
    small=plan(entry_units=123456789012345678)
    enormous={**definition(),'initial_capital':'99999999999999','allocation':'1'}
    same=plan(entry_units=123456789012345678,definition=enormous)
    assert small.amount_units==same.amount_units==123456789012345678
    assert plan().amount_units==1000000000000000001


def test_nested_rules_and_higher_timeframes_use_the_same_lab_evidence():
    entry={'op':'AND','children':[{'kind':'BEARISH_STREAK','period':3},
        {'op':'OR','children':[{'kind':'RSI_LT','period':2,'threshold':'30'},
            {'kind':'CLOSE_ABOVE_EMA','period':3}]}]}
    configured={**definition(),'entry':entry}
    result=plan(definition=configured)
    expected=evaluate(validate_strategy(configured)['entry'],bars(),len(bars())-1,IndicatorCache(bars()),base_timeframe='1m')
    assert result.action=='BUY' and result.evidence['entry']==expected[1]
    entry['children'][1]={'kind':'CLOSE_ABOVE_EMA','period':3,'timeframe':'5m'}
    result=plan(definition={**definition(),'entry':entry})
    assert result.action=='WAIT'
    assert result.evidence['entry']['children'][1]['timeframe']=='5m'


@pytest.mark.parametrize('change,reason',[
    ({'cursor':cursor(last_candle_t=bars()[-1].t)},'WAITING_ENTRY'),
    ({'cursor':cursor(last_checked_at=NOW-timedelta(seconds=31))},'OBSERVATION_GAP'),
    ({'cursor':cursor(last_candle_t=bars()[-1].t-120)},'CANDLE_CONTINUITY_LOST'),
    ({'quote':quote(event_at=NOW-timedelta(seconds=16))},'REFERENCE_QUOTE_STALE'),
    ({'snapshot':state(balance_wbnb=0,owned_wbnb=UNIT,cost_usdt=100*UNIT,acquired_wbnb=UNIT)},'POSITION_NOT_COVERED'),
    ({'snapshot':state(entries=4)},'ENTRY_LIMIT'),
    ({'snapshot':state(acquired_wbnb=10*UNIT)},'WBNB_QUOTA_EXHAUSTED'),
    ({'snapshot':state(realized_loss=UNIT)},'LOSS_LIMIT'),
])
def test_stale_repeated_missing_or_uncovered_observations_never_buy(change,reason):
    result=plan(**change);assert result.action in ('WAIT','PAUSE') and result.reason==reason


@pytest.mark.parametrize('bid,reason',[('96.99','STOP_LOSS'),('108','TAKE_PROFIT')])
def test_exit_uses_actual_cost_and_owned_units_including_fractional_wei(bid,reason):
    owned=12345678901234567;cost=100*owned
    result=plan(snapshot=state(owned_wbnb=owned,cost_usdt=cost,acquired_wbnb=owned),
        cursor=cursor(entry_candle_t=bars()[-3].t),quote=quote(bid))
    assert result.action=='SELL' and result.reason==reason and result.amount_units==owned


def test_partial_exit_keeps_its_original_reason_after_the_signal_candle_changes():
    configured={**definition(),'exit':{'kind':'BEARISH_STREAK','period':3}}
    first=plan(definition=configured,snapshot=state(owned_wbnb=UNIT,cost_usdt=100*UNIT,acquired_wbnb=UNIT),
        cursor=cursor(entry_candle_t=bars()[-3].t),exit_cap_units=UNIT//2)
    assert first.action=='SELL' and first.reason=='EXIT_RULE' and first.amount_units==UNIT//2
    second=plan(definition=configured,snapshot=state(owned_wbnb=UNIT//2,cost_usdt=50*UNIT,acquired_wbnb=UNIT),
        cursor=cursor(last_candle_t=bars()[-1].t,entry_candle_t=bars()[-3].t,closing_reason=first.closing_reason),
        exit_cap_units=UNIT//2)
    assert second.action=='SELL' and second.reason=='EXIT_RULE' and second.amount_units==UNIT//2


def test_trailing_exit_keeps_the_peak_and_never_uses_paper_units():
    configured={**definition(),'take_profit':'1','trailing_stop':'0.05'}
    opened=state(owned_wbnb=UNIT//10,cost_usdt=10*UNIT,acquired_wbnb=UNIT//10)
    result=plan(definition=configured,snapshot=opened,quote=quote('109'),
        cursor=cursor(entry_candle_t=bars()[-3].t,trailing_high='115'))
    assert result.action=='SELL' and result.reason=='TRAILING_STOP'
    assert result.trailing_high=='115' and result.amount_units==UNIT//10


def test_holding_limit_counts_closed_bars_since_the_actual_entry():
    result=plan(definition={**definition(),'max_holding_bars':2},
        snapshot=state(owned_wbnb=UNIT//10,cost_usdt=10*UNIT,acquired_wbnb=UNIT//10),
        cursor=cursor(entry_candle_t=bars()[-3].t))
    assert result.action=='SELL' and result.reason=='TIME_LIMIT'


def test_a_brief_close_publication_delay_does_not_disable_a_fresh_protective_exit():
    history=bars()[:-1]
    result=plan(candles=history,snapshot=state(owned_wbnb=UNIT//10,cost_usdt=10*UNIT,acquired_wbnb=UNIT//10),
        cursor=cursor(last_candle_t=history[-1].t,entry_candle_t=history[-2].t),quote=quote('96.99'))
    assert result.action=='SELL' and result.reason=='STOP_LOSS' and result.evidence['candle_pending'] is True
    waiting=plan(candles=history,cursor=cursor(last_candle_t=history[-1].t))
    assert waiting.action=='WAIT' and waiting.reason=='WAITING_CANDLE'


def test_future_candles_and_invalid_financial_inputs_are_rejected():
    for changes in ({'candles':bars()+[replace(bars()[-1],t=bars()[-1].t+60)]},
                    {'entry_units':True},{'exit_cap_units':-1},
                    {'definition':definition('SHORT')},
                    {'quote':quote(bid=Decimal('NaN'))}):
        with pytest.raises((DexExecutorError,ValueError)):plan(**changes)
