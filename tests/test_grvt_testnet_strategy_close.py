"""Cierre causal PAPER → conciliación nativa → reducción; HTTP ficticio únicamente."""

from decimal import Decimal
import time

import pytest

from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from app.portfolio.vault import RealVault
from tests.test_grvt_testnet_bracket_emergency import emergency_case
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER


def close_source(case):
    mark=Decimal('110') if case.origin['direction']=='LONG' else Decimal('80')
    assert case.venue.observe_risk_mark(case.origin['session_id'],mark)=='EXECUTED'
    with case.venue.paper.connect() as db:
        row=db.execute("SELECT request_id FROM paper_signal_actions WHERE session_id=? AND action='CLOSE'",
            (case.origin['session_id'],)).fetchone()
    return row['request_id']


def test_strategy_close_resumes_after_reopen_and_reduces_only_the_actual_residual(tmp_path):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,read_strategy_close,advance_strategy_close
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    granted=request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        action_request_id=request,max_attempts=2,deadline_ns=case.clock[0]+120_000_000_000,
        clock_ns=lambda:case.clock[0])
    assert granted['close_source']['client_order_id']==client_id_for_signal(case.origin['session_id'],request)
    assert not case.controls['sent']
    case.vault.lock(); case.vault=RealVault(tmp_path/'real.db'); case.vault.unlock(PASSWORD)
    assert read_strategy_close(case.vault,parent)==granted
    for _ in range(12):
        result=advance_strategy_close(case.vault,case.reader,parent,private_key=KEY.hex(),expected_signer=SIGNER,
            public_post=case.public,clock_ns=lambda:case.clock[0])
        if result['status']=='COMPLETE': break
        case.clock[0]+=6_000_000_000
    assert result['status']=='COMPLETE',result
    assert result['remaining_position']=='0'
    assert len(case.controls['sent'])==1
    order=case.controls['sent'][0]
    assert order['reduce_only'] is True and order['legs'][0]['size']=='0.3'
    case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_partial_close_stops_at_the_authorized_attempt_limit_and_reports_the_residual(tmp_path,direction):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,advance_strategy_close
    case=emergency_case(tmp_path,direction); parent=case.origin['client_order_id']; request=close_source(case)
    case.controls['fill_size']='0.1'
    request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        action_request_id=request,max_attempts=1,deadline_ns=case.clock[0]+120_000_000_000,clock_ns=lambda:case.clock[0])
    for _ in range(8):
        result=advance_strategy_close(case.vault,case.reader,parent,private_key=KEY.hex(),expected_signer=SIGNER,
            public_post=case.public,clock_ns=lambda:case.clock[0])
        if result['status']=='ATTEMPT_LIMIT': break
        case.clock[0]+=6_000_000_000
    assert result['status']=='ATTEMPT_LIMIT'
    assert Decimal(result['remaining_position'])==Decimal('0.2')*(1 if direction=='LONG' else -1)
    assert len(case.controls['sent'])==1
    case.second.lock(); case.vault.lock()


def test_frequent_ticks_wait_between_audits_instead_of_postponing_reconciliation_forever(tmp_path):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,advance_strategy_close
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        action_request_id=request,max_attempts=1,deadline_ns=case.clock[0]+60_000_000_000,clock_ns=lambda:case.clock[0])
    states=[]
    for _ in range(14):
        result=advance_strategy_close(case.vault,case.reader,parent,private_key=KEY.hex(),expected_signer=SIGNER,
            public_post=case.public,clock_ns=lambda:case.clock[0])
        states.append(result['status'])
        if result['status']=='COMPLETE': break
        case.clock[0]+=1_000_000_000
    assert states[-1]=='COMPLETE',states
    assert 'WAITING_AUDIT' in states and len(case.controls['sent'])==1
    case.second.lock(); case.vault.lock()


