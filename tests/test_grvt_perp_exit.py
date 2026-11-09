"""Independent prices, causal full-prefix rules and exact exit comparisons."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
from fractions import Fraction
import json

import pytest

from app.lab.engine import IndicatorCache,evaluate
from app.live_execution.grvt_perp_exit import observe_perp_exit,perp_exit_checkpoint,start_perp_exit
from app.live_execution.grvt_perp_signals import PerpSignalSeed,advance_signals
from tests.grvt_perp_signal_fixture import NS,bar,signal_input,signal_seed


def position(seed,*,size='1',notional='100',fill_offset=1,confirmed_offset=None):
    state=seed.start()
    fill=state.started_at_ns+fill_offset*NS
    return state,start_perp_exit(state,size=D(size),entry_notional=D(notional),last_fill_ns=fill,
        confirmed_at_ns=state.started_at_ns+(fill_offset if confirmed_offset is None else confirmed_offset)*NS)


def tick(state,seconds,**changes):
    return advance_signals(state,signal_input(state,seconds,**changes)).state


@pytest.mark.parametrize('side,bid,ask,reason',[
    ('LONG','96','100','STOP_OR_TRAILING'),('LONG','109','110','TAKE_PROFIT'),
    ('SHORT','96','104','STOP_OR_TRAILING'),('SHORT','90','91','TAKE_PROFIT')])
def test_actual_bid_for_long_and_ask_for_short_trigger_without_using_mark(side,bid,ask,reason):
    state,cursor=position(signal_seed(side))
    state=tick(state,10,bid=bid,ask=ask,mark_price='100')
    result=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
    assert result.action=='CLOSE' and result.reason==reason
    assert result.trigger_price==D(bid if side=='LONG' else ask)
    assert result.known_at_ns==state.last_observed_ns
    assert perp_exit_checkpoint(result)['execution_authority']=='NONE'


@pytest.mark.parametrize('side,mark',[('LONG','50'),('LONG','150'),('SHORT','50'),('SHORT','150')])
def test_mark_alone_is_not_a_software_exit_signal(side,mark):
    state,cursor=position(signal_seed(side))
    state=tick(state,10,bid='99.9',ask='100.1',mark_price=mark)
    result=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
    assert result.action=='HOLD' and result.reason=='POSITION_OPEN'


def test_close_is_sticky_when_the_observed_market_recovers():
    state,cursor=position(signal_seed())
    state=tick(state,10,bid='96',ask='100')
    result=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
    state=tick(state,20,bid='100',ask='100')
    assert observe_perp_exit(result,state,now_ns=state.last_observed_ns)==result
    assert result.trigger_price==96  # No hypothetical fill at this trigger price.


@pytest.mark.parametrize('side,price,close',[
    ('LONG','32.333333333333333333',True),('LONG','32.333333333333333334',False),
    ('SHORT','34.333333333333333333',False),('SHORT','34.333333333333333334',True)])
def test_nonterminating_vwap_is_compared_as_an_exact_notional_ratio(side,price,close):
    state,cursor=position(signal_seed(side),size='3',notional='100')
    state=tick(state,10,bid=price,ask=price,mark_price=price)
    with localcontext() as context:
        context.prec=4
        result=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
    threshold=Fraction(100)*(1-Fraction(3,100) if side=='LONG' else 1+Fraction(3,100))
    oracle=Fraction(price)*3 <= threshold if side=='LONG' else Fraction(price)*3 >= threshold
    assert oracle==close and (result.action=='CLOSE')==close


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_partial_entry_bar_cannot_supply_trailing_extremes_but_counts_as_a_later_close(side):
    seed=signal_seed(side,stop_loss='0.5',take_profit='0.5',trailing_stop='0.03')
    state,cursor=position(seed)
    start=state.started_at_ns//NS
    for second in range(10,121,10):
        candles=()
        if second==60:
            candles=(bar(start,side,h='200',l='1'),)
        elif second==120:
            candles=(bar(start+60,side,o='100',h='105',l='95',c='100'),)
        state=tick(state,second,bid='102' if side=='LONG' else '96',ask='103' if side=='LONG' else '97',candles=candles)
        cursor=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
        assert cursor.action=='HOLD'
        if second==60:assert cursor.trailing_notional==100 and cursor.holding_bars==1
    assert cursor.trailing_notional==(105 if side=='LONG' else 95) and cursor.holding_bars==2
    state=tick(state,130,bid='100',ask='100')
    result=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
    assert result.action=='CLOSE' and result.reason=='STOP_OR_TRAILING'


def test_holding_limit_counts_closes_including_the_partial_entry_bar():
    state,cursor=position(signal_seed(max_holding_bars=1),fill_offset=2)
    start=state.started_at_ns//NS
    for second in range(10,61,10):
        state=tick(state,second,candles=(bar(start),) if second==60 else ())
        cursor=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
        assert cursor.action==('CLOSE' if second==60 else 'HOLD')
    assert cursor.reason=='TIME_LIMIT' and cursor.holding_bars==1
    assert cursor.known_at_ns-cursor.last_fill_ns==58*NS  # Not 60 seconds from the mid-bar fill.


def test_pre_entry_true_condition_waits_for_a_new_post_fill_close_and_matches_paper_priority():
    seed=signal_seed(exit={'kind':'BULLISH_STREAK','period':2},max_holding_bars=1)
    state,cursor=position(seed)
    start=state.started_at_ns//NS
    for second in range(10,61,10):
        state=tick(state,second,bid='99' if second<60 else '96',ask='100',
            candles=(bar(start),) if second==60 else ())
        cursor=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
        if second<60:assert cursor.action=='HOLD'
    assert cursor.reason=='EXIT_RULE' and cursor.condition_candle_t==start
    assert json.loads(cursor.exit_rule_evidence_json)['matched'] is True


def test_complete_indicator_prefix_changes_the_correct_exit_decision():
    seed=signal_seed(exit={'kind':'CLOSE_ABOVE_EMA','period':200},stop_loss='0.5',take_profit='0.5')
    payload=seed.payload();start=int(seed.prepared_at_ns)//NS
    payload['history']=[bar(start+i*60,o='49' if i<-500 else '99',h='51' if i<-500 else '101',
        l='48' if i<-500 else '98',c='50' if i<-500 else '100').json() for i in range(-1000,0)]
    seed=PerpSignalSeed.model_validate(payload)
    state,cursor=position(seed)
    for second in range(10,61,10):
        state=tick(state,second,candles=(bar(start,o='99',h='101',l='98',c='100'),) if second==60 else ())
        cursor=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
    rows=list(state.history)
    matched,evidence=evaluate(seed.definition['exit'],rows,len(rows)-1,IndicatorCache(rows),base_timeframe='1m')
    clipped=rows[-500:]
    truncated,_=evaluate(seed.definition['exit'],clipped,len(clipped)-1,IndicatorCache(clipped),base_timeframe='1m')
    assert matched is True and truncated is False and cursor.reason=='EXIT_RULE'
    assert json.loads(cursor.exit_rule_evidence_json)==evidence


def test_higher_timeframe_rule_never_uses_an_open_aggregate():
    seed=signal_seed(exit={'kind':'CLOSE_ABOVE_SMA','period':2,'timeframe':'5m'},stop_loss='0.5',take_profit='0.5')
    payload=seed.payload();start=int(seed.prepared_at_ns)//NS
    payload['history']=[bar(start+i*60,o='100',h='100',l='100',c='100').json() for i in range(-60,0)]
    state,cursor=position(PerpSignalSeed.model_validate(payload))
    for second in range(10,301,10):
        candles=(bar(start+second-60,o='100',h='111',l='99',c='110'),) if second%60==0 else ()
        state=tick(state,second,candles=candles)
        cursor=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns)
        assert cursor.action==('CLOSE' if second==300 else 'HOLD')
    assert cursor.reason=='EXIT_RULE' and cursor.holding_bars==5
    assert json.loads(cursor.exit_rule_evidence_json)['higher_candle_t']==start


def test_confirmation_delay_waits_for_a_new_quote_and_does_not_claim_earlier_software_protection():
    state,cursor=position(signal_seed(),fill_offset=1,confirmed_offset=12)
    state=tick(state,10)
    result=observe_perp_exit(cursor,state,now_ns=state.started_at_ns+12*NS)
    assert result.reason=='AWAITING_CONFIRMED_QUOTE' and result.last_quote_ns==0
    state=tick(state,20,bid='96',ask='100')
    result=observe_perp_exit(result,state,now_ns=state.last_observed_ns)
    assert result.reason=='STOP_OR_TRAILING' and result.known_at_ns==state.started_at_ns+20*NS


@pytest.mark.parametrize('gap',[False,True])
def test_stale_or_interrupted_feed_produces_a_pending_close_without_inventing_an_execution_price(gap):
    state,cursor=position(signal_seed())
    state=tick(state,10)
    if gap:state=replace(state,status='GAP')
    result=observe_perp_exit(cursor,state,now_ns=state.last_observed_ns+(0 if gap else 6*NS))
    assert result.action=='CLOSE' and result.reason==('DATA_GAP' if gap else 'DATA_STALE')
    assert result.trigger_price is None and result.trigger_quote_ns is None
    assert perp_exit_checkpoint(result)['execution_authority']=='NONE'


@pytest.mark.parametrize('change',['seed','definition','instrument','time'])
def test_wrong_origin_or_time_cannot_reuse_a_cursor(change):
    from app.lab.store import encoded
    state,cursor=position(signal_seed())
    state=tick(state,10)
    now=state.last_observed_ns
    if change=='seed':state=replace(state,seed_hash='f'*64)
    elif change=='definition':state=replace(state,definition_json=encoded({**json.loads(state.definition_json),'stop_loss':'0.02'}))
    elif change=='instrument':state=replace(state,instrument='ETH_USDT_Perp')
    else:now=cursor.confirmed_at_ns-1
    with pytest.raises(ValueError):observe_perp_exit(cursor,state,now_ns=now)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('scenario',['stop','target','holding','exit_rule','trailing'])
def test_software_exit_matches_actual_observed_paper_engine_for_identical_quotes_and_closes(side,scenario):
    from app.derivatives.perp_observed import observe
    from tests.perp_observed_fixture import initial,batch
    from app.derivatives.perp_observed_wire import quote_input
    changes={}
    if scenario=='holding':changes['max_holding_bars']=1
    if scenario=='exit_rule':
        changes.update(max_holding_bars=1,exit={'kind':'BULLISH_STREAK' if side=='LONG' else 'BEARISH_STREAK','period':2})
    if scenario=='trailing':changes.update(stop_loss='0.5',take_profit='0.5',trailing_stop='0.03')
    paper=initial(side,'CROSS',**changes)
    feed=PerpSignalSeed.model_validate({'schema':'orbis.grvt.perp.signals.seed.v1',
        'engine':'grvt-perp-signals-1.0','origin_reference_hash':'a'*64,
        'profile':paper.profile.payload(),'definition':json.loads(paper.definition_json),
        'instrument':paper.last_quote.instrument,'timeframe':paper.timeframe,
        'requested_at_ns':str(paper.started_at_ns),'prepared_at_ns':str(paper.started_at_ns),
        'quote':quote_input(paper.last_quote).payload(),'history':[row.json() for row in paper.history],
        'execution_authority':'NONE'}).start()
    start=paper.started_at_ns//NS
    for second in range(10,61,10):
        candles=(bar(start,side),) if second==60 else ()
        observed=batch(paper,second,bars=candles)
        paper=observe(paper,observed).state
        feed=tick(feed,second,candles=candles,bid=str(observed.quote.bid),ask=str(observed.quote.ask))
    assert paper.opened is not None
    cursor=start_perp_exit(feed,size=paper.opened.quantity,
        entry_notional=paper.opened.quantity*paper.opened.entry_price,
        last_fill_ns=paper.opened.entry_time_ns,confirmed_at_ns=paper.last_received_ns)
    found=None
    for second in range(70,131,10):
        bid,ask=('99','100') if side=='LONG' else ('100','101')
        candles=(bar(start+60,side,h='105',l='95'),) if second==120 else ()
        if scenario=='stop':bid,ask=('96','100') if side=='LONG' else ('100','104')
        elif scenario=='target':bid,ask=('109','110') if side=='LONG' else ('90','91')
        elif scenario=='trailing' and second<=120:
            bid,ask=('102','103') if side=='LONG' else ('97','98')
        result=observe(paper,batch(paper,second,bid=bid,ask=ask,bars=candles))
        paper=result.state
        feed=tick(feed,second,candles=candles,bid=bid,ask=ask)
        cursor=observe_perp_exit(cursor,feed,now_ns=feed.last_observed_ns)
        if result.trades_json:
            found=json.loads(result.trades_json[0])
            break
        assert cursor.action=='HOLD'
    assert found is not None and cursor.action=='CLOSE'
    expected={'stop':'STOP_OR_TRAILING','target':'TAKE_PROFIT','holding':'TIME_LIMIT',
        'exit_rule':'EXIT_RULE','trailing':'STOP_OR_TRAILING'}[scenario]
    assert cursor.reason==expected
    assert found['exit_reason']==('EXIT_RULE_OBSERVED_QUOTE' if expected=='EXIT_RULE' else expected)
    assert cursor.known_at_ns==paper.last_received_ns
    assert cursor.trigger_price==(paper.last_quote.bid if side=='LONG' else paper.last_quote.ask)
