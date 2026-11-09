"""Independent Decimal oracles for real-cash sizing, never PAPER balances."""
from decimal import Decimal as D,localcontext,InvalidOperation

import pytest

from app.derivatives.perp_observed_wire import QuoteInput,rules_input,specification_input
from app.derivatives.paper_math import InstrumentSpec,MarginRules
from app.lab.store import content_hash
from app.live_execution.grvt_perp_plan import (
    PerpRealBudget,PerpEntryTerms,PerpInstrument,PerpPlanInput,PerpEntryPlan,build_perp_plan,
)
from tests.perp_simulation_fixture import definition,profile


NOW=1_800_000_000_000_000_000


def plan_source(direction='LONG',mode='ISOLATED',**changes):
    strategy=definition(direction,fee_rate='0',allocation='0.5')
    budget=PerpRealBudget(schema_name='orbis.grvt.perp.real.budget.v1',sub_account_id='123456',
        collateral_limit='1000',entry_cash_limit='25',entry_notional_limit='1000',exit_notional_limit='2000',
        fee_rate_ceiling='0',exit_seconds=3600,max_spread_bps=25,max_slippage_bps=25,
        collateral_scope='WHOLE_DEDICATED_USDT_SUBACCOUNT')
    terms=PerpEntryTerms(schema_name='orbis.grvt.perp.entry.terms.v1',instrument='BTC_USDT_Perp',timeframe='1h',profile=profile(strategy,margin_mode=mode),
        definition=strategy,budget=budget,execution_mapping='BOUNDED_FOK_WITH_NATIVE_LAST_BRACKETS',
        fee_assumption='USER_CEILING_NOT_VERIFIED_EXCHANGE_FEE',reserve_assumption='SHARED_BOOK_ESTIMATE_NOT_EXCHANGE_APPROVAL',
        execution_scope='PROTECTED_ENTRY_ONLY_NOT_SIGNAL_OR_EXIT_COORDINATOR')
    rules=MarginRules.from_payload({'instrument':'BTC_USDT_Perp','fetched_at_ns':NOW,'max_position_size':'100',
        'risk_brackets':[{'tier':1,'notional_floor':'0','notional_cap':None,'maintenance_margin_rate':'0.01',
            'initial_margin_rate':'0.02','max_leverage':'50','cumulative_maintenance_amount':'0'}]},'BTC_USDT_Perp',NOW)
    spec=InstrumentSpec('BTC_USDT_Perp',NOW,D('.001'),D('.001'),D('10'),D('.01'),None)
    raw={'terms':terms.payload(),'instrument':{'name':'BTC_USDT_Perp','asset_id':'123456789','base_decimals':3,
        'tick_size':'.01','min_size':'.001','min_notional':'10'},'specification':specification_input(spec).payload(),
        'rules':rules_input(rules).model_dump(),'quote':{'instrument':'BTC_USDT_Perp','event_time_ns':str(NOW),
        'bid':'100','ask':'100','mark_price':'100'},'account_equity':'100','checked_at_ns':str(NOW)}
    raw.update(changes)
    return PerpPlanInput.model_validate(raw)


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_real_budget_not_paper_capital_or_full_account_determines_quantity(direction,mode):
    source=plan_source(direction,mode)
    result=build_perp_plan(source).result
    assert source.terms.definition['initial_capital']=='1000'
    assert result['account_equity']=='100' and result['entry_cash_budget']=='25'
    # 25 / (100 / 5) = 1.25, minus a lot for the 2 settlement-quanta buffer.
    assert D(result['draft']['size'])==D('1.249')
    assert D(result['entry_notional'])==D('124.9')
    assert D(result['cash_required_estimate'])==D('24.980000000000000002')
    assert D(result['draft']['stop_loss'])==(D(97) if direction=='LONG' else D(103))
    assert D(result['draft']['take_profit'])==(D(108) if direction=='LONG' else D(92))
    assert result['execution_authority']=='NONE'
    larger=source.payload();larger['account_equity']='1000'
    assert build_perp_plan(PerpPlanInput.model_validate(larger)).result['draft']==result['draft']


