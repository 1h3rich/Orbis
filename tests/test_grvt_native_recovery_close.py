"""Recuperación desde contratos y saldo del proveedor, nunca señales inventadas."""

from decimal import Decimal
import time
import pytest

from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from tests.grvt_bracket_ui_fixture import BracketUiVenue
from tests.test_grvt_native_exit_watch import wait_finished
from tests.test_grvt_testnet_signer import KEY,SIGNER


def unfilled_case(directory):
    venue=BracketUiVenue(directory); venue.fill_entry=False
    client=venue.factory('fake-testnet-key-'+venue.cap.sub_account_id)
    submit_bracket_signal_once(venue.origin.paper,venue.origin.lab,venue.vault,client,
        venue.initial_orders,venue.cap,venue.cap,venue.instrument,session_id=venue.source['session_id'],
        action_request_id=venue.source['action_request_id'],public_post=venue.public)
    clock=[time.time_ns()+1_000_000_000]; venue.clock_ns=lambda:clock[0]
    return venue,clock


@pytest.mark.parametrize('lost',[False,True])
@pytest.mark.parametrize('entry_status',['CANCELLED','REJECTED'])
def test_unfilled_parent_cancels_orphan_exits_and_reconciles_without_an_ioc(tmp_path,lost,entry_status):
    venue,clock=unfilled_case(tmp_path); parent=venue.source['client_order_id']
    venue.states[parent]['state']['status']=entry_status
    venue.lose_cancel_reply=lost
    assert venue.position==0 and not venue.fills
    def forbidden(*args): raise AssertionError('No hay exposición que requiera precio o una orden nueva')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=forbidden,
        stream_factory=forbidden,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request']['exit_reason']=='ORPHANED_EXITS'
    assert venue.cancel_sends==1 and venue.close_sends==0 and venue.balance==Decimal('10000')
    group=read_bracket(venue.vault,parent)
    assert group['reconciliation']['result']['positions']==[]
    assert group['reconciliation']['result']['entry_filled_size']=='0'
    assert group['reconciliation']['result']['net_realized_pnl_usdt']=='0'
    assert group['emergency_attempts']==[]
    venue.vault.backup(tmp_path/'unfilled-orphans.db'); venue.vault.lock()


def test_a_completely_cancelled_unfilled_group_is_reconciled_without_any_order_submission(tmp_path):
    venue,clock=unfilled_case(tmp_path); parent=venue.source['client_order_id']
    for order in venue.states.values():
        order['state'].update(status='CANCELLED',update_time=str(clock[0]))
    def forbidden(*args): raise AssertionError('Un intento sin fills sólo necesita lecturas')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=forbidden,
        stream_factory=forbidden,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request']['exit_reason']=='ENTRY_UNFILLED'
    assert venue.cancel_sends==venue.close_sends==0
    assert read_bracket(venue.vault,parent)['reconciliation']['result']['positions']==[]
    venue.vault.backup(tmp_path/'empty-cancelled-group.db'); venue.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('partial',[False,True])
def test_expired_protection_requires_a_new_recovery_permission_then_closes_the_reconciled_position(tmp_path,direction,partial):
    from tests.test_grvt_filled_strategy_close import filled_case
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue,reader,public,clock,state=filled_case(tmp_path,direction); parent=venue.source['client_order_id']
    if partial: venue.trigger(fill_size='0.2')
    clock[0]=read_bracket(venue.vault,parent)['legs'][1]['expiration_ns']+1_000_000_000
    def forbidden(*args): raise AssertionError('Recuperación no observa nuevas señales ni velas')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=forbidden,clock_ns=lambda:clock[0],waiter=wait)
    credentials={'max_attempts':2,'ttl_seconds':120,'api_key':'fake-testnet-key-'+venue.cap.sub_account_id,
        'private_key':KEY.hex(),'expected_signer':SIGNER}
    with pytest.raises(GrvtTestnetBracketError,match='protección'):
        service.start_watch(parent,**credentials)
    assert venue.cancel_sends==venue.close_sends==0
    service.start_watch(parent,**credentials,recovery_only=True)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request']['exit_reason']=='EXIT_PROTECTION_EXPIRED'
    assert venue.cancel_sends==venue.close_sends==1 and venue.position==0
    group=read_bracket(venue.vault,parent)
    size='0.3' if partial else '0.5'
    assert group['reconciliation']['result']['positions']==[[venue.cap.instrument,size if direction=='LONG' else '-'+size]]
    assert Decimal(venue.fills[-1]['size'])==Decimal(size)
    from app.live_execution.grvt_exit_watch_archive import exit_watch_history
    record=exit_watch_history(venue.vault,parent)['items'][0]
    assert record['mode']=='RECOVERY' and record['observations']==0 and record['status']=='COMPLETE'
    venue.vault.backup(tmp_path/'expired-recovery.db'); venue.vault.lock()


