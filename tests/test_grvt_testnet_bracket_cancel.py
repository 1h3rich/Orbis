"""Cancelar grupo OTOCO no cierra una posición ni permite reenvíos."""

from datetime import datetime,timezone
from types import SimpleNamespace
from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,BULK_ORDERS_URL
from app.portfolio.vault import RealVault
from tests.test_grvt_testnet_bracket_submit import live_group,PASSWORD


def cancel_case(tmp_path,*,stopped=True):
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    if stopped: stop_testnet_subaccount(vault,cap.sub_account_id,now=datetime.now(timezone.utc))
    second=RealVault(tmp_path/'real.db'); second.unlock(PASSWORD)
    settings={'lost':False,'ack_only':False,'cancels':[]}
    original_http=client._post
    def post(url,body,headers):
        if url==BULK_ORDERS_URL and body.get('orders')==[]:
            assert read_bracket(second,origin['client_order_id'])['cancel_attempt']['status']=='CLAIMED'
            settings['cancels'].append(body)
            if not settings['ack_only']:
                for actual in recorded['venue_state'].values():
                    if actual['state']['status'] in ('OPEN','PENDING'): actual['state']['status']='CANCELLED'
            if settings['lost']:
                import httpx
                raise httpx.ReadTimeout('synthetic lost group cancellation reply')
            return venue.response(url,{'result':{'orders':[],'cancel_acks':[{'ack':True}]}})
        return original_http(url,body,headers)
    cancel_client=GrvtTestnetOrderClient('fake-testnet-key-'+cap.sub_account_id,http_post=post)
    return SimpleNamespace(venue=venue,origin=origin,vault=vault,second=second,orders=orders,
        cap=cap,instrument=instrument,client=cancel_client,settings=settings,recorded=recorded,http_post=post)


def test_cancel_group_is_claimed_before_network_and_keeps_filled_parent_position(tmp_path):
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    case=cancel_case(tmp_path)
    result=cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    assert len(case.settings['cancels'])==1
    assert case.settings['cancels'][0]=={'sub_account_id':case.cap.sub_account_id,'orders':[],
        'order_i_ds':[],'client_order_i_ds':[case.orders[1]['metadata']['client_order_id']],'time_to_live_ms':'5000'}
    assert result['cancel_attempt']['status']=='ACKNOWLEDGED'
    assert result['cancel_attempt']['financially_reconciled'] is False
    assert [row['status'] for row in result['last_observation']['orders']]==['FILLED','CANCELLED','CANCELLED']
    assert result['last_observation']['residual_size_from_orders']=='0.5'
    assert result['status']=='ACKNOWLEDGED' and result['can_submit'] is False
    case.vault.backup(tmp_path/'cancelled.db')
    case.second.lock(); case.vault.lock()


def test_lost_cancel_reply_only_allows_recovery_after_reopen(tmp_path):
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderError
    case=cancel_case(tmp_path); case.settings['lost']=True
    with pytest.raises(GrvtTestnetOrderError,match='desconocido'):
        cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    case.vault.lock(); reopened=RealVault(tmp_path/'real.db'); reopened.unlock(PASSWORD)
    assert read_bracket(reopened,case.origin['client_order_id'])['cancel_attempt']['status']=='CLAIMED'
    client=GrvtTestnetOrderClient('fake-testnet-key-'+case.cap.sub_account_id,http_post=case.http_post)
    with pytest.raises(GrvtTestnetBracketError,match='consumida'):
        cancel_bracket_once(reopened,client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    observed=observe_bracket_orders(reopened,client,case.origin['client_order_id'])
    assert [row['status'] for row in observed['orders']]==['FILLED','CANCELLED','CANCELLED']
    assert observed['residual_size_from_orders']=='0.5' and len(case.settings['cancels'])==1
    reopened.backup(tmp_path/'uncertain-cancel.db'); reopened.lock(); case.second.lock()


@pytest.mark.parametrize('consent',[False,1,'true',None])
def test_unconfirmed_cancel_does_not_even_query_the_exchange(tmp_path,consent):
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=cancel_case(tmp_path); calls=list(case.recorded['private'])
    with pytest.raises(GrvtTestnetBracketError,match='Confirma'):
        cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=consent)
    assert case.recorded['private']==calls and not case.settings['cancels']
    assert read_bracket(case.vault,case.origin['client_order_id'])['cancel_attempt'] is None
    case.second.lock(); case.vault.lock()


def test_cancel_ack_does_not_close_the_position_or_prove_children_terminal(tmp_path):
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=cancel_case(tmp_path); case.settings['ack_only']=True
    result=cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    assert [row['status'] for row in result['last_observation']['orders']]==['FILLED','OPEN','OPEN']
    assert result['cancel_attempt']['position_closed'] is False
    with pytest.raises(GrvtTestnetBracketError,match='consumida'):
        cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    assert len(case.settings['cancels'])==1
    case.second.lock(); case.vault.lock()


