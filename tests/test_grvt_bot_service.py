"""Autorización → señal futura real → FOK OTOCO → seguimiento y cierre."""

from decimal import Decimal
from threading import Event
import time

import pytest

from tests.grvt_bracket_ui_fixture import BracketUiVenue
from tests.test_grvt_testnet_signer import KEY, SIGNER


def wait_for(predicate, seconds=10):
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        value = predicate()
        if value: return value
        time.sleep(.01)
    raise AssertionError('El ciclo no alcanzó el estado esperado')


def bot_case(tmp_path, direction='LONG', *, public_hook=None,risk_heartbeat=False,min_notional=None):
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    from app.live_execution.grvt_bot_service import GrvtBotService
    from app.derivatives.paper_math import PerpQuote
    venue = BracketUiVenue(tmp_path, direction)
    origin = venue.origin.seed(direction, defer_open=True)
    origin['sub_account_id'] = venue.cap.sub_account_id
    venue.recorded['private'].clear()  # La construcción del venue autentica su transporte base.
    market_trigger = Event(); stream_open = Event(); closed = Event()
    clock = [time.time_ns()]
    def now(): return max(clock[0], time.time_ns())
    venue.clock_ns = now
    venue.advance_bot_clock = lambda seconds:clock.__setitem__(0,now()+int(seconds*1_000_000_000))
    def public(url, body):
        if public_hook: public_hook(url, body)
        reply = venue.public(url, body)
        if min_notional is not None and url.endswith('/instrument'):
            payload = reply.json(); payload['result']['min_notional'] = min_notional
            return venue.origin.response(url, payload)
        if url.endswith('/ticker'):
            payload = reply.json(); payload['result']['event_time'] = str(now())
            return venue.origin.response(url, payload)
        return reply
    class Stream:
        def __enter__(self): stream_open.set(); return self
        def __exit__(self, *args): closed.set()
        def recv(self, *, timeout):
            market_trigger.wait(.05)
            mark = Decimal('108.1') if market_trigger.is_set() else Decimal('100')
            return ('BTC/USDT:PERP', PerpQuote(venue.cap.instrument, now(), mark, Decimal('99.99'), Decimal('100.01')))
    def waiter(stop, seconds):
        clock[0] = now()+int(seconds*1_000_000_000)
        if risk_heartbeat:
            # Otro consumidor de datos públicos sigue observando PAPER
            # mientras TESTNET concilia, sin fabricar señales ni rellenar gaps.
            stamp=now(); venue.origin.clock['now']=stamp//1_000_000_000
            venue.origin.monitor.clock_ns=now
            assert venue.origin.monitor.on_risk_quote(origin['session_id'],PerpQuote(
                venue.cap.instrument,stamp,Decimal('100'),Decimal('99.99'),Decimal('100.01'))) in ('OBSERVED','NO_POSITION')
        return stop.wait(.001)
    closes = GrvtStrategyCloseService(venue.vault, client_factory=venue.factory, public_post=public,
        stream_factory=lambda symbol, clock_ns:Stream(), clock_ns=now, waiter=waiter)
    service = GrvtBotService(venue.vault, closes, client_factory=venue.factory,
        public_post=public, clock_ns=now, poll_seconds=.01)
    def activate(**policy):
        limits = dict(wait_seconds=120,exit_seconds=120)
        limits.update(policy)
        entry_cap = limits.pop('entry_cap', Decimal('50'))
        exit_cap = limits.pop('exit_cap', Decimal('100'))
        return service.start(venue.origin.paper, venue.origin.lab, origin['session_id'],
            sub_account_id=origin['sub_account_id'], entry_cap=entry_cap, exit_cap=exit_cap,
            **limits, max_attempts=2, max_spread_bps=25, max_slippage_bps=25,
            api_key='fake-testnet-key-'+origin['sub_account_id'], private_key=KEY.hex(), expected_signer=SIGNER,
            expected_ticket=service.permission_ticket(origin['sub_account_id']))
    def emit():
        venue.origin.clock['now'] = int(time.time())
        venue.origin.monitor.clock_ns = time.time_ns
        venue.origin.monitor.poll(origin['session_id'])
    return venue, origin, service, closes, activate, emit, market_trigger, stream_open, closed


