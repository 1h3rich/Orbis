"""Parciales nativos: contratos, fills, residuo y recibos en un venue controlado."""

from decimal import Decimal
import pytest

from app.derivatives.paper_math import PerpQuote
from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
from tests.test_grvt_filled_strategy_close import filled_case
from tests.test_grvt_native_exit_watch import wait_finished
from tests.test_grvt_testnet_signer import KEY,SIGNER


def test_native_partial_target_closes_only_the_reconciled_residue_and_preserves_evidence(tmp_path):
    venue,reader,public,clock,state=filled_case(tmp_path)
    parent=venue.source['client_order_id']
    venue.trigger(fill_size='0.2',cancel_sibling=False)
    assert venue.position==Decimal('0.3')
    def no_stream(*args): raise AssertionError('Un parcial nativo no necesita un disparador de precio')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=no_stream,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert venue.cancel_sends==venue.close_sends==1 and venue.position==0
    assert Decimal(venue.fills[-1]['size'])==Decimal('0.3')
    request=read_strategy_close(venue.vault,parent)
    assert request['schema']=='orbis.grvt.testnet.native.partial.close.v1'
    assert request['evidence']['reason']=='NATIVE_PARTIAL_TARGET'
    group=read_bracket(venue.vault,parent)
    assert group['reconciliation']['result']['positions']==[[venue.cap.instrument,'0.3']]
    assert len(group['emergency_attempts'])==1
    venue.vault.backup(tmp_path/'native-partial-target.db')
    venue.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('role',['STOP_LOSS','TAKE_PROFIT'])
def test_partial_native_exit_during_hold_recovers_lost_replies_without_duplicate_sends(tmp_path,direction,role):
    venue,reader,public,clock,state=filled_case(tmp_path,direction)
    parent=venue.source['client_order_id']; frames=[]
    venue.lose_cancel_reply=venue.lose_close_reply=True
    class Stream:
        def __enter__(self): return self
        def __exit__(self,*args): frames.append('closed')
        def recv(self,*,timeout):
            clock[0]+=1_000_000_000; frames.append('100')
            if len(frames)==2: venue.trigger(role,fill_size='0.2')
            return 'BTC/USDT:PERP',PerpQuote(venue.cap.instrument,clock[0],Decimal('100'),Decimal('99.99'),Decimal('100.01'))
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=lambda *args:Stream(),clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert venue.cancel_sends==venue.close_sends==1 and venue.position==0
    assert Decimal(venue.fills[-1]['size'])==Decimal('0.3')
    assert frames==['100']*5+['closed']
    assert read_strategy_close(venue.vault,parent)['evidence']['reason']==(
        'NATIVE_PARTIAL_STOP' if role=='STOP_LOSS' else 'NATIVE_PARTIAL_TARGET')
    from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
    recovery=read_testnet_recovery(venue.vault,venue.cap.sub_account_id)
    receipt=next(item.reconciliation for item in recovery.items if item.reconciliation)
    assert Decimal(receipt['result']['settle_balance'])==venue.balance
    venue.vault.backup(tmp_path/'partial-hold.db'); venue.vault.lock()


def test_partial_becoming_full_before_the_request_uses_readonly_settlement(tmp_path):
    from app.live_execution.grvt_testnet_order import LOOKUP_URL
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    venue.trigger(fill_size='0.2'); lookups=[]
    original=reader._post
    def post(url,body,headers):
        if url==LOOKUP_URL:
            lookups.append(body)
            if len(lookups)==4:
                clock[0]+=1_000_000_000; venue.trigger(fill_size='0.3')
        return original(url,body,headers)
    reader._post=post
    def no_stream(*args): raise AssertionError('El cierre nativo no necesita nuevas cotizaciones')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=lambda key:reader,public_post=no_stream,
        stream_factory=no_stream,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    result=wait_finished(service,parent)
    assert result['run']['status']=='COMPLETE',result
    assert result['request'] is None and venue.cancel_sends==venue.close_sends==0 and venue.position==0
    assert read_bracket(venue.vault,parent)['reconciliation']['result']['positions']==[]
    venue.vault.backup(tmp_path/'partial-became-full.db'); venue.vault.lock()


def test_stop_after_partial_cancellation_prevents_reduction_and_resume_keeps_original_limits(tmp_path):
    from app.live_execution.grvt_native_partial_close import request_native_partial_close
    from app.live_execution.grvt_stop_control import read_testnet_stop,release_automatic_stop
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.live_execution.grvt_testnet_oco import _stamp
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    venue.trigger(fill_size='0.2'); revoked=[]
    def no_stream(*args): raise AssertionError('Sólo seguimiento del parcial nativo')
    def wait(stop,seconds):
        clock[0]+=6_000_000_000
        if not revoked:
            revoked.append(True)
            stop_testnet_subaccount(venue.vault,venue.cap.sub_account_id,now=_stamp(clock[0]))
            service.stop(parent)
        return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=no_stream,clock_ns=lambda:clock[0],waiter=wait)
    credentials={'max_attempts':2,'ttl_seconds':120,'api_key':'fake-testnet-key-'+venue.cap.sub_account_id,
        'private_key':KEY.hex(),'expected_signer':SIGNER}
    service.start_watch(parent,**credentials)
    assert wait_finished(service,parent)['run']['status']=='STOPPED'
    assert venue.cancel_sends==1 and venue.close_sends==0 and venue.position==Decimal('0.3')
    request=read_strategy_close(venue.vault,parent)
    assert request_native_partial_close(venue.vault,reader,parent,max_attempts=3,
        deadline_ns=clock[0]+300_000_000_000,clock_ns=lambda:clock[0])==request
    service.start_watch(parent,**credentials)
    resumed=wait_finished(service,parent)
    assert resumed['run']['status']=='COMPLETE' and venue.position==0
    assert read_strategy_close(venue.vault,parent)==request and venue.cancel_sends==venue.close_sends==1
    saved=read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    assert saved['reason']=='MANUAL' and saved['stopped']
    with venue.vault.session() as db,pytest.raises(ValueError,match='stop changed'):
        release_automatic_stop(db,venue.cap.sub_account_id,parent,expected_revision=saved['revision'],now_ns=clock[0])
    venue.vault.backup(tmp_path/'partial-manual-stop.db'); venue.vault.lock()