def test_an_unfilled_bot_attempt_consumes_budget_without_inventing_a_trade_or_profit(tmp_path):
    from tests.test_grvt_bot_service import bot_case,wait_for
    from app.live_execution.grvt_bot_archive import read_bot_run
    venue,origin,service,closes,activate,emit,trigger,opened,closed=bot_case(tmp_path)
    venue.fill_entry=False; venue.lose_cancel_reply=True
    run=activate(max_entries=2,total_entry_cap=Decimal('50'),loss_cap=Decimal('10'))['id']
    emit()
    done=wait_for(lambda:(item if not (item:=service.snapshot(run))['active'] else None))
    assert done['status']=='BUDGET_LIMIT',done
    assert done['entry_count']==done['completed_entries']==1 and done['entry_budget_reserved']=='50'
    assert Decimal(done['net_realized_pnl_usdt'])==0 and venue.balance==Decimal('10000')
    assert done['entry_execution']=={'status':'UNFILLED','filled_size':'0','basis':'RECONCILED'}
    assert venue.bulk_sends==venue.cancel_sends==1 and venue.close_sends==0 and not venue.fills
    assert not opened.is_set()
    assert read_bot_run(venue.vault,run)['entry_execution']==done['entry_execution']
    venue.vault.backup(tmp_path/'unfilled-budget.db'); venue.vault.lock()


def test_native_full_exit_with_an_active_sibling_cancels_only_protection_and_never_reduces_again(tmp_path):
    from tests.test_grvt_filled_strategy_close import filled_case
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    venue.trigger(cancel_sibling=False)
    assert venue.position==0
    def forbidden(*args): raise AssertionError('La cuenta plana no requiere mercado ni nuevas posiciones')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=forbidden,
        stream_factory=forbidden,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request']['exit_reason']=='NATIVE_SIBLING_ACTIVE'
    assert venue.cancel_sends==1 and venue.close_sends==0 and venue.position==0
    assert read_bracket(venue.vault,parent)['reconciliation']['result']['positions']==[]
    venue.vault.backup(tmp_path/'native-active-sibling.db'); venue.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_terminal_native_protections_with_a_position_close_only_its_reconciled_residue(tmp_path,direction):
    from tests.test_grvt_filled_strategy_close import filled_case
    venue,reader,public,clock,state=filled_case(tmp_path,direction); parent=venue.source['client_order_id']
    for raw in list(venue.states.values())[1:]:
        raw['state'].update(status='CANCELLED',update_time=str(clock[0]))
    def forbidden(*args): raise AssertionError('La protección terminada no necesita otra señal')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=forbidden,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request']['exit_reason']=='NATIVE_PROTECTION_ENDED'
    assert venue.cancel_sends==0 and venue.close_sends==1 and venue.position==0
    assert Decimal(venue.fills[-1]['size'])==Decimal('0.5')
    venue.vault.backup(tmp_path/'terminal-protections.db'); venue.vault.lock()


@pytest.mark.parametrize('fault',['balance','position','unexpected_fill'])
def test_an_unfilled_group_with_unexplained_account_activity_never_becomes_complete(tmp_path,fault):
    venue,clock=unfilled_case(tmp_path); parent=venue.source['client_order_id']
    if fault=='balance': venue.balance+=Decimal('1')
    elif fault=='position': venue.position=Decimal('0.01')
    else:
        order=venue.states[parent]
        venue.add_fill(order,'0.01',clock[0])
    def forbidden(*args): raise AssertionError('Una inconsistencia no autoriza otra orden')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=forbidden,
        stream_factory=forbidden,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    assert wait_finished(service,parent)['run']['status']=='NEEDS_REVIEW'
    assert venue.close_sends==0 and read_bracket(venue.vault,parent)['reconciliation'] is None
    venue.vault.backup(tmp_path/'unexplained-unfilled.db'); venue.vault.lock()


