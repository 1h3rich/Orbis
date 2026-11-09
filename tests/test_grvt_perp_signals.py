"""Causal rules, consumer time, replay and independence from fictitious funds."""
from dataclasses import fields
from decimal import localcontext
import json

import pytest

from app.lab.engine import IndicatorCache, evaluate
from app.live_execution.grvt_perp_signals import (
    PerpSignalInput, PerpSignalSeed, advance_signals, replay_signals, signal_checkpoint, stop_signals,
    record_signal_input, replay_signal_records, verify_signal_record,
)
from tests.grvt_perp_signal_fixture import NS, bar, signal_input, signal_seed


def until_close(seed):
    state = seed.start()
    inputs, decisions = [], []
    for second in range(10, 61, 10):
        candles = (bar(state.started_at_ns//NS, seed.definition['direction']),) if second==60 else ()
        raw = signal_input(state, second, candles=candles)
        transition = advance_signals(state, raw)
        state = transition.state
        inputs.append(raw)
        decisions.extend(json.loads(value) for value in transition.decisions_json)
    return state, inputs, decisions


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_seed_never_emits_past_matches_then_future_closed_bar_uses_the_shared_evaluator(direction, mode):
    seed = signal_seed(direction, mode)
    state = seed.start()
    assert state.evaluated_bars==state.matched_entries==0
    assert not {'account','cash','opened','positions','funding_anchor'} & {field.name for field in fields(state)}
    state, inputs, decisions = until_close(seed)
    assert state.status=='RUNNING' and state.evaluated_bars==state.matched_entries==len(decisions)==1
    rows = list(state.history)
    expected, explanation = evaluate(seed.definition['entry'], rows, len(rows)-1,
        IndicatorCache(rows), base_timeframe=state.timeframe)
    assert expected is True and decisions[0]['entry_explanation']==explanation
    assert decisions[0]['entry_matched'] and decisions[0]['source_candle_t']==state.started_at_ns//NS
    assert decisions[0]['known_at_ns']==str(state.started_at_ns+60*NS)
    assert decisions[0]['execution_authority']=='NONE'
    replay = replay_signals(seed, inputs)
    assert signal_checkpoint(replay.state)==signal_checkpoint(state)
    assert [json.loads(value) for value in replay.decisions_json]==decisions


@pytest.mark.parametrize('capital', ['0.000000000000000001','1','1000000'])
def test_paper_initial_capital_cannot_suppress_a_real_rule_match(capital):
    state, inputs, decisions = until_close(signal_seed(initial_capital=capital))
    assert state.matched_entries==1 and decisions[0]['entry_matched']
    assert 'quantity' not in decisions[0] and 'account' not in signal_checkpoint(state)


def test_matching_again_is_not_suppressed_by_an_imaginary_position():
    seed = signal_seed()
    state, inputs, decisions = until_close(seed)
    for second in range(70, 121, 10):
        raw = signal_input(state, second,
            candles=(bar(state.started_at_ns//NS+60),) if second==120 else ())
        result = advance_signals(state, raw)
        state = result.state
        decisions.extend(json.loads(value) for value in result.decisions_json)
    assert state.matched_entries==len(decisions)==2
    assert all(row['entry_matched'] for row in decisions)
    # Whether REAL is already in a position belongs to its coordinator, not
    # this source or a fictitious PAPER execution/sizing decision.


@pytest.mark.parametrize('change', ['open', 'duplicate', 'gap', 'reordered'])
def test_new_suffix_is_exactly_contiguous_closed_and_unseen(change):
    seed = signal_seed()
    state = seed.start()
    for second in range(10, 60, 10):
        state = advance_signals(state, signal_input(state, second)).state
    stamp = state.started_at_ns//NS
    candles = {'open':(bar(stamp+60),), 'duplicate':(bar(stamp-60),),
        'gap':(bar(stamp-120),bar(stamp)), 'reordered':(bar(stamp),bar(stamp-60))}[change]
    with pytest.raises(ValueError, match='Velas de señales'):
        advance_signals(state, signal_input(state, 60, candles=candles))
    assert state.sequence==5 and state.evaluated_bars==0


@pytest.mark.parametrize('change', ['future','stale','wrong_instrument','inverted'])
def test_invalid_quote_never_becomes_a_signal(change):
    state = signal_seed().start()
    changes = {'future':{'quote_time':state.started_at_ns+11*NS},
        'stale':{'quote_time':state.started_at_ns-6*NS},
        'wrong_instrument':{'instrument':'ETH_USDT_Perp'}, 'inverted':{'bid':'102','ask':'100'}}[change]
    with pytest.raises(ValueError):advance_signals(state, signal_input(state,10,**changes))


def test_delayed_consumer_keeps_original_receipt_and_checks_age_again():
    state = signal_seed().start()
    raw = signal_input(state, 10, delay=5*NS)
    updated = advance_signals(state, raw).state
    assert updated.last_observed_ns==state.started_at_ns+15*NS
    old = signal_input(state,10,quote_time=state.started_at_ns-5*NS,delay=NS)
    with pytest.raises(ValueError,match='antigua'):advance_signals(state,old)
    with pytest.raises(ValueError,match='Lote de señales antiguo'):
        signal_input(state,10,delay=5*NS+1)


@pytest.mark.parametrize('mode', ['transport','quote'])
def test_gap_is_terminal_and_cannot_resume_by_reading_saved_state(mode):
    state = signal_seed().start()
    if mode=='quote':
        # A newer consumer receipt of an old venue tick never resets producer coverage.
        state = advance_signals(state,signal_input(state,10,quote_time=state.started_at_ns)).state
    raw = signal_input(state, 21)
    result = advance_signals(state, raw)
    assert result.state.status=='GAP' and result.outcome=='QUOTE_COVERAGE_GAP'
    assert not result.decisions_json
    with pytest.raises(ValueError,match='inicio explícito'):
        advance_signals(result.state,signal_input(result.state,22))


def test_duplicate_quotes_are_recorded_without_rejuvenating_coverage_or_accepting_new_candles():
    state = signal_seed().start()
    result = advance_signals(state,signal_input(state,10,quote_time=state.started_at_ns))
    assert result.state.sequence==1 and result.state.last_observed_ns==state.last_observed_ns
    assert result.outcome=='IGNORED_OLD_OR_DUPLICATE_QUOTE'
    with pytest.raises(ValueError,match='contradictoria'):
        advance_signals(state,signal_input(state,10,quote_time=state.started_at_ns,mark_price='101'))
    with pytest.raises(ValueError,match='no acredita velas'):
        advance_signals(state,signal_input(state,10,quote_time=state.started_at_ns,
            candles=(bar(state.history[-1].t),)))


def test_missing_closed_bar_and_late_bar_halt_without_reconstruction():
    state = signal_seed().start()
    for second in range(10,76,10):
        state = advance_signals(state,signal_input(state,second)).state
    result = advance_signals(state,signal_input(state,76))
    assert result.state.status=='GAP' and result.outcome=='MISSING_CLOSED_CANDLE'
    late = advance_signals(state,signal_input(state,76,candles=(bar(state.started_at_ns//NS),)))
    assert late.state.status=='GAP' and late.outcome=='CLOSED_CANDLE_RECEIVED_LATE'
    assert not late.decisions_json and late.state.evaluated_bars==0


def test_exit_and_nested_entry_conditions_use_complete_higher_timeframes_only():
    rule = {'op':'AND','children':[{'kind':'BULLISH_STREAK','period':2},
        {'op':'OR','children':[{'kind':'BULLISH_STREAK','period':2,'timeframe':'5m'},
                              {'kind':'BEARISH_STREAK','period':2}]}]}
    seed = signal_seed(entry=rule,exit={'kind':'BULLISH_STREAK','period':2})
    state, inputs, decisions = until_close(seed)
    assert decisions[0]['entry_matched'] is False
    assert decisions[0]['exit_matched'] is True
    assert decisions[0]['entry_explanation']['children'][1]['children'][0]['matched'] is False
    # Growing to a complete 5m candle can only use bars known at that close.
    for second in range(70,601,10):
        candle_t = state.started_at_ns//NS+second-60
        result = advance_signals(state,signal_input(state,second,
            candles=(bar(candle_t),) if second%60==0 else ()))
        state = result.state
        if result.decisions_json:
            actual = json.loads(result.decisions_json[0])
            rows = list(state.history)
            expected, explanation = evaluate(seed.definition['entry'],rows,len(rows)-1,
                IndicatorCache(rows),base_timeframe='1m')
            assert (actual['entry_matched'],actual['entry_explanation'])==(expected,explanation)
    assert state.matched_entries>0


def test_stop_clock_and_strict_input_formats():
    seed = signal_seed()
    state = seed.start()
    stopped = stop_signals(state,state.started_at_ns)
    with pytest.raises(ValueError,match='detenida'):advance_signals(stopped,signal_input(state,10))
    with pytest.raises(ValueError):stop_signals(state,state.started_at_ns-1)
    for change in ({'execution_authority':'REAL'}, {'prepared_at_ns':'01'}, {'timeframe':'30m'},
                   {'profile':{**seed.profile.payload(),'signal_engine':'lab-1.15'}}):
        with pytest.raises(ValueError):PerpSignalSeed.model_validate({**seed.payload(),**change})
    for change in ({'collected_at_ns':str(state.started_at_ns-1)},
                   {'accepted_at_ns':str(state.started_at_ns+2**63)}, {'authorization':True}):
        with pytest.raises(ValueError):
            raw=PerpSignalInput.model_validate({**signal_input(state,10).payload(),**change})
            advance_signals(state,raw)


def test_low_decimal_context_replay_and_mutable_wire_inputs_cannot_change_started_state():
    seed = signal_seed(entry={'kind':'RSI_GT','period':2,'threshold':'49.99999999999999999'})
    original, inputs, decisions = until_close(seed)
    with localcontext() as context:
        context.prec=4
        replay=replay_signals(seed,inputs)
    assert signal_checkpoint(replay.state)==signal_checkpoint(original)
    state = seed.start()
    seed.definition['entry']['threshold']='100'
    assert json.loads(state.definition_json)['entry']['threshold']=='49.99999999999999999'
    with pytest.raises(ValueError):seed.start()


@pytest.mark.parametrize('edit', ['checkpoint','decision','authority','outcome','input_time','input_candle'])
def test_rehashed_record_edits_cannot_replace_recalculation_of_the_seed_and_inputs(edit):
    from copy import deepcopy
    from app.lab.store import content_hash
    seed = signal_seed()
    state = seed.start()
    records = []
    for second in range(10,61,10):
        result, record = record_signal_input(state,
            signal_input(state,second,candles=(bar(state.started_at_ns//NS),) if second==60 else ()))
        state = result.state
        records.append(record)
    replay = replay_signal_records(seed,records)
    assert signal_checkpoint(replay.state)==signal_checkpoint(state)
    changed = deepcopy(records)
    record = changed[-1]
    if edit=='checkpoint':record['checkpoint']['matched_entries']=99
    elif edit=='decision':record['decisions'][0]['entry_matched']=False
    elif edit=='authority':record['execution_authority']='REAL'
    elif edit=='outcome':record['outcome']='ORDER_EXECUTED'
    elif edit=='input_time':record['input']['quote']['event_time_ns']=str(state.started_at_ns+59*NS)
    else:record['input']['closed_candles'][0]['v']='11'
    record['sha256']=content_hash({key:value for key,value in record.items() if key!='sha256'})
    with pytest.raises(ValueError,match='replay causal'):replay_signal_records(seed,changed)


def test_cold_subprocess_replays_saved_rule_evidence_without_network_keys_or_authority(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    seed = signal_seed()
    state = seed.start()
    records=[]
    for second in range(10,61,10):
        result,record=record_signal_input(state,signal_input(state,second,
            candles=(bar(state.started_at_ns//NS),) if second==60 else ()))
        state=result.state
        records.append(record)
    path=tmp_path/'owned-rule-evidence.json'
    path.write_text(json.dumps({'seed':seed.payload(),'records':records}))
    code='''
import json,sys,socket
def forbidden(*args,**kwargs):raise AssertionError('Cold replay attempted network')
socket.socket.connect=forbidden
from app.live_execution.grvt_perp_signals import PerpSignalSeed,replay_signal_records,signal_checkpoint
saved=json.load(open(sys.argv[1]))
result=replay_signal_records(PerpSignalSeed.model_validate(saved['seed']),saved['records'])
assert result.outcome=='REPLAYED_WITHOUT_AUTHORITY'
assert not hasattr(result.state,'account')
print(json.dumps(signal_checkpoint(result.state)))
'''
    completed=subprocess.run([sys.executable,'-c',code,str(path)],cwd=Path(__file__).resolve().parents[1],
        capture_output=True,text=True,timeout=30)
    assert completed.returncode==0,completed.stderr
    assert json.loads(completed.stdout)==signal_checkpoint(state)


def test_maximum_history_keeps_quote_checkpoints_bounded_and_stops_at_capacity():
    seed = signal_seed()
    raw = seed.payload()
    start = int(seed.prepared_at_ns)//NS
    raw['history'] = [bar(start+i*60).json() for i in range(-100_000,0)]
    state = PerpSignalSeed.model_validate(raw).start()
    for second in range(10,60,10):
        before = state.history
        state = advance_signals(state,signal_input(state,second)).state
        assert state.history is before  # No rebuilt growing series for a quote without a new candle.
        assert len(json.dumps(signal_checkpoint(state)))<2000
    result = advance_signals(state,signal_input(state,60,candles=(bar(start),)))
    assert result.state.status=='GAP' and result.outcome=='HISTORY_CAPACITY'
    assert len(result.state.history)==100_000 and result.state.evaluated_bars==0