def test_bot_rejects_an_impossible_provider_minimum_before_recording_permission_or_private_login(tmp_path):
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue, origin, service, closes, activate, *rest = bot_case(tmp_path, min_notional='100')
    with pytest.raises(GrvtTestnetBracketError, match='mínimos|Mínimos'):
        activate()
    assert not service.history(origin['sub_account_id'])['items']
    assert not venue.recorded['private'] and venue.bulk_sends == 0
    started = activate(entry_cap=Decimal('150'), exit_cap=Decimal('200'))
    assert started['active'] and started['status'] == 'WAITING_SIGNAL'
    assert started['entry_cap'] == '150' and started['exit_cap'] == '200'
    assert not venue.recorded['private'] and venue.bulk_sends == 0
    service.stop(started['id'])
    wait_for(lambda: not service.snapshot(started['id'])['active'])
    venue.vault.lock()


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_explicit_budget_above_actual_provider_minimum_can_complete_a_protected_cycle(tmp_path,direction):
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(
        tmp_path,direction,min_notional='100')
    started=activate(entry_cap=Decimal('150'),exit_cap=Decimal('200'))
    emit(); wait_for(opened.is_set)
    assert venue.bulk_sends == 1
    assert all(Decimal(order['legs'][0]['size'])*Decimal(order['legs'][0]['limit_price']) >= 100
        for order in venue.states.values())
    venue.trigger('TAKE_PROFIT')
    done=wait_for(lambda: (item if not (item:=service.snapshot(started['id']))['active'] else None))
    assert done['status'] == 'COMPLETE' and venue.position == 0
    assert done['entry_cap'] == '150' and venue.bulk_sends == 1
    venue.vault.backup(tmp_path/'provider-minimum-completed.db'); venue.vault.lock()


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_future_signal_enters_once_and_native_exit_finishes_without_another_authorization(tmp_path, direction):
    import json
    from hashlib import sha256
    from app.lab.store import content_hash
    from app.live_execution.grvt_bot_executions import export_bot_research, read_bot_executions

    venue, origin, service, closes, activate, emit, trigger, stream_open, closed = bot_case(tmp_path, direction)
    started = activate(); run = started['id']
    assert started['active'] and started['status'] == 'WAITING_SIGNAL'
    assert venue.bulk_sends == 0 and not venue.recorded['private']
    emit()
    wait_for(stream_open.is_set)
    parent = service.snapshot(run)['selection']['parent_client_order_id']
    assert venue.bulk_sends == 1 and venue.position != 0
    pending = export_bot_research(venue.vault, run, venue.cap.sub_account_id)
    pending_row = json.loads(pending['jsonl'])
    assert pending['record_count']==1 and pending['fill_count']==0
    assert pending_row['outcome']=={'entry_evidence':'NO_VERIFIED_ENTRY','completed_at_ns':None,'fills':[]}
    venue.trigger('TAKE_PROFIT')
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'COMPLETE', done
    assert done['selection']['parent_client_order_id'] == parent and venue.position == 0
    assert venue.bulk_sends == 1 and venue.close_sends == venue.cancel_sends == 0
    assert closed.is_set()
    from app.live_execution.grvt_bot_archive import read_bot_run
    assert read_bot_run(venue.vault, run)['status'] == 'COMPLETE'
    calls = len(venue.recorded['private'])
    chart = read_bot_executions(venue.vault, run, venue.cap.sub_account_id)
    assert chart['direction'] == direction and chart['network'] == 'TESTNET'
    assert [fill['role'] for fill in chart['executions']] == ['ENTRY','TAKE_PROFIT']
    assert [fill['side'] for fill in chart['executions']] == (['BUY','SELL'] if direction == 'LONG' else ['SELL','BUY'])
    assert all(fill['basis'] == 'RECONCILED' and Decimal(fill['price']) > 0 for fill in chart['executions'])
    exported = export_bot_research(venue.vault, run, venue.cap.sub_account_id)
    assert exported['record_count']==1 and exported['fill_count']==2
    assert sha256(exported['jsonl'].encode()).hexdigest()==exported['sha256']
    sample = json.loads(exported['jsonl'])
    assert sample.pop('row_sha256')==content_hash(sample)
    assert sample['environment']=='TESTNET' and sample['input']==pending_row['input']
    assert sample['provenance']==pending_row['provenance']
    assert sample['provenance']['strategy_version_id']==done['source']['version_id']
    assert sample['provenance']['dataset_sha256']==done['source']['dataset_hash']
    assert sample['outcome']['entry_evidence']=='RECONCILED_ENTRY'
    assert [fill['role'] for fill in sample['outcome']['fills']]==['ENTRY','TAKE_PROFIT']
    for private_value in (run,parent,done['source']['session_id'],done['source']['account_id'],KEY.hex(),SIGNER):
        assert private_value not in exported['jsonl']
    assert len(venue.recorded['private']) == calls
    venue.vault.backup(tmp_path/'automatic-complete.db')
    for secret in (KEY.hex(), SIGNER, 'fake-testnet-key-'+origin['sub_account_id']):
        assert secret not in str(done)
    venue.vault.lock()


