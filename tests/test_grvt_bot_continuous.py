"""Varios ciclos de la misma estrategia, con límites y parada conservados."""

from decimal import Decimal
import time
import pytest
from threading import Event

from app.derivatives.paper_math import PerpQuote
from tests.test_grvt_bot_service import bot_case, wait_for


def next_paper_open(venue, origin):
    stamp = venue.clock_ns()
    venue.origin.clock['now'] = stamp//1_000_000_000
    venue.origin.monitor.clock_ns = venue.clock_ns
    assert venue.origin.monitor.on_risk_quote(origin['session_id'],
        PerpQuote(venue.cap.instrument,stamp,Decimal('110'),Decimal('109.99'),Decimal('110.01'))) == 'EXECUTED'
    # Se observa la siguiente vela realmente cerrada. No se insertan señales
    # ni operaciones a mano en la base para producir el segundo episodio.
    for _ in range(2):
        stamp = venue.clock_ns()
        next_second = (stamp//1_000_000_000//3600+1)*3600+2
        while venue.origin.rows[-1]['t'] < next_second//3600*3600:
            previous = venue.origin.rows[-1]; close = Decimal(previous['c'])-1
            venue.origin.rows.append({'t':previous['t']+3600,'o':str(close+1),'h':str(close+2),
                'l':str(close-1),'c':str(close),'v':str(Decimal(previous['v'])+1)})
        venue.advance_bot_clock(next_second-stamp//1_000_000_000)
        venue.origin.clock['now'] = venue.clock_ns()//1_000_000_000
        venue.origin.monitor.poll(origin['session_id'])


@pytest.mark.parametrize('first_exit',['NATIVE','STRATEGY','NATIVE_PARTIAL','ORPHAN'])
def test_one_permission_can_complete_two_new_causal_entries_and_stops_at_the_entry_limit(tmp_path,first_exit):
    from app.live_execution.grvt_bot_archive import read_bot_run
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path,
        risk_heartbeat=first_exit in ('NATIVE_PARTIAL','ORPHAN'))
    if first_exit=='ORPHAN': venue.fill_entry=False
    run = activate(max_entries=2,total_entry_cap=Decimal('100'),loss_cap=Decimal('10'),wait_seconds=10800)['id']
    emit()
    if first_exit!='ORPHAN': wait_for(opened.is_set)
    if first_exit == 'NATIVE': venue.trigger()
    elif first_exit == 'NATIVE_PARTIAL':
        venue.trigger(fill_size='0.2',cancel_sibling=False)
        venue.advance_bot_clock(6)
    elif first_exit=='STRATEGY': trigger.set()
    first = wait_for(lambda: (item if (item := service.snapshot(run)).get('completed_entries') == 1
        and item['status'] == 'WAITING_SIGNAL' else None))
    assert first['active'] and venue.bulk_sends == 1 and venue.position == 0
    assert Decimal(first['net_realized_pnl_usdt']) == venue.balance-Decimal('10000')
    if first_exit=='ORPHAN':
        assert first['entry_execution']['status']=='UNFILLED' and Decimal(first['net_realized_pnl_usdt'])==0
    # Un proceso nuevo recupera sólo el registro; la copia de una concesión
    # activa nunca se convierte por sí sola en autoridad o conexión de red.
    from app.portfolio.vault import RealVault
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    from app.live_execution.grvt_bot_service import GrvtBotService
    from tests.test_grvt_testnet_bracket_submit import PASSWORD
    active_copy = tmp_path/'active-copy.db'; venue.vault.backup(active_copy)
    recovered = RealVault(active_copy); recovered.unlock(PASSWORD)
    def forbidden(*args): raise AssertionError('Consultar un backup no inicia el exchange')
    recovered_service = GrvtBotService(recovered,GrvtStrategyCloseService(recovered,client_factory=forbidden,public_post=forbidden),
        client_factory=forbidden,public_post=forbidden)
    cold = recovered_service.snapshot(run)
    assert cold['status'] == 'INTERRUPTED' and cold['active'] is False
    assert cold['permission_remaining_seconds'] == 0 and cold['authorization'] is False
    recovered.lock()
    opened.clear(); trigger.clear(); venue.fill_entry=True
    if first_exit in ('NATIVE_PARTIAL','ORPHAN'): venue.advance_bot_clock(1)
    next_paper_open(venue,origin)
    wait_for(lambda:venue.bulk_sends == 2); wait_for(opened.is_set); venue.trigger()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'ENTRY_LIMIT' and done['completed_entries'] == 2, done
    assert venue.position == 0 and venue.bulk_sends == 2
    assert venue.close_sends == (0 if first_exit in ('NATIVE','ORPHAN') else 1)
    assert venue.cancel_sends == (0 if first_exit == 'NATIVE' else 1)
    assert len({cycle['selection']['parent_client_order_id'] for cycle in done['cycles']}) == 2
    saved = read_bot_run(venue.vault,run)
    assert saved['status'] == 'ENTRY_LIMIT' and saved['entry_budget_reserved'] == '100'
    assert Decimal(saved['net_realized_pnl_usdt']) > 0
    assert Decimal(saved['net_realized_pnl_usdt']) == venue.balance-Decimal('10000')
    import json
    from app.live_execution.grvt_bot_executions import export_bot_research, read_bot_executions
    chart = read_bot_executions(venue.vault, run, venue.cap.sub_account_id)
    expected = {
        'NATIVE':['ENTRY','TAKE_PROFIT'], 'STRATEGY':['ENTRY','IOC'],
        'NATIVE_PARTIAL':['ENTRY','TAKE_PROFIT','IOC'], 'ORPHAN':[],
    }[first_exit]
    assert [fill['role'] for fill in chart['executions'] if fill['cycle'] == 1] == expected
    assert [fill['role'] for fill in chart['executions'] if fill['cycle'] == 2] == ['ENTRY','TAKE_PROFIT']
    exported = export_bot_research(venue.vault, run, venue.cap.sub_account_id)
    assert exported['record_count']==2 and exported['fill_count']==len(expected)+2
    first,second = map(json.loads,exported['jsonl'].splitlines())
    assert [first['cycle'],second['cycle']]==[1,2]
    assert first['outcome']['entry_evidence']==('RECONCILED_UNFILLED' if first_exit=='ORPHAN' else 'RECONCILED_ENTRY')
    assert [fill['role'] for fill in first['outcome']['fills']]==expected
    assert second['outcome']['entry_evidence']=='RECONCILED_ENTRY'
    venue.vault.backup(tmp_path/'continuous-two.db'); venue.vault.lock()


@pytest.mark.parametrize('total,loss,role,status',[
    ('50','10','TAKE_PROFIT','BUDGET_LIMIT'),
    ('200','1','STOP_LOSS','LOSS_LIMIT'),
])
def test_aggregate_budget_and_reconciled_net_loss_stop_before_another_entry(tmp_path,total,loss,role,status):
    from app.live_execution.grvt_bot_archive import read_bot_run
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path)
    run = activate(max_entries=4,total_entry_cap=Decimal(total),loss_cap=Decimal(loss),wait_seconds=10800)['id']
    emit(); wait_for(opened.is_set); venue.trigger(role)
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == status and done['completed_entries'] == done['entry_count'] == 1
    assert venue.bulk_sends == 1 and venue.position == 0
    if status == 'LOSS_LIMIT': assert Decimal(done['net_realized_pnl_usdt']) <= -Decimal(loss)
    assert read_bot_run(venue.vault,run)['status'] == status
    from app.live_execution.grvt_bot_executions import read_bot_executions
    chart = read_bot_executions(venue.vault, run, venue.cap.sub_account_id)
    assert [fill['role'] for fill in chart['executions']] == ['ENTRY',role]
    venue.vault.backup(tmp_path/'limit.db'); venue.vault.lock()