@pytest.mark.parametrize('fault',['missing_fill','unknown_fill','wrong_balance','both_exits'])
def test_unexplained_partial_activity_never_authorizes_a_residual_order(tmp_path,fault):
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    venue.trigger(fill_size='0.2')
    if fault=='missing_fill': venue.fills.pop()
    elif fault=='unknown_fill':
        venue.fills.append({**venue.fills[0],'trade_id':'foreign-trade','order_id':'0xforeign',
            'client_order_id':str(2**63+700),'size':'0.01'})
    elif fault=='wrong_balance': venue.balance+=Decimal('0.01')
    else: venue.trigger('STOP_LOSS',fill_size='0.2')
    def no_stream(*args): raise AssertionError('No debe recurrir a un disparador de mercado')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=venue.factory,public_post=public,
        stream_factory=no_stream,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    assert wait_finished(service,parent)['run']['status']=='NEEDS_REVIEW'
    group=read_bracket(venue.vault,parent)
    assert venue.close_sends==0 and group['reconciliation'] is None and group['emergency_attempts']==[]
    venue.vault.backup(tmp_path/'unexplained-partial.db'); venue.vault.lock()


def test_a_partial_observation_alone_cannot_rearm_a_subaccount(tmp_path):
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_stop_control import read_testnet_stop,release_automatic_stop
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    venue.trigger(fill_size='0.2')
    observe_bracket_orders(venue.vault,reader,parent,clock_ns=lambda:clock[0])
    saved=read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    assert saved['stopped'] and saved['reason']=='PARTIAL_EXIT' and saved['parent_client_order_id']==parent
    assert read_strategy_close(venue.vault,parent) is None
    with venue.vault.session() as db,pytest.raises(ValueError,match='stop changed'):
        release_automatic_stop(db,venue.cap.sub_account_id,parent,expected_revision=saved['revision'],now_ns=clock[0])
    assert venue.cancel_sends==venue.close_sends==0
    venue.vault.backup(tmp_path/'partial-only.db'); venue.vault.lock()


@pytest.mark.parametrize('fault',['reason','observation','deadline'])
def test_readback_and_backup_reject_changed_partial_evidence_even_with_a_recomputed_hash(tmp_path,fault):
    import json
    from app.live_execution.grvt_native_partial_close import request_native_partial_close,_close_id
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_oco import _encode
    from app.portfolio.models import GrvtTestnetStrategyClose
    from app.portfolio.vault import VaultError
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    venue.trigger(fill_size='0.2')
    request_native_partial_close(venue.vault,reader,parent,max_attempts=2,
        deadline_ns=clock[0]+120_000_000_000,clock_ns=lambda:clock[0])
    with venue.vault.session() as db:
        row=db.get(GrvtTestnetStrategyClose,parent); payload=json.loads(row.payload_json)
        if fault=='reason': payload['evidence']['reason']='NATIVE_PARTIAL_STOP'
        elif fault=='observation': payload['evidence']['native_observation_sequence']+=1
        else: payload['deadline_ns']=payload['created_at_ns']+301_000_000_000
        row.payload_json,row.payload_digest=_encode(payload)
        row.close_client_order_id=_close_id(parent,payload['evidence']); db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_strategy_close(venue.vault,parent)
    with pytest.raises(VaultError): venue.vault.backup(tmp_path/'invalid-partial.db')
    assert venue.cancel_sends==venue.close_sends==0
    venue.vault.lock()


def test_another_native_fill_during_cancellation_reduces_the_newly_reconciled_residue(tmp_path):
    from app.live_execution.grvt_testnet_order import BULK_ORDERS_URL
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    venue.trigger(fill_size='0.2'); original=reader._post; raced=[]
    def post(url,body,headers):
        if url==BULK_ORDERS_URL and not body['orders']:
            raced.append(True); clock[0]+=1_000_000_000
            venue.trigger(fill_size='0.1')
        return original(url,body,headers)
    reader._post=post
    def no_stream(*args): raise AssertionError('El parcial nativo ya es el disparador')
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=lambda key:reader,public_post=public,
        stream_factory=no_stream,clock_ns=lambda:clock[0],waiter=wait)
    service.start_watch(parent,max_attempts=2,ttl_seconds=120,api_key='fake-testnet-key-'+venue.cap.sub_account_id,
        private_key=KEY.hex(),expected_signer=SIGNER)
    assert wait_finished(service,parent)['run']['status']=='COMPLETE'
    assert raced==[True] and venue.cancel_sends==venue.close_sends==1 and venue.position==0
    assert Decimal(venue.fills[-1]['size'])==Decimal('0.2')
    assert read_bracket(venue.vault,parent)['reconciliation']['result']['positions']==[[venue.cap.instrument,'0.2']]
    venue.vault.backup(tmp_path/'native-fill-during-cancel.db'); venue.vault.lock()