def test_filled_strategy_target_uses_the_same_permission_to_close(tmp_path):
    venue, origin, service, closes, activate, emit, trigger, stream_open, closed = bot_case(tmp_path)
    run = activate()['id']; emit(); wait_for(stream_open.is_set)
    trigger.set()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'COMPLETE', done
    assert venue.bulk_sends == venue.close_sends == venue.cancel_sends == 1 and venue.position == 0
    from app.live_execution.grvt_bot_executions import read_bot_executions
    chart = read_bot_executions(venue.vault, run, venue.cap.sub_account_id)
    assert [fill['role'] for fill in chart['executions']] == ['ENTRY','IOC']
    assert chart['executions'][0]['event_time_ns'] < chart['executions'][1]['event_time_ns']
    venue.vault.backup(tmp_path/'automatic-strategy-close.db'); venue.vault.lock()


def test_stop_during_wait_revokes_delayed_start_and_does_not_copy_an_existing_entry(tmp_path):
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue, origin, service, closes, activate, emit, *rest = bot_case(tmp_path)
    old = service.permission_ticket(origin['sub_account_id'])
    run = activate()['id']
    service.stop(run)
    emit()
    result = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert result['status'] == 'STOPPED' and venue.bulk_sends == 0
    with pytest.raises(GrvtTestnetBracketError):
        service.require_permission(origin['sub_account_id'], old)
    # La sesión tiene ahora una entrada anterior al segundo permiso.
    second = activate()['id']; time.sleep(.05)
    assert service.snapshot(second)['status'] == 'WAITING_SIGNAL' and venue.bulk_sends == 0
    service.stop(run)  # Una repetición de STOP de un episodio antiguo no revoca el nuevo.
    time.sleep(.03)
    assert service.snapshot(second)['active'] and service.snapshot(second)['status'] == 'WAITING_SIGNAL'
    service.stop(second)
    wait_for(lambda: not service.snapshot(second)['active']); venue.vault.lock()


def test_stop_between_quote_and_send_blocks_entry_without_retry(tmp_path):
    armed = Event(); entered = Event(); released = Event()
    def hook(url, body):
        if armed.is_set() and url.endswith('/ticker'):
            entered.set(); assert released.wait(5)
    venue, origin, service, closes, activate, emit, *rest = bot_case(tmp_path, public_hook=hook)
    run = activate()['id']; armed.set(); emit(); assert entered.wait(5)
    service.stop(run); released.set()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'STOPPED' and venue.bulk_sends == 0
    venue.vault.lock()


def test_lost_entry_reply_keeps_selected_ids_and_never_resends(tmp_path):
    venue, origin, service, closes, activate, emit, *rest = bot_case(tmp_path)
    venue.lost = True
    run = activate()['id']; emit()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'NEEDS_REVIEW' and done['selection']
    assert venue.bulk_sends == 1 and venue.position != 0
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    assert read_bracket(venue.vault, done['selection']['parent_client_order_id'])['status'] == 'POST_CLAIMED'
    time.sleep(.05); assert venue.bulk_sends == 1
    venue.vault.backup(tmp_path/'lost-automatic-entry.db'); venue.vault.lock()


@pytest.mark.parametrize('fault',['expired','paper_stop','old_signal'])
def test_waiting_episode_cannot_trade_after_deadline_source_stop_or_signal_expiration(tmp_path,fault):
    venue, origin, service, closes, activate, emit, *rest = bot_case(tmp_path)
    run = activate()['id']
    if fault == 'expired': venue.advance_bot_clock(121)
    if fault == 'paper_stop': venue.origin.monitor.stop(origin['session_id'])
    if fault == 'old_signal':
        venue.advance_bot_clock(21); emit(); time.sleep(.05)
        assert service.snapshot(run)['status'] == 'WAITING_SIGNAL'
        assert service.snapshot(run)['selection'] is None
        service.stop(run)
    result = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert result['status'] == {'expired':'EXPIRED','paper_stop':'NEEDS_REVIEW','old_signal':'STOPPED'}[fault]
    assert venue.bulk_sends == 0 and not venue.recorded['private']
    venue.vault.lock()


def test_passive_read_during_completion_cannot_drop_the_durable_terminal(tmp_path):
    from app.live_execution.grvt_bot_archive import read_bot_run
    venue, origin, service, closes, activate, emit, trigger, stream_open, closed = bot_case(tmp_path)
    terminal = Event(); proceed = Event(); real_finish = service._finish
    def delayed_finish(job):
        terminal.set(); assert proceed.wait(5); real_finish(job)
    service._finish = delayed_finish
    run = activate()['id']; emit(); wait_for(stream_open.is_set); venue.trigger()
    assert terminal.wait(8)
    generation = venue.vault.session_generation
    with venue.vault.guard_session_generation(generation):
        proceed.set(); time.sleep(.05)
        # Una lectura concurrente no es una revocación. El trabajador espera
        # el archivo, sin perder su resultado en un intento oportunista.
        assert service.snapshot(run)['active']
    result = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert result['status'] == 'COMPLETE' and result['archive_error'] is False
    assert read_bot_run(venue.vault,run)['status'] == 'COMPLETE'
    venue.vault.backup(tmp_path/'concurrent-complete.db'); venue.vault.lock()
