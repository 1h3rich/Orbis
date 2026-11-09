"""Native DCA signals with REAL integer budgets and receipt-month accounting."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal,localcontext
from uuid import uuid4

import pytest

from app.automation.dca import validate_dca
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_dca_plan import DexDcaAttempt,DexDcaCursor,DexDcaLimits,fold_attempts,plan_dca
from tests.test_dex_bot_plan import state,quote
from app.lab.engine import validate_candles

UNIT=10**18
NOW=datetime(2026,11,1,0,0,1,tzinfo=timezone.utc)


def rule(**changes):
    return validate_dca({'frequency':'monthly','amount':'10.000000000000000001','budget_total':'999999',
        'monthly_limit':'30','max_exposure':'1','fee_rate':'0.01','slippage_rate':'0.02',
        'dips':[{'drop_pct':'0.1','amount':'5'},{'drop_pct':'0.2','amount':'7'}],**changes})


def bars():
    boundary=int(NOW.timestamp())//86400*86400
    return validate_candles([{'t':boundary+(i-4)*86400,'o':'100','h':'101','l':'79','c':'80','v':'10'}
        for i in range(4)],'1d',now=int(NOW.timestamp()))[0]


def attempt(*,key='BASE:2026-11',kind='BASE',status='FINALIZED',amount=10*UNIT,at=NOW,executed_at=None,drop=None):
    return DexDcaAttempt(str(uuid4()),key,kind,int(at.timestamp())//86400*86400-86400,at,'100',drop,amount,status,
        (executed_at or at) if status in ('FINALIZED','REVERTED') else None,12345 if status in ('FINALIZED','REVERTED') else 0)


def planning_arguments(**changes):
    snapshot=state(block_at=NOW,observed_at=NOW)
    snapshot=replace(snapshot,policy_limits=replace(snapshot.policy_limits,expires_at=int(NOW.timestamp())+3600,
        max_buy_per_order=50*UNIT,buy_budget=100*UNIT,max_exposure=100*UNIT,allow_sell=False))
    values=dict(definition=rule(),candles=bars(),cursor=DexDcaCursor(bars()[-2].t,NOW-timedelta(seconds=1),'100'),
        snapshot=snapshot,quote=quote('80',event_at=NOW,observed_at=NOW),
        limits=DexDcaLimits(12*UNIT,60*UNIT,25*UNIT),attempts=(),skip_through_t=bars()[-2].t,now=NOW)
    values.update(changes);return values


def plan(**changes):return plan_dca(**planning_arguments(**changes))


def test_dca_base_uses_exact_real_capital_and_ignores_historical_fictitious_funds():
    result=plan()
    assert result.action=='BUY' and result.reason=='DCA_BASE' and result.amount_units==10*UNIT+1
    assert result.dca['key']=='BASE:2026-11' and result.dca['requested_units']==str(10*UNIT+1)
    same=plan(definition=rule(budget_total='1',fee_rate='0.5',slippage_rate='0.5'))
    assert same.amount_units==result.amount_units
    with localcontext() as context:
        context.prec=6
        assert plan().json()==result.json()


def test_base_and_distinct_dips_can_run_on_the_same_new_closed_bar_without_duplicate_keys():
    base=attempt();first=plan(attempts=(base,))
    assert first.action=='BUY' and first.reason=='DCA_DIP' and first.dca['key']=='DIP:0.1' and first.amount_units==5*UNIT
    dip=attempt(key='DIP:0.1',kind='DIP',amount=5*UNIT,drop='0.1')
    second=plan(attempts=(base,dip))
    assert second.dca['key']=='DIP:0.2' and second.amount_units==7*UNIT
    last=attempt(key='DIP:0.2',kind='DIP',amount=7*UNIT,drop='0.2')
    assert plan(attempts=(base,dip,last)).action=='WAIT'


def test_dex_volume_filter_uses_the_threshold_and_leaves_the_requested_amount_unchanged():
    definition = rule(volume_gate={"period":2,"min_ratio":"2"})
    candles=bars()
    rejected=plan(definition=definition,candles=candles)
    assert rejected.action=='WAIT' and rejected.reason=='DCA_VOLUME_BELOW_MINIMUM'
    assert rejected.amount_units==0 and rejected.dca is None
    assert rejected.evidence['volume']['volume_ratio']=='1'
    candles[-1]=replace(candles[-1],v=Decimal(20))
    matched=plan(definition=definition,candles=candles)
    assert matched.action=='BUY' and matched.dca['kind']=='BASE'
    assert matched.dca['requested_units']==str(10*UNIT+1) and matched.amount_units==10*UNIT+1
    assert matched.evidence['volume']=={'volume_period':2,'volume_min_ratio':'2','volume_ratio':'2'}


def test_dex_ema_amount_rounds_down_and_keeps_approved_integer_caps():
    adjustment = {'kind':'ema','period':2,'condition':'below','matched_factor':'0.5','unmatched_factor':'0'}
    closed = bars()
    closed[-2] = replace(closed[-2], c=closed[-2].c + 10)
    filtered = plan(definition=rule(ma_adjustment=adjustment), candles=closed)
    assert filtered.action == 'BUY' and filtered.amount_units == 5*UNIT
    assert filtered.evidence['source_engine'] == 'dca-1.8'
    assert filtered.evidence['average']['amount_factor'] == '0.5'
    assert filtered.dca['requested_quote'] == '5.0000000000000000005'
    assert filtered.dca['requested_units'] == str(5*UNIT)
    assert filtered.dca['base_requested'] == '10.000000000000000001'
    cap = DexDcaLimits(3*UNIT,60*UNIT,25*UNIT)
    assert plan(definition=rule(ma_adjustment=adjustment), candles=closed, limits=cap).amount_units == 3*UNIT
    dip = plan(definition=rule(ma_adjustment=adjustment), candles=closed, attempts=(attempt(amount=5*UNIT),))
    assert dip.reason == 'DCA_DIP' and dip.amount_units == 25*UNIT//10


def test_dex_average_waits_without_consuming_activation_on_equality_or_subunit_amount():
    adjustment = {'kind':'ema','period':2,'condition':'above','matched_factor':'1.5','unmatched_factor':'0'}
    definition = rule(ma_adjustment=adjustment)
    equal = plan(definition=definition)
    assert equal.action == 'WAIT' and equal.reason == 'DCA_MA_ZERO_AMOUNT' and equal.dca is None
    assert equal.evidence['average']['ma_matched'] is False
    adjusted = adjustment | {'matched_factor':'0.0000000000000000001'}
    closed = bars()
    closed[-1] = replace(closed[-1], c=closed[-1].c + 1)
    too_small = plan(definition=rule(amount='1',dips=[],ma_adjustment=adjusted), candles=closed)
    assert too_small.action == 'WAIT' and too_small.reason == 'DCA_AMOUNT_BELOW_UNIT'
    recovered = plan(definition=definition, candles=closed)
    assert recovered.action == 'BUY' and recovered.dca['key'] == 'BASE:2026-11'


def test_dex_sma_filter_blocks_buy_and_uses_existing_budget_after_match():
    gate = {'period':2,'condition':'below'}
    assert plan(definition=rule(sma_gate=gate)).reason == 'DCA_SMA_CONDITION_NOT_MET'
    closed = bars()
    closed[-2] = replace(closed[-2], c=closed[-2].c + 2)
    accepted = plan(definition=rule(sma_gate=gate), candles=closed)
    assert accepted.action == 'BUY' and accepted.amount_units == 10*UNIT+1
    assert accepted.evidence['source_engine'] == 'dca-1.7'
    assert accepted.evidence['average']['sma_value'] == '81'


def test_monthly_room_uses_actual_receipt_date_and_does_not_count_native_gas_as_usdt():
    created=NOW-timedelta(seconds=2)
    settled=attempt(key='BASE:2026-10',at=created,amount=24*UNIT,executed_at=NOW)
    projection=fold_attempts((settled,),definition=rule(),now=NOW)
    assert projection.total_spent_units==24*UNIT and projection.monthly_spent_units=={'2026-11':24*UNIT}
    assert projection.gas_spent_wei==12345
    result=plan(attempts=(settled,))
    assert result.action=='BUY' and result.amount_units==UNIT
    assert result.evidence['monthly_remaining_units']==str(UNIT)


def test_pending_attempt_blocks_every_new_activation_and_revert_consumes_only_key_and_gas():
    assert plan(attempts=(attempt(status='PENDING'),)).reason=='ORDER_PENDING'
    reverted=attempt(status='REVERTED')
    projection=fold_attempts((reverted,),definition=rule(),now=NOW)
    assert projection.total_spent_units==0 and projection.gas_spent_wei==12345
    assert 'BASE:2026-11' in projection.consumed_keys
    assert plan(attempts=(reverted,)).dca['key']=='DIP:0.1'


@pytest.mark.parametrize('change,reason',[
    ({'skip_through_t':bars()[-1].t},'DCA_WAITING_NEXT_CANDLE'),
    ({'now':NOW+timedelta(seconds=31)},'OBSERVATION_GAP'),
    ({'quote':quote('80',event_at=NOW-timedelta(seconds=16),observed_at=NOW)},'REFERENCE_QUOTE_STALE'),
    ({'limits':DexDcaLimits(12*UNIT,60*UNIT,0)},None),
])
def test_expired_missing_or_invalid_inputs_never_buy(change,reason):
    if reason is None:
        with pytest.raises(DexExecutorError):plan(**change)
    else:
        result=plan(**change);assert result.action in ('WAIT','PAUSE') and result.reason==reason


def test_budget_and_dip_limits_survive_a_later_daily_observation():
    first=attempt(amount=10*UNIT+1)
    second=attempt(key='DIP:0.1',kind='DIP',amount=5*UNIT,drop='0.1')
    result=plan(attempts=(first,second),limits=DexDcaLimits(12*UNIT,16*UNIT,25*UNIT))
    assert result.action=='BUY' and result.amount_units==UNIT-1
    assert result.dca['key']=='DIP:0.2'
    capped=plan(attempts=(first,second),definition=rule(max_extra_activations=1))
    assert capped.action=='WAIT'


def test_duplicate_keys_and_incoherent_receipts_are_rejected_instead_of_silently_recounted():
    good=attempt()
    for records in ((good,attempt()),(replace(good,amount_units=True),),
                    (replace(good,executed_at=NOW+timedelta(seconds=1)),),
                    (replace(good,key='BASE:2026-10'),),(replace(good,gas_fee_wei=0),)):
        with pytest.raises(DexExecutorError):fold_attempts(records,definition=rule(),now=NOW)


def test_high_watermark_and_drop_cooldown_use_own_observations():
    base=attempt();dip=attempt(key='DIP:0.1',kind='DIP',amount=5*UNIT,drop='0.1')
    assert plan(attempts=(base,dip),definition=rule(cooldown_bars=1)).action=='WAIT'
    reference=plan(definition=rule(reference_mode='high_watermark'),
        cursor=DexDcaCursor(bars()[-2].t,NOW-timedelta(seconds=1),'70'))
    assert reference.reference=='80'
    initial=plan(cursor=DexDcaCursor(bars()[-2].t,NOW-timedelta(seconds=1),'70'))
    assert initial.reference=='70'


@pytest.mark.parametrize('frequency,key',[('daily','BASE:2026-11-01'),('weekly','BASE:2026-W44'),('monthly','BASE:2026-11')])
def test_period_keys_follow_native_utc_calendar(frequency,key):
    assert plan(definition=rule(frequency=frequency)).dca['key']==key


def test_never_sent_abandonment_releases_only_its_own_key():
    abandoned=attempt(status='ABANDONED_UNSENT')
    assert plan(attempts=(abandoned,)).dca['key']=='BASE:2026-11'
    assert plan(attempts=(abandoned,attempt())).dca['key']=='DIP:0.1'


def test_reference_exposure_cap_uses_real_allocated_equity_and_exact_token_floor():
    from decimal import Decimal
    result=plan(definition=rule(max_exposure='0.1'))
    assert 6*UNIT<result.amount_units<7*UNIT
    with localcontext() as context:
        context.prec=300
        marked=Decimal(result.amount_units)*Decimal(80)/Decimal('80.01')
        equity=Decimal(60*UNIT-result.amount_units)+marked
        assert marked/equity<=Decimal('0.1')
        marked_next=Decimal(result.amount_units+1)*Decimal(80)/Decimal('80.01')
        assert marked_next/(Decimal(60*UNIT-result.amount_units-1)+marked_next)>Decimal('0.1')


def test_verified_dex_quote_must_preserve_decision_amount_exposure_and_execution_month():
    from app.live_execution.dex_dca_plan import DexDcaExecutionQuote,validate_execution_quote
    from decimal import Decimal
    expected=plan(definition=rule(max_exposure='0.1'))
    out=int(Decimal(expected.amount_units)/Decimal('80.01'))
    execution=DexDcaExecutionQuote(True,expected.amount_units,out,int(NOW.timestamp())+60,NOW)
    # The helper recomputes the decision, rather than trusting its public JSON.
    values=planning_arguments(definition=rule(max_exposure='0.1'))
    assert validate_execution_quote(execution,**values).json()==expected.json()
    for changed in (replace(execution,buy=False),replace(execution,amount_units=execution.amount_units+1),
                    replace(execution,quote_out_units=out+UNIT),replace(execution,deadline=int(NOW.timestamp())+31*86400),
                    replace(execution,observed_at=NOW-timedelta(seconds=1))):
        with pytest.raises(DexExecutorError):validate_execution_quote(changed,**values)


def test_reference_cannot_shrink_below_a_previous_activation_after_rearming():
    values={'attempts':(attempt(),),'cursor':DexDcaCursor(bars()[-2].t,NOW-timedelta(seconds=1),'70')}
    for mode in ('initial','high_watermark'):
        with pytest.raises(DexExecutorError):plan(**values,definition=rule(reference_mode=mode))


def test_new_month_restores_only_monthly_room_without_replenishing_lifetime_budget():
    previous=attempt(key='BASE:2026-10',at=NOW-timedelta(days=1),amount=55*UNIT)
    result=plan(attempts=(previous,))
    assert result.action=='BUY' and result.amount_units==5*UNIT
    assert result.evidence['total_remaining_units']==str(5*UNIT)
    assert result.evidence['monthly_remaining_units']==str(25*UNIT)


def test_changed_policy_or_uncovered_position_pauses_before_generating_an_order():
    values=planning_arguments();original=values['snapshot']
    for snapshot,reason in (
        (replace(original,current_operator_code_empty=False),'POLICY_UNAVAILABLE'),
        (replace(original,policy_limits=replace(original.policy_limits,allow_sell=True)),'DCA_POLICY_NOT_BUY_ONLY'),
        (replace(original,owned_wbnb=UNIT,cost_usdt=UNIT,acquired_wbnb=UNIT,balance_wbnb=0),'POSITION_NOT_COVERED'),
        (replace(original,buy_spent=100*UNIT),'DCA_BUDGET_OR_EXPOSURE')):
        result=plan(snapshot=snapshot);assert result.action in ('WAIT','PAUSE') and result.reason==reason


def test_dip_waits_for_contract_cooldown_then_can_activate_on_the_same_closed_day():
    values=planning_arguments();base=attempt();state=values['snapshot']
    state=replace(state,last_entry_at=int(NOW.timestamp()),entries=1,buy_spent=10*UNIT,
        owned_wbnb=UNIT//10,cost_usdt=10*UNIT,acquired_wbnb=UNIT//10)
    for seconds,reason in ((59,'ENTRY_COOLDOWN'),(61,'DCA_DIP')):
        at=NOW+timedelta(seconds=seconds)
        result=plan(attempts=(base,),snapshot=replace(state,block_at=at,observed_at=at),
            quote=quote('80',event_at=at,observed_at=at),cursor=DexDcaCursor(bars()[-1].t,at-timedelta(seconds=1),'100'),now=at)
        assert result.reason==reason
        assert result.action==('BUY' if seconds==61 else 'WAIT')


def test_expired_activation_window_cannot_be_revived_by_fresh_prices_or_rearming():
    at=NOW+timedelta(seconds=901);values=planning_arguments()
    result=plan(now=at,snapshot=replace(values['snapshot'],observed_at=at,block_at=at),
        quote=quote('80',event_at=at,observed_at=at),cursor=DexDcaCursor(bars()[-1].t,at-timedelta(seconds=1),'100'))
    assert result.action=='WAIT' and result.reason=='DCA_SIGNAL_EXPIRED'


def test_a_receipt_available_in_the_observed_block_uses_only_bounded_chain_clock_skew():
    fact=attempt(executed_at=NOW+timedelta(seconds=1));values=planning_arguments()
    with pytest.raises(DexExecutorError):fold_attempts((fact,),definition=rule(),now=NOW)
    result=plan(attempts=(fact,),snapshot=replace(values['snapshot'],block_at=NOW+timedelta(seconds=1)))
    assert result.action=='BUY' and result.dca['key']=='DIP:0.1'
    future=replace(fact,decided_at=NOW+timedelta(seconds=1))
    with pytest.raises(DexExecutorError):plan(attempts=(future,),snapshot=replace(values['snapshot'],block_at=NOW+timedelta(seconds=1)))
    with pytest.raises(DexExecutorError):plan(attempts=(fact,),snapshot=replace(values['snapshot'],block_at=NOW+timedelta(seconds=16)))


def test_dca_trade_deadline_is_clipped_to_the_remaining_activation_window():
    from app.live_execution.dex_dca_plan import execution_ttl
    values=planning_arguments();at=NOW+timedelta(seconds=879)
    values.update(now=at,snapshot=replace(values['snapshot'],observed_at=at,block_at=at),
        quote=quote('80',event_at=at,observed_at=at),cursor=DexDcaCursor(bars()[-1].t,at-timedelta(seconds=1),'100'))
    decision=plan_dca(**values);assert decision.action=='BUY'
    assert execution_ttl(decision,now=at,policy_expires_at=values['snapshot'].policy_limits.expires_at)==20
    end=at+timedelta(seconds=20)
    values.update(now=end,snapshot=replace(values['snapshot'],observed_at=end,block_at=end),
        quote=quote('80',event_at=end,observed_at=end),cursor=DexDcaCursor(bars()[-1].t,end-timedelta(seconds=1),'100'))
    assert plan_dca(**values).reason=='DCA_SIGNAL_EXPIRED'


def test_a_chain_clock_in_the_next_month_waits_without_spending_or_stopping_the_bot():
    values=planning_arguments();at=NOW-timedelta(seconds=2);history=bars()[:-1]
    values.update(candles=history,now=at,snapshot=replace(values['snapshot'],observed_at=at,block_at=NOW),
        quote=quote('80',event_at=at,observed_at=at),cursor=DexDcaCursor(history[-1].t,at-timedelta(seconds=1),'100'),
        limits=DexDcaLimits(12*UNIT,60*UNIT,25*UNIT,86400),skip_through_t=history[-2].t)
    result=plan_dca(**values)
    assert result.action=='WAIT' and result.reason=='DCA_PERIOD_BOUNDARY_SKEW' and result.amount_units==0
