"""Exact quantities, quote causality and encrypted replay for PAPER partials."""
from decimal import Decimal as D, ROUND_FLOOR
import io
import json
from uuid import uuid4

import pytest

from app.derivatives.funding import FundingRecord
from app.derivatives.perp_gap_close import estimate_gap_close, GapCloseInput
from app.derivatives.perp_observed import observe, request_observed_close, start_observed, stop_observed
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_archive_format import event_hash, pack, unpack
from app.derivatives.perp_observed_export import verify_observed_export
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_wire import CloseEvent, observation_input, read_observed_seed
from app.lab.store import encoded
from app.live_execution.grvt_perp_limit_origin import verified_limit_paper_origin, verified_partial_paper_origin
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import PerpLimitSignalSeed, PerpSignalSeed, record_signal_input, replay_signal_records
from app.research.perp_live_review import review_perp_live
from tests.perp_observed_archive_fixture import owned_archive
from tests.perp_observed_fixture import BTC, NOW, NS, bar, batch, initial
from tests.perp_observed_source_fixture import OwnedPublicAdapter
from tests.test_grvt_perp_plan import plan_source
from tests.test_perp_observed import assert_deltas
from tests.test_perp_partial_simulation import TARGETS, partial_profile


def entered(side='LONG', mode='ISOLATED', *, limited=False, targets=TARGETS, **changes):
    base = initial(side, mode, **changes)
    definition = json.loads(base.definition_json)
    profile = partial_profile(definition, limited=limited, targets=targets, margin_mode=mode)
    state = start_observed(profile, definition, base.timeframe, base.account.markets[0],
        base.history, base.last_quote, base.started_at_ns, (base.funding_anchor,)).state
    for second in range(10, 61, 10):
        candles = (bar(state.started_at_ns//NS, side),) if second == 60 else ()
        state = observe(state, batch(state, second, bars=candles)).state
    if limited:
        assert state.resting is not None and state.opened is None
        state = observe(state, batch(state, 65)).state
    assert state.opened is not None
    return state


def fills(transition):
    return [row for row in transition.journal if row.kind == 'FILL']


@pytest.mark.parametrize('limited', [False, True])
@pytest.mark.parametrize('mode', ['ISOLATED', 'CROSS'])
@pytest.mark.parametrize(('side','first_quote','last_quote','prices','sign'), [
    ('LONG', ('103','104'), ('109','110'), (D(103),D(109)), 1),
    ('SHORT', ('96','97'), ('90','91'), (D(97),D(91)), -1),
])
def test_each_target_is_once_and_final_fill_reduces_only_the_remainder(limited,mode,side,first_quote,last_quote,prices,sign):
    state = entered(side,mode,limited=limited)
    quantity = state.opened.quantity
    assert state.opened.entry_price == 100
    position = state.account.positions[0]
    initial_cash = state.account.cash
    first = observe(state,batch(state,70,bid=first_quote[0],ask=first_quote[1]))
    assert_deltas(state.account,first)
    assert first.state.closed_trades == 0
    lot = D('.001')
    q1,q2 = ((quantity*D(row['fraction'])/lot).to_integral_value(rounding=ROUND_FLOOR)*lot for row in TARGETS)
    assert [abs(row.size_delta) for row in fills(first)] == [q1]
    assert first.state.account.positions[0].size == position.size - sign*q1
    assert first.state.account.order(fills(first)[0].order_id).reduce_only
    repeated = observe(first.state,batch(first.state,75,bid=first_quote[0],ask=first_quote[1]))
    assert not fills(repeated)
    final = observe(repeated.state,batch(repeated.state,80,bid=last_quote[0],ask=last_quote[1]))
    assert_deltas(repeated.state.account,final)
    assert final.state.closed_trades == 1 and not final.state.account.positions
    assert [abs(row.size_delta) for row in fills(final)] == [q2,quantity-q1-q2]
    assert [row.price for row in fills(first)+fills(final)] == [prices[0],prices[1],prices[1]]
    assert all(final.state.account.order(row.order_id).reduce_only for row in fills(final))
    trade, = map(json.loads,final.trades_json)
    assert trade['exit_reason'] == 'TAKE_PROFIT'
    assert [row['target_index'] for row in trade['partial_exits']] == [1,2]
    assert [D(row['remaining_after']) for row in trade['partial_exits']] == [quantity-q1,quantity-q1-q2]
    realized = sign*((prices[0]-100)*q1+(prices[1]-100)*(quantity-q1))
    closing_fees = D('.001')*(prices[0]*q1+prices[1]*(quantity-q1))
    assert final.state.account.cash == initial_cash + position.collateral + realized - closing_fees
    assert D(trade['net_profit']) == realized - closing_fees - quantity*D('.1')


def test_short_uses_ask_and_long_uses_bid_at_the_trigger():
    long,short = entered(),entered('SHORT')
    assert not fills(observe(long,batch(long,70,bid='101',ask='104')))
    assert not fills(observe(short,batch(short,70,bid='96',ask='99')))
    assert len(fills(observe(long,batch(long,70,bid='102',ask='104')))) == 1
    assert len(fills(observe(short,batch(short,70,bid='96',ask='98')))) == 1


def test_dust_and_manual_close_preserve_residual_quantity():
    state = entered(targets=[{'target':'0.02','fraction':'0.000001'},*TARGETS[1:]])
    step = observe(state,batch(state,70,bid='103',ask='104'))
    event, = map(json.loads,step.state.opened.partial_exits_json)
    assert event['kind'] == 'PARTIAL_TARGET_SKIPPED_MINIMUM' and not fills(step)
    state = stop_observed(step.state,NOW+71*NS).state
    manual = request_observed_close(state,NOW+72*NS)
    assert manual.state.opened == state.opened and not manual.journal
    final = observe(manual.state,batch(manual.state,80,bid='109',ask='110'))
    assert [abs(row.size_delta) for row in fills(final)] == [state.opened.quantity]
    assert final.state.status == 'STOPPED'
    assert json.loads(final.trades_json[0])['exit_reason'] == 'MANUAL_CLOSE_OBSERVED'


def test_funding_and_stop_use_the_remainder_after_an_observed_partial():
    state = entered(start=NOW+3480*NS,fee='0')
    first = observe(state,batch(state,70,bid='103',ask='104'))
    state = first.state
    remainder = abs(state.account.positions[0].size)
    for second in range(80,120,10):
        state = observe(state,batch(state,second)).state
    record = FundingRecord(BTC,NOW+3600*NS,D('1'),D('100'),1)
    final = observe(state,batch(state,120,bid='96',ask='97',bars=(bar(state.started_at_ns//NS+60),),
        funding=(state.funding_anchor,record)))
    assert final.state.account.funding_paid == remainder
    assert [abs(row.size_delta) for row in fills(final)] == [remainder]
    assert json.loads(final.trades_json[0])['exit_reason'] == 'STOP_OR_TRAILING'


def test_liquidation_and_coverage_gaps_do_not_invent_a_partial():
    state = entered()
    liquidated = observe(state,batch(state,70,bid='109',ask='110',mark='60'))
    assert liquidated.state.status == 'LIQUIDATED' and not fills(liquidated)
    assert json.loads(liquidated.trades_json[0])['partial_exits'] == []
    first = observe(state,batch(state,70,bid='103',ask='104'))
    gap = observe(first.state,batch(first.state,100,bid='110',ask='111'))
    assert gap.state.status == 'GAP' and not gap.journal
    assert gap.state.opened == first.state.opened and gap.state.account.positions == first.state.account.positions
    fresh = observation_input(batch(first.state,110,bid='104',ask='105')).payload()
    estimate = estimate_gap_close(gap.state,GapCloseInput.model_validate({
        'schema':'orbis.paper.perp.gap-close.input.v1','engine':'perp-gap-close-1.0',
        'acknowledgement':'UNKNOWN_INTERVAL_ACCEPTED',
        **{key:fresh[key] for key in ('received_at_ns','quote','rules','specification','funding')}}))
    assert D(estimate['remaining_quantity']) == abs(gap.state.account.positions[0].size)
    assert estimate['quality'] == 'ESTIMATED_GAP_CLOSE'


@pytest.mark.parametrize('limited',[False,True])
def test_encrypted_archive_and_export_replay_partials_and_real_gate(tmp_path,monkeypatch,limited):
    lab,paper,archive,origin,seed,clock = owned_archive(tmp_path,monkeypatch,limited=limited,partial_targets=TARGETS)
    assert origin.reference().execution_authority == 'NONE'
    with pytest.raises(ValueError):
        read_observed_seed({**seed.payload(),'engine':'perp-observed-2.0'})
    identifier = archive.create(origin,seed,request_id=str(uuid4()))['id']
    with pytest.raises(ValueError,match='con parciales'):
        verified_partial_paper_origin(lab,archive,identifier)
    for second in (10,20,30,40,50,60,70,80,90):
        state = archive.current_state(identifier)
        candles = (bar(state.started_at_ns//NS),) if second == 60 else ()
        prices = {70:{'bid':'98','ask':'99'},80:{'bid':'107','ask':'108'},
                  90:{'bid':'103','ask':'104'}}.get(second,{})
        if second == 90:
            clock['now'] = state.started_at_ns + 85*NS
            archive.append(identifier,CloseEvent(schema_name='orbis.paper.perp.observed.input.v1',
                kind='CLOSE_REQUEST',received_at_ns=str(clock['now'])),request_id=str(uuid4()))
        event = observation_input(batch(state,second,bars=candles,**prices))
        clock['now'] = int(event.received_at_ns)
        archive.append(identifier,event,request_id=str(uuid4()))
    original = archive.read(identifier)
    snapshot = original['session']['checkpoint']
    assert snapshot['schema'] == 'orbis.paper.perp.observed.checkpoint.v3'
    assert snapshot['entry_kind']==('LIMIT' if limited else 'MARKET')
    assert snapshot['closed_trades'] == 1 and snapshot['status'] == 'STOPPED'
    cold = ObservedArchive(paper,clock_ns=lambda:clock['now'])
    assert cold.read(identifier)['session']['checkpoint'] == snapshot
    assert not cold.active_ids()
    exported = verify_observed_export(io.BytesIO(b''.join(archive.export_lines(identifier))))
    assert exported['checkpoint'] == snapshot and exported['execution_authority'] == 'NONE'
    with pytest.raises(ValueError,match='LAB y PAPER'):
        review_perp_live(lab,archive,identifier,'1',object())
    with pytest.raises(ValueError,match='PAPER LIMIT'):
        verified_limit_paper_origin(lab,archive,identifier)
    with pytest.raises(ValueError,match='REAL compatible'):
        GrvtPerpSignalSource(adapter=object(),clock_ns=lambda:clock['now']).prepare(origin)
    verified = verified_partial_paper_origin(lab,cold,identifier)
    assert verified.partial_fills==2 and verified.paper.head_hash==original['session']['head_hash']
    budget = plan_source().terms.budget
    terms = verified.terms(budget,max_attempts=4,reserved_close_attempts=2)
    assert terms.paper_partial_fills==2 and terms.research.sha256==origin.reference().sha256
    assert terms.execution_authority=='NONE' and terms.budget.sub_account_id==budget.sub_account_id
    with pytest.raises(ValueError,match='reserva de intentos'):
        verified.terms(budget,max_attempts=3,reserved_close_attempts=2)
    adapter = OwnedPublicAdapter(seed,clock)
    start = int(seed.received_at_ns)//NS
    adapter.rows[start] = bar(start).json()
    source = GrvtPerpSignalSource(adapter=adapter,clock_ns=lambda:clock['now'])
    altered_terms = terms.model_copy(update={'paper':terms.paper.model_copy(update={'head_hash':'0'*64})})
    with pytest.raises(ValueError,match='prefijo PAPER'):
        source.prepare_partial(verified,altered_terms)
    assert adapter.calls==[]
    real_seed = source.prepare_partial(verified,terms)
    assert real_seed.schema_name=='orbis.grvt.perp.signals.seed.v3' and real_seed.execution_authority=='NONE'
    assert real_seed.origin_reference_hash==terms.research.sha256
    assert real_seed.start().matched_entries==0
    for legacy_seed in (PerpSignalSeed,PerpLimitSignalSeed):
        with pytest.raises(ValueError):
            legacy_seed.model_validate(real_seed.payload())
    adapter.rows[start+60] = bar(start+60).json()
    clock['now'] = (start+105)*NS
    initial_feed = real_seed.start()
    interim,first_record = record_signal_input(initial_feed,source.poll(initial_feed))
    clock['now'] = (start+120)*NS
    observed,record = record_signal_input(interim.state,source.poll(interim.state))
    assert observed.state.evaluated_bars==1
    assert replay_signal_records(real_seed,[first_record,record]).state==observed.state
    assert set(adapter.calls)=={'candles','quote'}
    with paper.connect() as db:
        tail = db.execute('SELECT * FROM paper_observed_events WHERE session_id=? AND sequence=?',
            (identifier,original['session']['sequence'])).fetchone()
        source,evidence = unpack(tail['input_blob']),unpack(tail['evidence_blob'])
        evidence['trades'][0]['partial_exits'][0]['remaining_after'] = '999'
        forged = event_hash(identifier,tail['sequence'],tail['previous_hash'],source,evidence)
        db.execute('UPDATE paper_observed_events SET evidence_blob=?,hash=? WHERE session_id=? AND sequence=?',
            (pack(evidence),forged,identifier,tail['sequence']))
        db.execute('UPDATE paper_observed_sessions SET checkpoint_json=?,head_hash=? WHERE id=?',
            (encoded(evidence['checkpoint']),forged,identifier))
    with pytest.raises(ObservedError,match='reproduce'):
        cold.read(identifier)
    with pytest.raises(ValueError,match='cambió'):
        with verified.hold_current():
            pytest.fail('El prefijo alterado no puede producir términos nuevos')
