"""Frozen view pagination, exact equity and refusal to return partial replay."""
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_view import ObservedReplayView
from app.derivatives.perp_observed_archive_format import pack,unpack
from app.derivatives.perp_observed_wire import observation_input
from tests.perp_observed_archive_fixture import NS,owned_archive
from tests.perp_observed_fixture import batch


def uid():return str(uuid4())


def test_view_pins_a_replayed_old_head_after_the_active_account_advances(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    created=archive.create(origin,seed,request_id=uid());id=created['id']
    snapshot=archive.read(id,view=ObservedReplayView(candle_limit=3))
    clock['now']+=NS;archive.control(id,'STOP',request_id=uid())
    frozen=archive.read(id,view=ObservedReplayView(candle_limit=3,before_t=snapshot['view']['candles'][0]['t']),
        snapshot_sequence=snapshot['session']['sequence'],snapshot_hash=snapshot['session']['head_hash'])
    assert frozen['session']['checkpoint']==snapshot['session']['checkpoint']
    assert not frozen['snapshot_is_latest'] and frozen['session']['sequence']==0
    assert frozen['view']['candles'][-1]['t']<snapshot['view']['candles'][0]['t']
    assert frozen['view']['metrics']['equity']=='1000' and frozen['view']['metrics']['net_profit']=='0'
    with pytest.raises(ObservedError,match='snapshot'):
        archive.read(id,view=ObservedReplayView(),snapshot_sequence=0,snapshot_hash='0'*64)


def test_corrupted_old_snapshot_cannot_be_loaded_as_a_financial_checkpoint(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    created=archive.create(origin,seed,request_id=uid());id=created['id']
    clock['now']+=NS;archive.control(id,'STOP',request_id=uid())
    with paper.connect() as db:
        row=db.execute('SELECT evidence_blob FROM paper_observed_events WHERE session_id=? AND sequence=0',(id,)).fetchone()
        evidence=unpack(row['evidence_blob']);evidence['checkpoint']['account']['cash']='999999'
        db.execute('UPDATE paper_observed_events SET evidence_blob=? WHERE session_id=? AND sequence=0',(pack(evidence),id))
    with pytest.raises(ObservedError,match='reproduce'):
        archive.read(id,view=ObservedReplayView(),snapshot_sequence=0,snapshot_hash=created['head_hash'])


@pytest.mark.parametrize('options',[{'candle_limit':2001},{'trade_limit':101},{'marker_limit':5001},{'trade_offset':-1},{'before_t':True}])
def test_view_windows_have_explicit_size_bounds(options):
    with pytest.raises(ObservedError):ObservedReplayView(**options)


def test_margin_history_distinguishes_changes_from_refreshes_and_follows_the_replayed_head(tmp_path, monkeypatch):
    _, _, archive, origin, seed, clock = owned_archive(tmp_path, monkeypatch)
    created = archive.create(origin, seed, request_id=uid())
    session_id = created['id']
    state = seed.start().state
    started = clock['now']
    original = state.account.markets[0].rules
    refresh = replace(original, fetched_at_ns=started+NS, max_size=original.max_size.quantize(Decimal('.00')))
    changed = replace(refresh, fetched_at_ns=started+2*NS-50_000_000,
                      tiers=(replace(refresh.tiers[0], maintenance_rate=Decimal('.01234')), *refresh.tiers[1:]))
    for seconds, rules in enumerate((refresh, changed, replace(changed, fetched_at_ns=started+3*NS)), 1):
        clock['now'] = started + seconds*NS
        receipt = archive.append(session_id, observation_input(batch(state, seconds, rules=rules)), request_id=uid())
        state = archive.current_state(session_id)
        if seconds == 1:
            before = receipt
    timeline = archive.read(session_id, view=ObservedReplayView())['view']['margin_rules']
    assert timeline['change_total'] == 1
    assert timeline['last_capture_at_ns'] == str(started+3*NS)
    assert timeline['initial']['sequence'] == 0
    point, = timeline['changes']
    assert point['sequence'] == 2 and point['changes'] == ['RISK_TIERS']
    assert point['applied_at_ns'] == str(started+2*NS)
    assert point['rules']['fetched_at_ns'] == str(started+2*NS-50_000_000)
    assert point['rules']['risk_brackets'][0]['maintenance_margin_rate'] == '0.01234'
    old = archive.read(session_id, view=ObservedReplayView(),
                       snapshot_sequence=before['sequence'], snapshot_hash=before['head_hash'])['view']['margin_rules']
    assert old == {'initial': timeline['initial'], 'changes': [], 'change_total': 0,
                   'last_capture_at_ns': str(started+NS)}


def test_margin_history_limits_display_size_and_keeps_initial_rules(tmp_path, monkeypatch):
    _, _, archive, origin, seed, clock = owned_archive(tmp_path, monkeypatch)
    created = archive.create(origin, seed, request_id=uid())
    session_id = created['id']
    state = seed.start().state
    started = clock['now']
    for seconds in range(1, 23):
        clock['now'] = started + seconds*NS
        rules = state.account.markets[0].rules
        changed = replace(rules, fetched_at_ns=clock['now'], max_size=rules.max_size+1)
        archive.append(session_id, observation_input(batch(state, seconds, rules=changed)), request_id=uid())
        state = archive.current_state(session_id)
    history = archive.read(session_id, view=ObservedReplayView())['view']['margin_rules']
    assert history['initial']['sequence'] == 0 and history['change_total'] == 22
    assert [row['sequence'] for row in history['changes']] == list(range(22, 2, -1))
    assert all(row['changes'] == ['POSITION_LIMIT'] for row in history['changes'])