def test_manual_stop_before_native_completion_cannot_be_cleared_for_the_next_cycle(tmp_path):
    from app.live_execution.grvt_stop_control import read_testnet_stop
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.live_execution.grvt_testnet_oco import _stamp
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path)
    run = activate(max_entries=2,total_entry_cap=Decimal('100'),loss_cap=Decimal('10'),wait_seconds=10800)['id']
    emit(); wait_for(opened.is_set)
    stop_testnet_subaccount(venue.vault,venue.cap.sub_account_id,now=_stamp(venue.clock_ns()))
    venue.trigger()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] in ('STOPPED','NEEDS_REVIEW') and venue.bulk_sends == 1
    stop = read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    assert stop['stopped'] and stop['reason'] == 'MANUAL'
    assert done['cycles'][0]['rearm'] is None
    next_paper_open(venue,origin); time.sleep(.05); assert venue.bulk_sends == 1
    venue.vault.backup(tmp_path/'manual-after-close.db'); venue.vault.lock()


def test_finished_run_can_be_explicitly_rearmed_without_reusing_its_permission(tmp_path):
    from app.live_execution.grvt_stop_control import read_testnet_stop
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path)
    run = activate(max_entries=2,total_entry_cap=Decimal('50'),loss_cap=Decimal('10'),wait_seconds=10800)['id']
    emit(); wait_for(opened.is_set); venue.trigger()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'BUDGET_LIMIT'
    stop = read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    result = service.rearm_finished(run,expected_ticket=service.permission_ticket(venue.cap.sub_account_id),
        expected_stop_revision=stop['revision'])
    assert result['stopped'] is False and venue.bulk_sends == 1
    second = activate(max_entries=2,total_entry_cap=Decimal('100'),loss_cap=Decimal('10'),wait_seconds=10800)['id']
    time.sleep(.05)
    assert service.snapshot(second)['status'] == 'WAITING_SIGNAL' and venue.bulk_sends == 1
    service.stop(second); wait_for(lambda:not service.snapshot(second)['active'])
    venue.vault.backup(tmp_path/'explicit-rearm.db'); venue.vault.lock()


