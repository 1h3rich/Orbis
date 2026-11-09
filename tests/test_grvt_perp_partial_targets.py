"""Controlled GRVT facts for exact target sizing, retries and final-close priority."""
from dataclasses import replace
from decimal import Decimal as D, localcontext

import pytest

from app.lab.store import content_hash
from app.live_execution import grvt_perp_partial_targets as partial_targets
from app.live_execution.grvt_mainnet_reduction_order import sign_reduction, verify_reduction
from app.live_execution.grvt_mainnet_transport import TICKER_URL
from app.live_execution.grvt_perp_partial_targets import (
    PartialExitObservation, PerpPartialExitTerms, ReconciledReduction,
    decide_partial_exit, plan_partial_exit_order,
)
from tests.test_grvt_perp_limit_entry import CID, NS, source
from tests.test_grvt_perp_plan import NOW
from tests.test_grvt_testnet_signer import KEY, SIGNER


def terms_and_instrument(side='LONG', *, max_attempts=5, reserved=1, fractions=('.25','.35')):
    plan = source(side)
    original = plan.terms
    research = original.research.payload()
    profile = {**research['profile'],'schema':'orbis.lab.perp.profile.v3','partial_targets':[
        {'target':target,'fraction':fraction} for target,fraction in zip(('.02','.05'),fractions)]}
    research.update(schema='orbis.research.perp.reference.v3',profile=profile,profile_hash=content_hash(profile),
        validation_engine='perp-validation-3.0',simulation_engine='perp-ohlc-3.0')
    terms = PerpPartialExitTerms.model_validate({
        'schema':'orbis.grvt.perp.partial.exit.terms.v1','research':{**research,'sha256':content_hash(research)},
        'definition':original.definition,'paper':original.paper.model_dump(),'paper_partial_fills':2,
        'budget':original.budget.payload(),'max_attempts':max_attempts,'reserved_close_attempts':reserved,
        'execution_authority':'NONE'})
    return terms,plan.instrument.instrument()


def observation(side='LONG', **changes):
    quote = {'source':TICKER_URL,'instrument':'BTC_USDT_Perp','event_time_ns':NOW,'observed_at_ns':NOW,
        'bid':'103' if side=='LONG' else '96.9','ask':'103.1' if side=='LONG' else '97',
        'bid_size':'5','ask_size':'5'}
    return replace(PartialExitObservation(D('1.003'),D('100.3'),D('1.003') if side=='LONG' else D('-1.003'),
        NOW,(),None,quote,NOW),**changes)


def attempt(index, target, requested, filled):
    return ReconciledReduction(str(int(CID)+index),target,D(requested),D(filled) if filled is not None else None)


@pytest.mark.parametrize('side,price', [('LONG','102.75'),('SHORT','97.24')])
def test_signed_intent_uses_confirmed_entry_size_and_executable_side(side,price):
    terms,instrument = terms_and_instrument(side)
    point = observation(side)
    decision,template = plan_partial_exit_order(terms,instrument,point,client_order_id=CID,nonce=1,expiration_ns=NOW+10*NS)
    assert decision.action=='PARTIAL' and decision.target_index==1
    assert decision.quantity==D('.250') and decision.price_bound==D(price)
    assert template['reduce_only'] is True and template['time_in_force']=='IMMEDIATE_OR_CANCEL'
    assert template['legs']==[{'instrument':instrument.name,'size':'0.25','limit_price':price,
                              'is_buying_asset':side=='SHORT'}]
    signed = sign_reduction(template,instrument,private_key=KEY,expected_signer=SIGNER)
    assert verify_reduction(signed,template,instrument)==SIGNER
    with localcontext() as context:
        context.prec=6
        assert decide_partial_exit(terms,instrument,point)==decision


