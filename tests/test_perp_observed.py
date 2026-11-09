"""Financial oracles and causality for the unwired current-observation consumer."""
from dataclasses import replace
from decimal import Decimal as D, localcontext, ROUND_DOWN
import json

import pytest

from app.derivatives.funding import FundingRecord
from app.derivatives.perp_observed import observe,observed_gap,request_observed_close,stop_observed
from app.derivatives.perp_observed_types import ObservedError
from app.lab.perp_account import fill_order,mark_all,mark_with_rules,submit_order
from app.lab.perp_book_types import Cursor
from tests.perp_observed_fixture import NS,BTC,NOW,bar,batch,enter,initial


def assert_deltas(before,transition):
    after=transition.state.account;entries=transition.journal
    assert after.cash-before.cash==sum((e.cash_delta for e in entries),D(0))
    assert sum((p.collateral for p in after.positions),D(0))-sum((p.collateral for p in before.positions),D(0))==sum((e.collateral_delta for e in entries),D(0))
    for field,journal in [('realized_pnl','realized_pnl'),('fees_paid','fee'),('funding_paid','funding_payment'),('insurance_transferred','insurance_transfer'),('insurance_covered','insurance_cover')]:
        assert getattr(after,field)-getattr(before,field)==sum((getattr(e,journal) for e in entries),D(0))


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_first_new_closed_signal_then_real_bid_ask_fee_oracle(side,mode):
    state,steps=enter(side,mode)
    assert all(not step.state.account.positions for step in steps[:-1])
    assert state.opened.quantity==D('24.875') and state.opened.entry_price==100
    assert state.opened.entry_time_ns==NOW+60*NS
    assert state.opened.source_candle_t==NOW//NS
    assert json.loads(steps[-1].signals_json[0])['action']=='ENTERED_OBSERVED_QUOTE'
    final=observe(state,batch(state,70,bid='109' if side=='LONG' else '90',ask='110' if side=='LONG' else '91'))
    assert_deltas(state.account,final)
    expected=D('1000')+D('24.875')*(D('9')-D('0.1')-(D('0.109') if side=='LONG' else D('0.091')))
    assert final.state.account.cash==expected
    trade=json.loads(final.trades_json[0])
    assert trade['exit_reason']=='TAKE_PROFIT'
    assert D(trade['net_profit'])==expected-D('1000')
    assert D(trade['exit_price'])==(D('109') if side=='LONG' else D('91'))
    assert final.state.opened is None and not final.state.account.positions


def test_seed_signals_never_open_positions_and_no_synthetic_end_close():
    state=initial();assert state.opened is None and not state.account.orders
    state,_=enter()
    result=observe(state,batch(state,70))
    assert result.state.opened is not None and not result.trades_json
    assert not any(e.kind=='FILL' for e in result.journal)


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_mark_liquidation_precedes_executable_quote_exit(mode):
    state,_=enter(mode=mode)
    result=observe(state,batch(state,70,bid='109',ask='110',mark='60'))
    assert result.state.status=='LIQUIDATED' and result.state.opened is None
    assert not any(e.kind=='FILL' for e in result.journal)
    assert json.loads(result.trades_json[0])['exit_reason']=='LIQUIDATION'
    assert result.state.account.cash==(D('500.0125') if mode=='ISOLATED' else D(0))
    assert_deltas(state.account,result)


def test_new_mark_and_lower_maintenance_are_one_risk_observation():
    state,_=enter(maintenance='0.1')
    now=NOW+70*NS;old=state.account.markets[0].rules
    rules=replace(old,fetched_at_ns=now,tiers=(replace(old.tiers[0],maintenance_rate=D('0.05')),))
    naive=mark_all(state.account,Cursor(now,0),{BTC:D('85')})
    assert not naive.account.positions  # applying obsolete maintenance would liquidate
    result=observe(state,batch(state,70,bid='99',ask='100',mark='85',rules=rules))
    assert result.state.status=='RUNNING' and result.state.account.positions
    pocket=result.state.account.positions[0]
    assert pocket.collateral+D('24.875')*(D('85')-D('100'))==D('124.375')
    assert not any('LIQUIDATION' in e.kind for e in result.journal)


