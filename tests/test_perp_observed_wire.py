"""Typed source events, exact timestamps and replay-only checkpoints."""
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.derivatives.perp_observed_wire import (
    INPUT,CloseEvent,GapEvent,ObservedSeed,StopEvent,apply_input,checkpoint,
    funding_input,observation_input,quote_input,rules_input,specification_input,transition_evidence,ObservedRiskSnapshot,
)
from app.lab.perp_dataset import TradeBar
from tests.perp_observed_fixture import NOW,NS,bar,batch,initial


def seed_from_initial(state):
    market=state.account.markets[0]
    return ObservedSeed(schema_name='orbis.paper.perp.observed.seed.v1',engine='perp-observed-1.0',
        profile=state.profile,definition=json.loads(state.definition_json),timeframe=state.timeframe,
        received_at_ns=str(state.started_at_ns),quote=quote_input(state.last_quote),
        rules=rules_input(market.rules),specification=specification_input(market.specification),
        history=[TradeBar.model_validate(b.json()) for b in state.history],funding=[funding_input(state.funding_anchor)])


def test_seed_and_all_input_kinds_roundtrip_without_binary_money_or_time():
    state=initial();seed=seed_from_initial(state)
    restored=ObservedSeed.model_validate(json.loads(json.dumps(seed.payload())))
    assert restored.start().state==state
    incoming=observation_input(batch(state,10))
    parsed=INPUT.validate_python(json.loads(json.dumps(incoming.payload())))
    assert parsed.batch()==incoming.batch()
    assert apply_input(state,parsed).state.last_received_ns==NOW+10*NS
    for model,kind,extra in [(StopEvent,'STOP',{}),(CloseEvent,'CLOSE_REQUEST',{}),(GapEvent,'GAP',{'reason':'SERVICE_SHUTDOWN'})]:
        value=model(schema_name='orbis.paper.perp.observed.input.v1',kind=kind,received_at_ns=str(NOW+NS),**extra)
        assert INPUT.validate_python(value.payload()).payload()==value.payload()


def test_checkpoint_binds_history_but_does_not_serialize_the_growing_array():
    state=initial();transition=seed_from_initial(state).start()
    chain,evidence=transition_evidence(None,transition,None)
    assert 'bars' not in evidence['checkpoint']['history']
    assert evidence['checkpoint']['history']['count']==3
    assert evidence['checkpoint']['account']['cursor']['time_ns']==str(NOW)
    next_transition=apply_input(state,observation_input(batch(state,10)))
    next_chain,result=transition_evidence(state,next_transition,chain)
    assert next_chain==chain and result['checkpoint']['last_received_ns']==str(NOW+10*NS)
    with pytest.raises(TypeError):json.dumps(state)  # state cannot be accepted as JSON authority


@pytest.mark.parametrize('field,value',[
    ('received_at_ns',True),('received_at_ns',str(NOW)+'.0'),('received_at_ns','0'),
    ('kind','REAL_ORDER'),('extra','secret'),
])
def test_event_boundary_rejects_ambiguous_time_actions_and_unknown_fields(field,value):
    raw=observation_input(batch(initial(),10)).payload();raw[field]=value
    with pytest.raises(ValueError):INPUT.validate_python(raw)


def test_mutated_nested_seed_or_future_candles_are_revalidated():
    seed=seed_from_initial(initial());seed.definition['initial_capital']='999999'
    with pytest.raises(ValueError):seed.start()
    state=initial();raw=observation_input(batch(state,10)).payload();raw['closed_candles']=[bar(NOW//NS).json()]
    with pytest.raises(ValueError):apply_input(state,INPUT.validate_python(raw))


def test_fractional_profile_leverage_and_nondecimal_power_lot_step_roundtrip_exactly():
    state=initial();market=state.account.markets[0]
    spec=replace(market.specification,size_step=D('0.003'),min_size=D('0.003'))
    market=replace(market,leverage=D('4.5'),specification=spec)
    profile=type(state.profile).model_validate({**state.profile.payload(),'leverage':'4.5'})
    state=replace(state,profile=profile,account=replace(state.account,markets=(market,)))
    seed=seed_from_initial(state)
    assert seed.profile.leverage=='4.5' and seed.rules.risk_brackets[0].max_leverage=='5'
    assert seed.specification.size_step=='0.003'
    assert ObservedSeed.model_validate(seed.payload()).start().state==state


def test_fractional_public_rule_maximum_is_rejected_without_truncation():
    raw=rules_input(initial().account.markets[0].rules).model_dump()
    raw['risk_brackets'][0]['max_leverage']='5.5'
    with pytest.raises(ValueError):ObservedRiskSnapshot.model_validate(raw)


@pytest.mark.parametrize('field,value',[
    ('max_leverage',5.5),('max_leverage',5.0),('max_leverage',True),
    ('tier',1.5),('tier',True),('fetched_at_ns',float(NOW)),('fetched_at_ns',True),
])
def test_shared_public_metadata_reader_never_truncates_or_rounds_integer_fields(field,value):
    from app.derivatives.paper_math import MarginRules,PaperPerpError
    raw=initial().account.markets[0].rules.json()
    if field=='fetched_at_ns':raw[field]=value
    else:raw['risk_brackets'][0][field]=value
    with pytest.raises(PaperPerpError):MarginRules.from_payload(raw,raw['instrument'],NOW)


def test_optional_collection_time_preserves_old_payload_and_identical_financial_state():
    state=initial();old=observation_input(batch(state,10));raw=old.payload()
    assert 'collected_at_ns' not in raw and INPUT.validate_python(raw).payload()==raw
    new=INPUT.validate_python({**raw,'collected_at_ns':raw['received_at_ns']})
    previous,current=apply_input(state,old),apply_input(state,new)
    assert previous.state==current.state and previous.journal==current.journal
    assert json.loads(current.events_json[0])['kind']=='QUOTE_ACCEPTED'


@pytest.mark.parametrize('future',['quote','rules','funding'])
def test_later_consumer_acceptance_cannot_validate_future_data_at_collection(future):
    state=initial();raw=observation_input(batch(state,10)).payload();collected=int(raw['received_at_ns'])
    raw.update(collected_at_ns=str(collected),received_at_ns=str(collected+10))
    if future=='quote':raw['quote']['event_time_ns']=str(collected+1)
    if future=='rules':raw['rules']['fetched_at_ns']=str(collected+1)
    if future=='funding':raw['funding'][-1]['funding_time']=str(collected+1)
    with pytest.raises(ValueError):apply_input(state,INPUT.validate_python(raw))


def test_candle_that_closed_after_collection_is_not_made_valid_by_acceptance_delay():
    state=initial();raw=observation_input(batch(state,60,bars=(bar(NOW//NS),))).payload()
    collected=int(raw['received_at_ns'])-1;raw['collected_at_ns']=str(collected)
    raw['quote']['event_time_ns']=str(collected)
    for key in ('rules','specification'):raw[key]['fetched_at_ns']=str(collected)
    with pytest.raises(ValueError,match='vela todavía abierta'):apply_input(state,INPUT.validate_python(raw))


def test_collection_older_than_freshness_window_is_rejected():
    state=initial();raw=observation_input(batch(state,10)).payload()
    raw['collected_at_ns']=str(int(raw['received_at_ns'])-15*NS-1)
    with pytest.raises(ValueError):INPUT.validate_python(raw)
