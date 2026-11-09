"""Salidas ejecutadas por GRVT: observación y conciliación, sin otro envío."""

from decimal import Decimal
import time
import pytest

from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from app.live_execution.grvt_exit_watch_archive import exit_watch_history
from tests.test_grvt_testnet_bracket_finance import finance_case
from tests.test_grvt_testnet_signer import KEY,SIGNER
from tests.test_grvt_filled_strategy_close import filled_case
from app.derivatives.paper_math import PerpQuote


def wait_finished(service,parent):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        result=service.snapshot(parent)
        if not result['run']['active']: return result
        time.sleep(.01)
    raise AssertionError('Seguimiento nativo sin resultado terminal')


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_native_target_before_activation_is_reconciled_without_market_or_order_submission(tmp_path,direction):
    case=finance_case(tmp_path,direction); parent=case.origin['client_order_id']
    def forbidden(*args,**kwargs): raise AssertionError('La salida nativa no necesita mercado ni nuevos envíos')
    def wait(stop,seconds): case.clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(case.vault,client_factory=lambda key:case.reader,
        public_post=forbidden,stream_factory=forbidden,clock_ns=lambda:case.clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+case.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request'] is None and result['run']['watch']['observations']==0
    group=read_bracket(case.vault,parent)
    assert group['reconciliation']['result']['positions']==[]
    assert Decimal(group['reconciliation']['result']['net_realized_pnl_usdt'])==case.pnl-Decimal('0.02')
    assert group['cancel_attempt'] is None and group['emergency_attempts']==[]
    from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
    assert read_testnet_recovery(case.vault,case.cap.sub_account_id).stopped is True
    assert not case.settings['created_ioc']
    saved=exit_watch_history(case.vault,parent)['items'][0]
    assert saved['status']=='COMPLETE' and saved['observations']==0
    case.vault.backup(tmp_path/'native-target.db'); case.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('role',['STOP_LOSS','TAKE_PROFIT'])
def test_native_exit_is_detected_during_hold_without_a_mark_trigger(tmp_path,direction,role):
    venue,reader,public,clock,state=filled_case(tmp_path,direction); parent=venue.source['client_order_id']
    frames=[]
    class Stream:
        def __enter__(self): return self
        def __exit__(self,*args): frames.append('closed')
        def recv(self,*,timeout):
            clock[0]+=1_000_000_000; frames.append('100')
            if len(frames)==2: venue.trigger(role)
            return 'BTC/USDT:PERP',PerpQuote(venue.cap.instrument,clock[0],Decimal('100'),Decimal('99.99'),Decimal('100.01'))
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=lambda *args:Stream(),clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request'] is None and result['run']['watch']['observations']==4
    assert frames==['100']*5+['closed']
    assert venue.position==0 and venue.cancel_sends==venue.close_sends==0
    group=read_bracket(venue.vault,parent)
    assert group['reconciliation']['result']['positions']==[] and group['emergency_attempts']==[]
    assert exit_watch_history(venue.vault,parent)['items'][0]['status']=='COMPLETE'
    venue.vault.lock()


def test_native_fill_between_entry_audit_and_signal_review_does_not_create_an_ioc(tmp_path):
    from app.live_execution.grvt_testnet_order import FILL_HISTORY_URL
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']; triggered=[]
    original=reader._post
    def post(url,body,headers):
        response=original(url,body,headers)
        if url==FILL_HISTORY_URL and not triggered:
            triggered.append(True); venue.trigger()
        return response
    reader._post=post
    class Stream:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def recv(self,*,timeout):
            clock[0]+=1_000_000_000
            return 'BTC/USDT:PERP',PerpQuote(venue.cap.instrument,clock[0],Decimal('100'),Decimal('99.99'),Decimal('100.01'))
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=lambda key:reader,public_post=public,
        stream_factory=lambda *args:Stream(),clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert triggered and result['run']['status']=='COMPLETE',result
    assert result['request'] is None and venue.cancel_sends==venue.close_sends==0
    venue.vault.lock()


@pytest.mark.parametrize('fault',['partial','unexplained_position','sibling_pending'])
def test_native_exit_does_not_claim_complete_for_partial_inconsistent_or_pending_states(tmp_path,fault):
    case=finance_case(tmp_path,exit_size='0.25' if fault=='partial' else '0.5'); parent=case.origin['client_order_id']
    if fault=='unexplained_position': case.settings['position']='0.1'
    if fault=='sibling_pending':
        sibling=case.settings['venue_state'][case.orders[1]['metadata']['client_order_id']]
        sibling['state'].update(status='OPEN',update_time=str(case.clock[0]))
    def forbidden(*args,**kwargs): raise AssertionError('No se reconstruyen precios ni se envía otro cierre')
    def wait(stop,seconds): case.clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(case.vault,client_factory=lambda key:case.reader,
        public_post=forbidden,stream_factory=forbidden,clock_ns=lambda:case.clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=30,api_key='fake-testnet-key-'+case.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']==('EXPIRED' if fault=='sibling_pending' else 'NEEDS_REVIEW'),result
    assert not case.settings['created_ioc']
    group=read_bracket(case.vault,parent)
    if fault=='partial':
        # Un parcial terminal ahora puede solicitar su recuperación, pero el
        # proveedor de este caso prohíbe la cotización necesaria para reducir.
        assert result['request']['schema']=='orbis.grvt.testnet.native.partial.close.v1'
        assert group['reconciliation']['result']['positions']==[[case.cap.instrument,'0.25']]
    elif fault=='sibling_pending':
        # Este proveedor sintético no retira la hermana al cancelar. La
        # solicitud queda registrada, pero no permite un recibo plano falso.
        assert result['request']['schema']=='orbis.grvt.testnet.native.recovery.close.v1'
        assert group['reconciliation'] is None and group['cancel_attempt'] is not None
        assert any(order['status']=='OPEN' for order in group['last_observation']['orders'])
    else:
        assert result['request'] is None and group['reconciliation'] is None
    if fault!='sibling_pending': assert not group['cancel_attempt']
    assert not group['emergency_attempts']
    assert exit_watch_history(case.vault,parent)['items'][0]['status']==result['run']['status']
    case.vault.lock()


def test_stop_between_native_audits_requires_explicit_activation_to_finish_the_receipt(tmp_path):
    case=finance_case(tmp_path); parent=case.origin['client_order_id']; revoked=[]
    def forbidden(*args,**kwargs): raise AssertionError('La conciliación nativa sólo consulta GRVT')
    def wait(stop,seconds):
        case.clock[0]+=6_000_000_000
        if not revoked:
            revoked.append(True); service.stop(parent)
        return stop.is_set()
    service=GrvtStrategyCloseService(case.vault,client_factory=lambda key:case.reader,
        public_post=forbidden,stream_factory=forbidden,clock_ns=lambda:case.clock[0],waiter=wait)
    credentials={'max_attempts':2,'ttl_seconds':120,'api_key':'fake-testnet-key-'+case.cap.sub_account_id,
        'private_key':KEY.hex(),'expected_signer':SIGNER}
    service.start_watch(parent,**credentials)
    assert wait_finished(service,parent)['run']['status']=='STOPPED'
    assert read_bracket(case.vault,parent)['reconciliation'] is None
    history=exit_watch_history(case.vault,parent)['items']
    assert len(history)==1 and history[0]['stop_cause']=='USER_STOP'
    service.start_watch(parent,**credentials)
    assert wait_finished(service,parent)['run']['status']=='COMPLETE'
    history=exit_watch_history(case.vault,parent)['items']
    assert [item['status'] for item in history]==['COMPLETE','STOPPED']
    assert not case.settings['created_ioc'] and read_bracket(case.vault,parent)['cancel_attempt'] is None
    case.vault.lock()


def test_native_fill_after_a_strategy_close_was_recorded_still_sends_no_reduction(tmp_path):
    from app.live_execution.grvt_filled_close import request_filled_strategy_close
    from app.live_execution.grvt_testnet_strategy_close import run_strategy_close
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    request_filled_strategy_close(venue.vault,reader,parent,max_attempts=2,
        deadline_ns=clock[0]+120_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    venue.trigger()
    def wait(seconds): clock[0]+=6_000_000_000; return False
    result=run_strategy_close(venue.vault,reader,parent,private_key=KEY.hex(),expected_signer=SIGNER,
        public_post=public,clock_ns=lambda:clock[0],wait=wait)
    assert result['status']=='COMPLETE' and result['remaining_position']=='0'
    assert venue.cancel_sends==venue.close_sends==0 and venue.position==0
    assert read_bracket(venue.vault,parent)['reconciliation']['result']['positions']==[]
    venue.vault.lock()