@pytest.mark.parametrize('side,price', [('LONG','102'),('SHORT','98')])
def test_crossing_compares_exact_entry_notional_without_rounded_vwap(side,price):
    terms,instrument = terms_and_instrument(side)
    point = observation(side)
    quote = {**point.quote,'bid':price,'ask':price}
    reference = D('100.3000001') if side=='LONG' else D('100.2999999')
    assert decide_partial_exit(terms,instrument,replace(point,quote=quote,entry_notional=reference)).action=='HOLD'
    assert decide_partial_exit(terms,instrument,replace(point,quote=quote)).action=='PARTIAL'


@pytest.mark.parametrize('side', ['LONG','SHORT'])
def test_reconciled_partial_ioc_and_zero_fill_stay_on_same_target(side):
    terms,instrument = terms_and_instrument(side)
    sign = 1 if side=='LONG' else -1
    point = observation(side,attempts=(attempt(1,1,'.250','.075'),attempt(2,1,'.175','0')),
        confirmed_position=sign*D('.928'))
    decision = decide_partial_exit(terms,instrument,point)
    assert (decision.action,decision.target_index,decision.quantity)==('PARTIAL',1,D('.175'))
    complete = replace(point,attempts=(*point.attempts,attempt(3,1,'.175','.175')),
        confirmed_position=sign*D('.753'))
    assert decide_partial_exit(terms,instrument,complete).reason=='TARGET_NOT_REACHED'
    price = '106' if side=='LONG' else '94'
    crossed = replace(complete,quote={**point.quote,'bid':price,'ask':price})
    decision = decide_partial_exit(terms,instrument,crossed)
    assert (decision.action,decision.target_index,decision.quantity)==('PARTIAL',2,D('.351'))


def test_lost_receipt_blocks_both_retry_and_protection_until_reconciled():
    terms,instrument = terms_and_instrument()
    pending = attempt(1,1,'.250',None)
    point = observation(attempts=(pending,),quote=None,confirmed_position=None,reconciled_at_ns=None,
        close_reason='STOP_OR_TRAILING')
    decision,template = plan_partial_exit_order(terms,instrument,point,client_order_id=CID,nonce=2,expiration_ns=NOW+10*NS)
    assert decision.action=='WAIT_RECEIPT' and decision.pending_id==pending.client_order_id and template is None
    settled = observation(attempts=(attempt(1,1,'.250','.075'),),confirmed_position=D('.928'),
        close_reason='STOP_OR_TRAILING')
    decision = decide_partial_exit(terms,instrument,settled)
    assert (decision.action,decision.target_index,decision.quantity)==('CLOSE',None,D('.928'))


@pytest.mark.parametrize('reason', ['STOP_OR_TRAILING','EXIT_RULE','TIME_LIMIT','DATA_GAP','MANUAL_CLOSE'])
def test_protection_takes_priority_over_a_crossed_target(reason):
    terms,instrument = terms_and_instrument()
    decision = decide_partial_exit(terms,instrument,observation(close_reason=reason))
    assert (decision.action,decision.reason,decision.quantity)==('CLOSE',reason,D('1.003'))


def test_last_attempts_remain_available_for_final_close_and_latch_after_a_fill():
    terms,instrument = terms_and_instrument(max_attempts=4,reserved=2)
    point = observation(attempts=(attempt(1,1,'.250','.075'),attempt(2,1,'.175','0')),
        confirmed_position=D('.928'))
    assert decide_partial_exit(terms,instrument,point).reason=='CLOSE_ATTEMPTS_RESERVED'
    assert decide_partial_exit(terms,instrument,replace(point,close_reason='TAKE_PROFIT')).quantity==D('.928')
    closing = replace(point,attempts=(*point.attempts,attempt(3,None,'.928','.5')),confirmed_position=D('.428'))
    result = decide_partial_exit(terms,instrument,closing)
    assert (result.action,result.reason,result.quantity)==('CLOSE','CLOSE_IN_PROGRESS',D('.428'))
    closed = replace(closing,attempts=(*closing.attempts,attempt(4,None,'.428','.428')),confirmed_position=D(0))
    assert decide_partial_exit(terms,instrument,closed).action=='FLAT'