def test_repeated_stop_during_final_archiving_preserves_the_terminal_state(tmp_path):
    from app.live_execution.grvt_bot_archive import read_bot_run
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path)
    entered = Event(); release = Event(); finish = service._finish
    def paused(job):
        entered.set(); assert release.wait(5); finish(job)
    service._finish = paused
    run = activate(max_entries=2,total_entry_cap=Decimal('100'),loss_cap=Decimal('10'))['id']
    service.stop(run); assert entered.wait(5)
    service.stop_all('VAULT_LOCKED'); release.set()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'STOPPED' and done['archive_error'] is False
    assert read_bot_run(venue.vault,run)['status'] == 'STOPPED'
    assert venue.bulk_sends == 0
    venue.vault.lock()


def test_backup_rejects_a_removed_cycle_even_if_the_run_itself_remains(tmp_path):
    from app.portfolio.models import GrvtTestnetBotCycle
    from app.portfolio.vault import VaultError
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path)
    run = activate(max_entries=2,total_entry_cap=Decimal('100'),loss_cap=Decimal('10'),wait_seconds=10800)['id']
    emit(); wait_for(opened.is_set); venue.trigger()
    wait_for(lambda:(item if (item:=service.snapshot(run)).get('completed_entries') == 1 and item['status'] == 'WAITING_SIGNAL' else None))
    service.stop(run); wait_for(lambda:not service.snapshot(run)['active'])
    with venue.vault.session() as db:
        db.delete(db.get(GrvtTestnetBotCycle,(run,1))); db.commit()
    with pytest.raises(VaultError): venue.vault.backup(tmp_path/'missing-cycle.db')
    venue.vault.lock()