def test_retry_cannot_extend_the_deadline_or_attempt_budget_and_expiry_sends_nothing(tmp_path):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,advance_strategy_close
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    arguments=dict(action_request_id=request,max_attempts=1,deadline_ns=case.clock[0]+60_000_000_000,
        clock_ns=lambda:case.clock[0])
    original=request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,**arguments)
    assert request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        **{**arguments,'max_attempts':3,'deadline_ns':case.clock[0]+180_000_000_000})==original
    case.clock[0]=original['deadline_ns']+1
    response=advance_strategy_close(case.vault,case.reader,parent,private_key=KEY.hex(),expected_signer=SIGNER,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    assert response['status']=='EXPIRED' and not case.controls['sent']
    case.second.lock(); case.vault.lock()


def test_minimum_ttl_allows_time_spent_verifying_the_source_without_extending_the_deadline(tmp_path):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    deadline=case.clock[0]+10_000_000_000
    def ticking():
        at=case.clock[0]; case.clock[0]+=1; return at
    saved=request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,action_request_id=request,
        max_attempts=1,deadline_ns=deadline,clock_ns=ticking)
    assert saved['deadline_ns']==deadline and saved['created_at_ns']<deadline
    case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('fault',['open_action','expired_close','other_position','missing_open_book'])
def test_only_a_recent_close_of_the_same_paper_position_can_request_the_reduction(tmp_path,fault):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,read_strategy_close
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    if fault=='open_action': request=case.origin['action_request_id']
    if fault=='expired_close': case.clock[0]+=21_000_000_000
    if fault=='other_position':
        # Se prueba también la relación entre las dos acciones: compartir
        # sesión/versión no basta. El cierre por sí mismo sigue siendo válido.
        with case.venue.paper.connect() as db:
            db.execute("UPDATE paper_signal_actions SET position_id=? WHERE request_id=?",
                ('00000000-0000-0000-0000-000000000001',case.origin['action_request_id']))
    if fault=='missing_open_book':
        with case.venue.paper.connect() as db:
            db.execute('DELETE FROM paper_perp_events WHERE request_id=?',(case.origin['action_request_id'],))
    with pytest.raises((ValueError,RuntimeError)):
        request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
            action_request_id=request,max_attempts=1,deadline_ns=case.clock[0]+60_000_000_000,clock_ns=lambda:case.clock[0])
    assert read_strategy_close(case.vault,parent) is None and not case.controls['sent']
    case.second.lock(); case.vault.lock()


def test_v41_migration_keeps_native_receipts_and_a_recoverable_encrypted_preimage(tmp_path):
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close,request_strategy_close
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']
    before=read_bracket(case.vault,parent)
    case.second.lock()
    with case.vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_strategy_closes')
        db.connection().exec_driver_sql('PRAGMA user_version=41'); db.commit()
    case.vault.lock(); migrated=RealVault(tmp_path/'real.db'); migrated.unlock(PASSWORD)
    assert migrated.last_migration_backup is not None
    assert migrated.last_migration_backup.read_bytes()[:16]!=b'SQLite format 3\x00'
    assert read_bracket(migrated,parent)==before and read_strategy_close(migrated,parent) is None
    request=close_source(case)
    request_strategy_close(case.venue.paper,case.venue.lab,migrated,parent,
        action_request_id=request,max_attempts=1,deadline_ns=case.clock[0]+60_000_000_000,clock_ns=lambda:case.clock[0])
    migrated.backup(tmp_path/'strategy-close-backup.db'); migrated.lock()


def test_runner_completes_the_bounded_close_without_manual_audit_clicks_after_a_lost_reply(tmp_path):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,run_strategy_close
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        action_request_id=request,max_attempts=2,deadline_ns=case.clock[0]+60_000_000_000,clock_ns=lambda:case.clock[0])
    case.controls['lost']=True
    def wait(seconds): case.clock[0]+=6_000_000_000; return False
    result=run_strategy_close(case.vault,case.reader,parent,private_key=KEY.hex(),expected_signer=SIGNER,
        public_post=case.public,clock_ns=lambda:case.clock[0],wait=wait)
    assert result['status']=='COMPLETE' and result['remaining_position']=='0'
    assert len(case.controls['sent'])==1
    case.second.lock(); case.vault.lock()


def test_bad_signer_is_rejected_before_cancelling_or_contacting_the_provider(tmp_path):
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,advance_strategy_close
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        action_request_id=request,max_attempts=1,deadline_ns=case.clock[0]+60_000_000_000,clock_ns=lambda:case.clock[0])
    calls=[]
    def public(url,body): calls.append(url); return case.public(url,body)
    with pytest.raises(RuntimeError):
        advance_strategy_close(case.vault,case.reader,parent,private_key=KEY.hex(),expected_signer='0x'+'f'*40,
            public_post=public,clock_ns=lambda:case.clock[0])
    assert calls==[] and not case.controls['sent']
    case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('cause',['stop','reopen'])