@pytest.mark.parametrize('changes,reason', [
    ({'confirmed_position':D('-1.003')},'POSITION_MISMATCH'),
    ({'reconciled_at_ns':NOW-6*NS},'POSITION_STALE'),
    ({'quote':None},'QUOTE_UNAVAILABLE'),
])
def test_position_and_quote_are_required_before_emitting_an_order(changes,reason):
    terms,instrument = terms_and_instrument()
    point = observation(**changes)
    decision,template = plan_partial_exit_order(terms,instrument,point,client_order_id=CID,nonce=1,expiration_ns=NOW+10*NS)
    assert (decision.action,decision.reason,template)==('REVIEW',reason,None)


def test_quote_cannot_be_future_stale_or_older_than_reconciliation():
    terms,instrument = terms_and_instrument()
    point = observation()
    for quote,reason in [
        ({**point.quote,'event_time_ns':NOW+NS},'QUOTE_NOT_EXECUTABLE'),
        ({**point.quote,'event_time_ns':NOW-6*NS},'QUOTE_NOT_EXECUTABLE'),
        ({**point.quote,'event_time_ns':NOW-NS,'observed_at_ns':NOW-NS},'QUOTE_PRECEDES_RECONCILIATION'),
    ]:
        assert decide_partial_exit(terms,instrument,replace(point,quote=quote)).reason==reason


def test_quote_and_terms_used_to_construct_one_order_are_snapshots(monkeypatch):
    terms,instrument = terms_and_instrument()
    point = observation()
    actual_bound = partial_targets.bounded_reduction
    def mutate_original(*args,**kwargs):
        result = actual_bound(*args,**kwargs)
        point.quote.update(bid='100',ask='100')
        terms.definition['direction'] = 'SHORT'
        return result
    monkeypatch.setattr(partial_targets,'bounded_reduction',mutate_original)
    decision,template = plan_partial_exit_order(terms,instrument,point,client_order_id=CID,nonce=1,expiration_ns=NOW+10*NS)
    assert decision.action=='PARTIAL' and decision.price_bound==D('102.75')
    assert template['legs'][0]['is_buying_asset'] is False


@pytest.mark.parametrize('attempts', [
    (attempt(1,1,'.251','.100'),),
    (attempt(1,2,'.200','0'),),
    (attempt(1,1,'.250','.251'),),
    (attempt(1,1,'.250',None),attempt(2,1,'.250','0')),
    (attempt(1,None,'.500','0'),attempt(2,1,'.250','0')),
    (attempt(1,1,'.250','0'),attempt(1,1,'.250','0')),
    (attempt(1,1,'.250','0'),attempt(2,1,'.250','0'),attempt(3,1,'.250','0')),
])
def test_replay_rejects_duplicate_oversize_unordered_or_unreconciled_attempts(attempts):
    terms,instrument = terms_and_instrument(max_attempts=3)
    with pytest.raises(ValueError):
        decide_partial_exit(terms,instrument,observation(attempts=attempts))


def test_residual_and_order_caps_cannot_leave_known_dust():
    terms,instrument = terms_and_instrument()
    point = observation()
    high_minimum = replace(instrument,min_notional=D('80'))
    assert decide_partial_exit(terms,high_minimum,point).reason=='PARTIAL_OR_RESIDUAL_BELOW_MINIMUM'
    price = '2000'
    tight_bound = replace(point,quote={**point.quote,'bid':price,'ask':price},close_reason='TAKE_PROFIT',
        attempts=(attempt(1,1,'.250','0'),attempt(2,1,'.250','0'),attempt(3,1,'.250','0'),attempt(4,1,'.250','0')))
    decision = decide_partial_exit(terms,high_minimum,tight_bound)
    assert decision.action=='CLOSE' and decision.quantity==D('.962')
    assert decision.quantity*D(price)<=D('2000')
    assert (decision.remaining-decision.quantity)*decision.price_bound>=D('80')