@pytest.mark.parametrize('direction,price',[('LONG','101'),('SHORT','99')])
def test_adverse_mark_loss_and_fee_reserve_are_included(direction,price):
    raw=plan_source(direction).payload()
    raw['terms']['definition']['fee_rate']='0.001'
    raw['terms']['profile']['strategy_hash']=content_hash(raw['terms']['definition'])
    raw['terms']['budget'].update(fee_rate_ceiling='0.001',max_spread_bps=100,max_slippage_bps=100)
    raw['quote'].update(bid='99.5',ask='100.5')
    # Use a 1% displacement from MARK without violating the bid/ask spread cap.
    raw['quote'].update(bid=price,ask=price)
    result=build_perp_plan(PerpPlanInput.model_validate(raw)).result
    with localcontext() as context:
        context.prec=100
        expected=(D(25)/(D(20)+D(1)+D(price)*D('.001'))//D('.001'))*D('.001')
        assert D(result['draft']['size'])==expected
        assert D(result['adverse_mark_buffer'])==expected
        assert D(result['fee_reserve'])==expected*D(price)*D('.001')
        assert D(result['cash_required_estimate'])==expected*20+expected+expected*D(price)*D('.001')+D('2e-18')


def test_notional_and_native_exit_caps_are_applied_before_tier_sizing():
    raw=plan_source().payload();raw['terms']['budget'].update(entry_notional_limit='50',exit_notional_limit='50')
    result=build_perp_plan(PerpPlanInput.model_validate(raw)).result
    assert D(result['draft']['size'])==D('.462')  # 50 / 108 take-profit cap, lot .001.
    assert D(result['entry_notional'])<=50
    assert D(result['draft']['take_profit'])*D(result['draft']['size'])<=50


def test_notional_cap_must_not_clip_a_supported_tier_into_an_unsupported_one():
    raw=plan_source().payload();first=raw['rules']['risk_brackets'][0]
    raw['rules']['risk_brackets']=[{**first,'notional_cap':'100','initial_margin_rate':'0.5','max_leverage':'2'},
        {**first,'tier':2,'notional_floor':'100','initial_margin_rate':'0.2','max_leverage':'5'}]
    assert D(build_perp_plan(PerpPlanInput.model_validate(raw)).result['draft']['size'])>=1
    raw['terms']['budget']['entry_notional_limit']='50'
    with pytest.raises(ValueError,match='apalancamiento'):
        build_perp_plan(PerpPlanInput.model_validate(raw))


@pytest.mark.parametrize('mutation',[
    lambda x:x.update(account_equity='1001'),lambda x:x.update(account_equity=100),
    lambda x:x['quote'].update(event_time_ns=str(NOW+1)),
    lambda x:x['quote'].update(event_time_ns=str(NOW-5_000_000_001)),
    lambda x:x['specification'].update(size_step='.01'),
    lambda x:x['terms']['budget'].update(collateral_scope='ENTRY_BUDGET_ONLY'),
    lambda x:x['terms']['budget'].update(entry_notional_limit='1001'),
    lambda x:x['terms']['definition'].update(initial_capital='2000'),
    lambda x:x['terms']['profile'].update(leverage='100'),
    lambda x:x['terms']['budget'].update(entry_cash_limit='0.001'),
    lambda x:x['instrument'].update(asset_id='0'),
])
def test_unsupported_scope_precision_freshness_budget_or_leverage_never_silently_adjusts(mutation):
    raw=plan_source().payload();mutation(raw)
    with pytest.raises((ValueError,RuntimeError)):build_perp_plan(PerpPlanInput.model_validate(raw))


def test_recomputed_digest_cannot_forge_quantity_and_caller_precision_does_not_change_plan():
    source=plan_source();result=build_perp_plan(source);raw=result.payload()
    raw['result']['draft']['size']='9'
    raw['sha256']=content_hash({key:value for key,value in raw.items() if key!='sha256'})
    with pytest.raises(ValueError):PerpEntryPlan.model_validate(raw)
    with localcontext() as context:
        context.prec=4;context.traps[InvalidOperation]=True
        actual=build_perp_plan(PerpPlanInput.model_validate(source.payload()))
    assert actual.payload()==result.payload()