def test_funding_after_entry_and_before_tied_exit_posts_once():
    start=NOW+3480*NS
    state,_=enter(start=start,fee='0')
    for second in range(70,120,10):state=observe(state,batch(state,second)).state
    record=FundingRecord(BTC,NOW+3600*NS,D('1'),D('100'),1)
    result=observe(state,batch(state,120,bid='109',ask='110',bars=(bar(start//NS+60),),funding=(state.funding_anchor,record)))
    assert result.state.account.funding_paid==D('24.999')
    assert result.state.account.cash==D('1199.992')
    kinds=[e.kind for e in result.journal]
    assert kinds.index('FUNDING')<kinds.index('FILL')
    again=observe(result.state,batch(result.state,130,bid='109',ask='110',funding=(record,)))
    assert again.state.account.cash==result.state.account.cash and not any(e.kind=='FUNDING' for e in again.journal)
    assert_deltas(state.account,result)


def test_new_maintenance_is_not_applied_to_the_preceding_funding_mark():
    start = NOW+3480*NS
    state, _ = enter(start=start, maintenance='0.05')
    for second in range(70, 120, 10):
        state = observe(state, batch(state, second)).state
    settled = FundingRecord(BTC, NOW+3600*NS, D('0.1'), D('85'), 1)
    quote_at = start+125*NS
    prior = state.account.markets[0].rules
    rules = replace(prior, fetched_at_ns=quote_at-NS,
                    tiers=(replace(prior.tiers[0], maintenance_rate=D('0.10')),))
    result = observe(state, batch(state, 125, bid='84', ask='85', mark='85', rules=rules,
                                  bars=(bar(start//NS+60),), funding=(state.funding_anchor, settled)))
    funding, = [entry for entry in result.journal if entry.kind == 'FUNDING']
    assert funding.cursor.time_ns == settled.time_ns
    assert funding.funding_payment == state.opened.quantity*D('85')*D('0.001')
    assert result.state.status == 'LIQUIDATED'
    assert json.loads(result.trades_json[0])['exit_time_ns'] == str(quote_at)
    assert_deltas(state.account, result)


def test_duplicate_and_out_of_order_quotes_cannot_fill_or_settle_twice():
    state,_=enter()
    duplicate=replace(batch(state,70),quote=state.last_quote)
    result=observe(state,duplicate)
    assert result.state==state and not result.journal
    old=replace(duplicate,quote=replace(state.last_quote,event_time_ns=state.last_quote.event_time_ns-NS))
    assert observe(state,old).state==state
    bad=replace(duplicate,quote=replace(state.last_quote,bid=D('98')))
    with pytest.raises(ObservedError,match='contradictorias'):observe(state,bad)


def test_unknown_quote_interval_stays_a_gap_with_position_and_no_invented_fill():
    state,_=enter();result=observe(state,batch(state,90,bid='50',ask='51',mark='50'))
    assert result.state.status=='GAP' and result.state.account==state.account
    assert result.state.opened==state.opened and not result.journal and not result.trades_json
    assert json.loads(result.events_json[0])['reconstructed'] is False
    assert observe(result.state,batch(result.state,100)).state==result.state


def test_stop_and_manual_close_need_a_new_verified_quote():
    state,_=enter();stopped=stop_observed(state,NOW+61*NS)
    assert stopped.state.status=='CLOSE_ONLY' and stopped.state.account==state.account
    requested=request_observed_close(stopped.state,NOW+62*NS)
    assert requested.state.opened is not None and not requested.journal
    final=observe(requested.state,batch(requested.state,70,bid='103',ask='104'))
    assert final.state.status=='STOPPED' and final.state.opened is None
    assert json.loads(final.trades_json[0])['exit_reason']=='MANUAL_CLOSE_OBSERVED'
    assert stop_observed(initial(),NOW+NS).state.status=='STOPPED'
    with pytest.raises(ObservedError):observe(requested.state,batch(requested.state,61))


def test_trailing_only_moves_after_its_candle_has_closed():
    state,_=enter(trailing_stop='0.03')
    for second in range(70,120,10):state=observe(state,batch(state,second,bid='105',ask='106')).state
    assert state.opened is not None and state.opened.trailing==100
    result=observe(state,batch(state,120,bid='105',ask='106',bars=(bar(NOW//NS+60,h='110',c='108'),)))
    assert result.state.opened is None
    trade=json.loads(result.trades_json[0]);assert trade['exit_reason']=='STOP_OR_TRAILING' and D(trade['exit_price'])==105


@pytest.mark.parametrize('strategy,reason',[
    ({'max_holding_bars':1},'TIME_LIMIT'),
    ({'exit':{'kind':'CLOSE_ABOVE_SMA','period':2}},'EXIT_RULE_OBSERVED_QUOTE'),
])
def test_closed_rule_and_time_exit_do_not_reenter_the_same_signal(strategy,reason):
    state,_=enter(**strategy)
    for second in range(70,120,10):state=observe(state,batch(state,second)).state
    result=observe(state,batch(state,120,bars=(bar(NOW//NS+60,h='110',c='108'),)))
    assert result.state.opened is None and len(result.trades_json)==1
    assert json.loads(result.trades_json[0])['exit_reason']==reason
    assert json.loads(result.signals_json[0])['action']=='SKIPPED_POSITION_OPEN'


def test_missing_new_closed_candle_is_not_backfilled_as_live_signal():
    state=initial()
    for second in range(10,80,10):state=observe(state,batch(state,second)).state
    result=observe(state,batch(state,80))
    assert result.state.status=='GAP' and result.state.reason=='MISSING_CLOSED_CANDLE'
    assert not result.state.account.positions


def test_future_open_candle_and_uncovered_or_changed_funding_are_rejected():
    state=initial()
    with pytest.raises(ObservedError,match='abiertas'):observe(state,batch(state,10,bars=(bar(NOW//NS),)))
    with pytest.raises(ObservedError,match='cambió|falta'):
        observe(state,batch(state,10,funding=(replace(state.funding_anchor,rate_percent=D('1')),)))
    with pytest.raises(ObservedError,match='esperada'):
        observe(state,batch(state,10,funding=(replace(state.funding_anchor,time_ns=NOW-3600*NS),)))
    with pytest.raises(ObservedError,match='futura'):
        observe(state,batch(state,10,quote_time=NOW+11*NS))


def test_external_decimal_context_cannot_change_the_observed_book():
    state,_=enter();incoming=batch(state,70,bid='109',ask='110')
    result=observe(state,incoming)
    with localcontext() as context:
        context.prec=6;context.rounding=ROUND_DOWN
        assert observe(state,incoming)==result


def test_explicit_gap_and_stop_do_not_claim_a_close_or_auto_recover():
    state,_=enter();gap=observed_gap(state,NOW+61*NS,'SERVICE_SHUTDOWN')
    assert gap.state.status=='GAP' and gap.state.opened==state.opened
    assert stop_observed(gap.state,NOW+62*NS).state.status=='GAP'
    with pytest.raises(ObservedError):request_observed_close(gap.state,NOW+63*NS)


def test_delayed_entry_does_not_take_its_partial_candle_high_for_trailing():
    state=initial(trailing_stop='0.03')
    for second in range(10,60,10):state=observe(state,batch(state,second)).state
    entered=observe(state,batch(state,65,bars=(bar(NOW//NS),)));state=entered.state
    assert state.opened.entry_time_ns==NOW+65*NS
    for second in range(70,120,10):state=observe(state,batch(state,second,bid='105',ask='106')).state
    result=observe(state,batch(state,120,bid='105',ask='106',bars=(bar(NOW//NS+60,h='110',c='108'),)))
    assert result.state.opened is not None and result.state.opened.trailing==100
    assert not result.trades_json
    assert any(json.loads(e)['kind']=='PARTIAL_ENTRY_BAR_NOT_USED_FOR_TRAILING' for e in result.events_json)


def test_entry_on_funding_boundary_does_not_pay_funding_before_it_exists():
    state=initial(start=NOW+3540*NS)
    for second in range(10,60,10):state=observe(state,batch(state,second)).state
    row=FundingRecord(BTC,NOW+3600*NS,D('1'),D('100'),1)
    result=observe(state,batch(state,60,bars=(bar(state.started_at_ns//NS),),funding=(state.funding_anchor,row)))
    assert result.state.opened is not None and result.state.account.funding_paid==0
    kinds=[e.kind for e in result.journal]
    assert kinds.index('FUNDING_NO_POSITION')<kinds.index('FILL')


def test_short_receives_positive_funding_and_large_long_payment_can_liquidate():
    for side,rate in [('SHORT','1'),('LONG','100')]:
        start=NOW+3480*NS;state,_=enter(side,start=start)
        for second in range(70,120,10):state=observe(state,batch(state,second)).state
        row=FundingRecord(BTC,NOW+3600*NS,D(rate),D('100'),1)
        result=observe(state,batch(state,120,bid='90' if side=='SHORT' else '109',
            ask='91' if side=='SHORT' else '110',bars=(bar(start//NS+60,side),),funding=(state.funding_anchor,row)))
        assert_deltas(state.account,result)
        if side=='SHORT':
            assert result.state.account.funding_paid==D('-24.875')
            assert result.state.account.cash==D('1243.998875')
        else:
            assert result.state.status=='LIQUIDATED' and not any(e.kind=='FILL' for e in result.journal)
            assert result.state.account.funding_paid==D('2487.5')
            assert result.state.account.cash==D('500.0125')


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_same_explicit_events_match_the_shared_account_consumer(mode):
    state=initial(mode=mode)
    for second in range(10,60,10):state=observe(state,batch(state,second)).state
    incoming=batch(state,60,bars=(bar(NOW//NS),))
    result=observe(state,incoming)
    expected=mark_with_rules(state.account,Cursor(NOW+60*NS,0),{BTC:D('100')},{BTC:incoming.rules})
    ordered=submit_order(expected.account,Cursor(NOW+60*NS,1),identifier='observed-1',
        instrument=BTC,side='BUY',quantity=D('24.875'),price_bound=D('100'),reduce_only=False)
    filled=fill_order(ordered.account,Cursor(NOW+60*NS,2),'observed-1',quantity=D('24.875'),price=D('100'))
    assert result.state.account==filled.account
    assert result.journal==(*expected.entries,*ordered.entries,*filled.entries)
    intent=next(json.loads(e) for e in result.events_json if json.loads(e)['kind']=='ORDER_INTENT')
    assert intent['reason']=='ENTRY_SIGNAL' and intent['side']=='BUY'


def test_rule_metadata_cannot_roll_back_or_change_at_same_snapshot_time():
    state,_=enter();prior=state.account.markets[0].rules
    with pytest.raises(ObservedError,match='anteriores'):
        observe(state,batch(state,70,rules=replace(prior,fetched_at_ns=prior.fetched_at_ns-NS)))
    different=replace(prior,tiers=(replace(prior.tiers[0],maintenance_rate=D('0.06')),))
    with pytest.raises(ObservedError,match='contradictorias'):observe(state,batch(state,70,rules=different))
