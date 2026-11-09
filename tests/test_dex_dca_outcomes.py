"""DCA v2 distinguishes successful cancellation from reverted execution."""
from dataclasses import replace
from datetime import timedelta

import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_dca_plan import ENGINE,OUTCOME_ENGINE,fold_attempts,plan_dca,execution_ttl,DexDcaExecutionQuote,validate_execution_quote
from tests.test_dex_dca_plan import attempt,rule,planning_arguments,NOW,UNIT


def cancelled(**changes):
    return replace(attempt(status='REVERTED'),status='CANCELLED',**changes)


def test_v1_stays_strict_and_v2_counts_cancelled_key_and_gas_without_token_spend():
    fact=cancelled()
    with pytest.raises(DexExecutorError):fold_attempts((fact,),definition=rule(),now=NOW)
    projected=fold_attempts((fact,),definition=rule(),now=NOW,engine=OUTCOME_ENGINE)
    assert projected.total_spent_units==0 and not projected.monthly_spent_units and not projected.pending
    assert projected.gas_spent_wei==12345 and 'BASE:2026-11' in projected.consumed_keys
    plan=plan_dca(**planning_arguments(attempts=(fact,)),engine=OUTCOME_ENGINE)
    assert plan.reason=='DCA_DIP' and plan.dca['key']=='DIP:0.1' and plan.evidence['engine']==OUTCOME_ENGINE


def test_cancelled_dip_does_not_reset_extra_activation_or_current_month_budgets():
    base=attempt(amount=24*UNIT)
    dip=cancelled(key='DIP:0.1',kind='DIP',drop_pct='0.1',amount_units=5*UNIT)
    projection=fold_attempts((base,dip),definition=rule(),now=NOW,engine=OUTCOME_ENGINE)
    assert projection.total_spent_units==24*UNIT and projection.monthly_spent_units=={'2026-11':24*UNIT}
    assert projection.gas_spent_wei==24690 and projection.triggered==frozenset({'0.1'})
    plan=plan_dca(**planning_arguments(attempts=(base,dip)),engine=OUTCOME_ENGINE)
    assert plan.dca['key']=='DIP:0.2' and plan.amount_units==UNIT
    assert plan_dca(**planning_arguments(attempts=(base,dip),definition=rule(max_extra_activations=1)),engine=OUTCOME_ENGINE).action=='WAIT'


@pytest.mark.parametrize('changes',[{'gas_fee_wei':0},{'gas_fee_wei':True},{'executed_at':None},{'executed_at':NOW+timedelta(seconds=1)}])
def test_a_cancelled_attempt_needs_an_actual_consistent_receipt(changes):
    with pytest.raises(DexExecutorError):fold_attempts((cancelled(**changes),),definition=rule(),now=NOW,engine=OUTCOME_ENGINE)


def test_v2_execution_quote_and_expiry_preserve_the_same_decision_constraints():
    planning=planning_arguments(attempts=(cancelled(),));planning['engine']=OUTCOME_ENGINE
    plan=plan_dca(**planning);assert plan.action=='BUY'
    execution=DexDcaExecutionQuote(True,plan.amount_units,plan.amount_units//80,int(NOW.timestamp())+60,NOW)
    assert validate_execution_quote(execution,**planning)==plan
    assert execution_ttl(plan,now=NOW,policy_expires_at=int(NOW.timestamp())+3600)==60
    with pytest.raises(DexExecutorError):validate_execution_quote(replace(execution,amount_units=plan.amount_units+1),**planning)
    with pytest.raises(DexExecutorError):fold_attempts((cancelled(),),definition=rule(),now=NOW,engine='unknown')