@pytest.mark.parametrize('fault',['reason','observation','deadline'])
def test_recovery_request_replays_its_own_evidence_when_read_or_backed_up(tmp_path,fault):
    import json
    from app.live_execution.grvt_native_recovery_close import request_native_recovery_close,_close_id
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_oco import _encode
    from app.portfolio.models import GrvtTestnetStrategyClose
    from app.portfolio.vault import VaultError
    venue,clock=unfilled_case(tmp_path); parent=venue.source['client_order_id']
    request_native_recovery_close(venue.vault,venue.factory('fake-testnet-key-'+venue.cap.sub_account_id),parent,
        max_attempts=2,deadline_ns=clock[0]+120_000_000_000,clock_ns=lambda:clock[0])
    with venue.vault.session() as db:
        row=db.get(GrvtTestnetStrategyClose,parent); payload=json.loads(row.payload_json)
        if fault=='reason': payload['evidence']['reason']='ENTRY_UNFILLED'
        elif fault=='observation': payload['evidence']['native_observation_sequence']+=1
        else: payload['deadline_ns']=payload['created_at_ns']+301_000_000_000
        row.payload_json,row.payload_digest=_encode(payload)
        row.close_client_order_id=_close_id(parent,payload['evidence']); db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_strategy_close(venue.vault,parent)
    with pytest.raises(VaultError): venue.vault.backup(tmp_path/'invalid-recovery.db')
    assert venue.cancel_sends==venue.close_sends==0
    venue.vault.lock()


def test_explicit_recovery_does_not_cancel_a_healthy_protected_position(tmp_path):
    from tests.test_grvt_filled_strategy_close import filled_case
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    def forbidden(*args): raise AssertionError('Recuperación no evalúa ni opera posiciones sanas')
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=forbidden,
        stream_factory=forbidden,clock_ns=lambda:clock[0])
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER,recovery_only=True)
    result=wait_finished(service,parent)
    assert result['run']['status']=='NEEDS_REVIEW' and result['request'] is None
    assert venue.cancel_sends==venue.close_sends==0 and venue.position==Decimal('0.5')
    venue.vault.backup(tmp_path/'healthy-group.db'); venue.vault.lock()


def test_stop_during_expired_recovery_prevents_reduction_and_cannot_be_cleared_automatically(tmp_path):
    from tests.test_grvt_filled_strategy_close import filled_case
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.live_execution.grvt_stop_control import read_testnet_stop,release_automatic_stop
    from app.live_execution.grvt_testnet_oco import _stamp
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    clock[0]=read_bracket(venue.vault,parent)['legs'][1]['expiration_ns']+1_000_000_000
    revoked=[]
    def forbidden(*args): raise AssertionError('No se buscan nuevas señales durante recuperación')
    def wait(stop,seconds):
        clock[0]+=6_000_000_000
        if not revoked:
            revoked.append(True)
            stop_testnet_subaccount(venue.vault,venue.cap.sub_account_id,now=_stamp(clock[0]))
            service.stop(parent)
        return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=forbidden,clock_ns=lambda:clock[0],waiter=wait)
    credentials={'max_attempts':2,'ttl_seconds':120,'api_key':'fake-testnet-key-'+venue.cap.sub_account_id,
        'private_key':KEY.hex(),'expected_signer':SIGNER,'recovery_only':True}
    service.start_watch(parent,**credentials)
    assert wait_finished(service,parent)['run']['status']=='STOPPED'
    request=read_strategy_close(venue.vault,parent)
    assert venue.cancel_sends==1 and venue.close_sends==0 and venue.position==Decimal('0.5')
    service.start_watch(parent,**credentials)
    assert wait_finished(service,parent)['run']['status']=='COMPLETE'
    assert read_strategy_close(venue.vault,parent)==request
    saved=read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    assert saved['stopped'] and saved['reason']=='MANUAL'
    with venue.vault.session() as db,pytest.raises(ValueError,match='stop changed'):
        release_automatic_stop(db,venue.cap.sub_account_id,parent,expected_revision=saved['revision'],now_ns=clock[0])
    assert venue.cancel_sends==venue.close_sends==1 and venue.position==0
    venue.vault.backup(tmp_path/'expired-stopped.db'); venue.vault.lock()