def test_stop_is_required_and_terminal_group_never_generates_a_cancel_post(tmp_path):
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=cancel_case(tmp_path,stopped=False); calls=list(case.recorded['private'])
    with pytest.raises(GrvtTestnetBracketError,match='Detén'):
        cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    assert case.recorded['private']==calls
    stop_testnet_subaccount(case.vault,case.cap.sub_account_id,now=datetime.now(timezone.utc))
    for state in case.recorded['venue_state'].values():
        if state['state']['status']=='OPEN': state['state']['status']='CANCELLED'
    result=cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    assert not case.settings['cancels'] and result['cancel_attempt'] is None
    assert result['last_observation']['residual_size_from_orders']=='0.5' and result['can_submit'] is False
    case.second.lock(); case.vault.lock()


def test_partial_exit_is_retained_when_the_last_open_member_is_cancelled(tmp_path):
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    case=cancel_case(tmp_path)
    stop_id,target_id=[order['metadata']['client_order_id'] for order in case.orders[1:]]
    case.recorded['venue_state'][stop_id]['state'].update(status='CANCELLED',traded_size=['0'])
    case.recorded['venue_state'][target_id]['state']['traded_size']=['0.2']
    result=cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    assert case.settings['cancels'][0]['client_order_i_ds']==[target_id]
    assert result['last_observation']['residual_size_from_orders']=='0.3'
    assert result['last_observation']['orders'][2]['traded_size']=='0.2'
    case.vault.backup(tmp_path/'partial-exit-cancel.db'); case.second.lock(); case.vault.lock()


def test_expired_claim_before_network_is_kept_without_sending_a_cancel(tmp_path):
    import time
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=cancel_case(tmp_path)
    def clock():
        claimed=read_bracket(case.vault,case.origin['client_order_id'])['cancel_attempt'] is not None
        return time.time_ns()+(6_000_000_000 if claimed else 0)
    with pytest.raises(GrvtTestnetBracketError,match='revisión final'):
        cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],
            acknowledge_unprotected_position=True,clock_ns=clock)
    assert not case.settings['cancels']
    assert read_bracket(case.vault,case.origin['client_order_id'])['cancel_attempt']['status']=='CLAIMED'
    case.vault.backup(tmp_path/'expired-cancel.db'); case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('fault',['foreign_target','future_stop','missing_observation','false_ack'])
def test_rehashed_cancel_corruption_is_rejected_by_read_and_backup(tmp_path,fault):
    import json
    from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_oco import _encode
    from app.portfolio.models import GrvtTestnetBracketCancel
    from app.portfolio.vault import VaultError
    case=cancel_case(tmp_path)
    cancel_bracket_once(case.vault,case.client,case.origin['client_order_id'],acknowledge_unprotected_position=True)
    with case.vault.session() as db:
        row=db.get(GrvtTestnetBracketCancel,case.origin['client_order_id'])
        payload=json.loads(row.payload_json)
        if fault=='foreign_target': payload['target_client_order_id']=str(2**63+999)
        if fault=='future_stop': payload['stop_at_ns']=payload['claimed_at_ns']+1
        if fault=='missing_observation': payload['observation_sequence']=999
        if fault=='false_ack':
            ack=json.loads(row.ack_json); ack['acknowledgement_received']=1
        row.payload_json,row.payload_digest=_encode(payload)
        if fault!='false_ack':
            ack=json.loads(row.ack_json); ack['cancel_digest']=row.payload_digest
        row.ack_json,row.ack_digest=_encode(ack); db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_bracket(case.vault,case.origin['client_order_id'])
    with pytest.raises(VaultError): case.vault.backup(tmp_path/'corrupt-cancel.db')
    case.second.lock(); case.vault.lock()


def test_v38_group_migrates_with_existing_claim_and_observation_intact(tmp_path):
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    case=cancel_case(tmp_path)
    observe_bracket_orders(case.vault,case.client,case.origin['client_order_id'])
    before=read_bracket(case.vault,case.origin['client_order_id'])
    with case.vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_cancels')
        db.connection().exec_driver_sql('PRAGMA user_version = 38'); db.commit()
    case.second.lock(); case.vault.lock()
    reopened=RealVault(tmp_path/'real.db'); reopened.unlock(PASSWORD)
    assert read_bracket(reopened,case.origin['client_order_id'])==before
    assert reopened.last_migration_backup.read_bytes()[:16]!=b'SQLite format 3\x00'
    with reopened.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    reopened.backup(tmp_path/'migrated-v39.db'); reopened.lock()