def test_runner_stops_between_steps_and_never_adopts_a_new_vault_session(tmp_path,cause):
    from threading import Event
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,run_strategy_close
    from app.portfolio.vault import VaultError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case); stop=Event()
    request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        action_request_id=request,max_attempts=2,deadline_ns=case.clock[0]+60_000_000_000,clock_ns=lambda:case.clock[0])
    def wait(seconds):
        if cause=='stop': stop.set(); return True
        case.vault.lock(); case.vault.unlock(PASSWORD); return False
    def run():
        return run_strategy_close(case.vault,case.reader,parent,private_key=KEY.hex(),expected_signer=SIGNER,
            public_post=case.public,clock_ns=lambda:case.clock[0],stop_event=stop,wait=wait)
    if cause=='stop': assert run()['status']=='STOPPED'
    else:
        with pytest.raises(VaultError): run()
    assert len(case.controls['sent'])==1
    case.second.lock(); case.vault.lock()


def test_two_vault_connections_cannot_advance_the_same_close_concurrently(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,advance_strategy_close
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,ORDER_URL
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; request=close_source(case)
    request_strategy_close(case.venue.paper,case.venue.lab,case.vault,parent,
        action_request_id=request,max_attempts=1,deadline_ns=case.clock[0]+60_000_000_000,clock_ns=lambda:case.clock[0])
    entered,release=Event(),Event()
    def post(url,body,headers):
        if url==ORDER_URL: entered.set(); assert release.wait(10)
        return case.reader._post(url,body,headers)
    client=GrvtTestnetOrderClient('fake-testnet-key-'+case.cap.sub_account_id,http_post=post,clock_ns=lambda:case.clock[0])
    def advance(vault):
        return advance_strategy_close(vault,client,parent,private_key=KEY.hex(),expected_signer=SIGNER,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(advance,case.vault)
        try:
            assert entered.wait(10)
            with pytest.raises(RuntimeError,match='Otro paso'): advance(case.second)
        finally: release.set()
        assert future.result(timeout=10)['status']=='CLOSE_SENT'
    assert len(case.controls['sent'])==1
    case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('fault',['none','cancel_reply','close_reply'])
def test_signal_close_cancels_native_exits_and_recovers_uncertain_steps_without_reposting(tmp_path,fault):
    from types import SimpleNamespace
    import httpx
    from app.live_execution.grvt_testnet_strategy_close import request_strategy_close,advance_strategy_close
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_quote import TESTNET_TICKER_URL
    from tests.grvt_bracket_ui_fixture import BracketUiVenue
    venue=BracketUiVenue(tmp_path); client=venue.factory('fake-testnet-key-'+venue.cap.sub_account_id)
    submit_bracket_signal_once(venue.origin.paper,venue.origin.lab,venue.vault,client,venue.initial_orders,
        venue.cap,venue.cap,venue.instrument,session_id=venue.source['session_id'],
        action_request_id=venue.source['action_request_id'],public_post=venue.public)
    clock=[time.time_ns()+1_000_000_000]; venue.clock_ns=lambda:clock[0]
    venue.lose_cancel_reply=fault=='cancel_reply'; venue.lose_close_reply=fault=='close_reply'
    case=SimpleNamespace(origin=venue.source,venue=venue.origin)
    request=close_source(case); parent=venue.source['client_order_id']
    original_public=venue.public
    def public(url,body):
        response=original_public(url,body)
        if url==TESTNET_TICKER_URL:
            data=response.json(); data['result']['event_time']=str(clock[0])
            return venue.origin.response(url,data)
        return response
    request_strategy_close(venue.origin.paper,venue.origin.lab,venue.vault,parent,
        action_request_id=request,max_attempts=2,deadline_ns=clock[0]+120_000_000_000,clock_ns=lambda:clock[0])
    client=venue.factory('fake-testnet-key-'+venue.cap.sub_account_id)
    errors=[]
    for _ in range(12):
        try:
            result=advance_strategy_close(venue.vault,client,parent,private_key=KEY.hex(),expected_signer=SIGNER,
                public_post=public,clock_ns=lambda:clock[0])
            if result['status']=='COMPLETE': break
        except (httpx.ReadTimeout,RuntimeError) as exc:
            errors.append(type(exc).__name__)
        clock[0]+=6_000_000_000
    assert result['status']=='COMPLETE',result
    assert venue.cancel_sends==venue.close_sends==1 and venue.position==0
    assert bool(errors)==(fault!='none')
    venue.vault.lock()
