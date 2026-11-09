"""Salida desde fills → cancelación/conciliación/reducción, sólo venue ficticio."""

from decimal import Decimal
import time

import pytest

from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
from app.portfolio.vault import RealVault
from tests.grvt_bracket_ui_fixture import BracketUiVenue
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER


def filled_case(directory,direction='LONG'):
    venue=BracketUiVenue(directory,direction)
    client=venue.factory('fake-testnet-key-'+venue.cap.sub_account_id)
    submit_bracket_signal_once(venue.origin.paper,venue.origin.lab,venue.vault,client,
        venue.initial_orders,venue.cap,venue.cap,venue.instrument,session_id=venue.source['session_id'],
        action_request_id=venue.source['action_request_id'],public_post=venue.public)
    clock=[time.time_ns()+1_000_000_000]; venue.clock_ns=lambda:clock[0]
    state={'mark':'108.1' if direction=='LONG' else '91.9'}
    original=venue.public
    def public(url,body):
        reply=original(url,body)
        if url.endswith('/ticker'):
            data=reply.json(); data['result'].update(mark_price=state['mark'],event_time=str(clock[0]))
            return venue.origin.response(url,data)
        return reply
    return venue,venue.factory('fake-testnet-key-'+venue.cap.sub_account_id),public,clock,state


def test_a_fill_based_exit_completes_without_a_paper_close_and_survives_reopen(tmp_path):
    from app.live_execution.grvt_filled_close import request_filled_strategy_close
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close,run_strategy_close
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    venue,client,public,clock,state=filled_case(tmp_path)
    parent=venue.source['client_order_id']
    paper_perp_access.lock(venue.origin.paper.path); lab_access.lock(venue.origin.lab.path)
    request=request_filled_strategy_close(venue.vault,client,parent,max_attempts=2,
        deadline_ns=clock[0]+120_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    assert request['schema']=='orbis.grvt.testnet.strategy.close.v2'
    assert request['evidence']['decision']['reason']=='TARGET'
    assert 'close_source' not in request and 'paper_position_id' not in request
    venue.lose_cancel_reply=venue.lose_close_reply=True
    def wait(seconds): clock[0]+=6_000_000_000; return False
    result=run_strategy_close(venue.vault,client,parent,private_key=KEY.hex(),expected_signer=SIGNER,
        public_post=public,clock_ns=lambda:clock[0],wait=wait)
    assert result['status']=='COMPLETE' and result['remaining_position']=='0'
    assert venue.cancel_sends==venue.close_sends==1 and venue.position==0
    venue.vault.lock(); reopened=RealVault(tmp_path/'real.db'); reopened.unlock(PASSWORD)
    assert read_strategy_close(reopened,parent)==request
    reopened.backup(tmp_path/'filled-exit-backup.db'); reopened.lock()


def test_partial_reductions_get_fresh_transports_without_reusing_a_consumed_post(tmp_path):
    from app.live_execution.grvt_filled_close import request_filled_strategy_close
    from app.live_execution.grvt_testnet_strategy_close import run_strategy_close
    venue,client,public,clock,state=filled_case(tmp_path)
    parent=venue.source['client_order_id']; venue.close_fill_size='0.15'
    request_filled_strategy_close(venue.vault,client,parent,max_attempts=3,
        deadline_ns=clock[0]+240_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    transports=[]
    def fresh():
        transport=venue.factory('fake-testnet-key-'+venue.cap.sub_account_id)
        transports.append(transport); return transport
    def wait(seconds):
        clock[0]+=6_000_000_000
        if venue.close_sends==2: venue.close_fill_size=None
        return False
    result=run_strategy_close(venue.vault,client,parent,private_key=KEY.hex(),expected_signer=SIGNER,
        public_post=public,clock_ns=lambda:clock[0],wait=wait,submission_client_factory=fresh)
    assert result['status']=='COMPLETE' and venue.position==0
    assert venue.cancel_sends==1 and venue.close_sends==3 and len(transports)==2
    assert len({row['client_order_id'] for row in venue.fills[1:]})==3
    venue.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_hold_does_not_persist_stop_and_retries_cannot_extend_a_recorded_close(tmp_path,direction):
    from app.live_execution.grvt_filled_close import request_filled_strategy_close
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.portfolio.models import GrvtTestnetStop
    venue,client,public,clock,state=filled_case(tmp_path,direction)
    parent=venue.source['client_order_id']; state['mark']='100'
    with pytest.raises(GrvtTestnetBracketError):
        request_filled_strategy_close(venue.vault,client,parent,max_attempts=1,
            deadline_ns=clock[0]+120_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    assert read_strategy_close(venue.vault,parent) is None
    with venue.vault.session() as db:
        stop=db.get(GrvtTestnetStop,venue.cap.sub_account_id)
        assert stop is None or not stop.stopped
    state['mark']='108.1' if direction=='LONG' else '91.9'
    original=request_filled_strategy_close(venue.vault,client,parent,max_attempts=1,
        deadline_ns=clock[0]+120_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    clock[0]+=10_000_000_000
    repeated=request_filled_strategy_close(venue.vault,client,parent,max_attempts=3,
        deadline_ns=clock[0]+300_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    assert repeated==original and venue.cancel_sends==venue.close_sends==0
    venue.vault.lock()


@pytest.mark.parametrize('fault',['decision','market_source','stale','native_change'])
def test_changed_or_old_prepared_evidence_cannot_persist_a_close(tmp_path,fault):
    from app.live_execution.grvt_bracket_exit_review import collect_bracket_exit_evidence
    from app.live_execution.grvt_filled_close import request_filled_strategy_close
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    venue,client,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    _,evidence=collect_bracket_exit_evidence(venue.vault,client,parent,public_post=public,clock_ns=lambda:clock[0])
    if fault=='decision': evidence['decision']['entry_price']='1'
    if fault=='market_source': evidence['market']['history']['source']='GRVT_MAINNET'
    if fault=='stale': clock[0]+=6_000_000_000
    if fault=='native_change':
        venue.trigger(); observe_bracket_orders(venue.vault,client,parent,clock_ns=lambda:clock[0])
    with pytest.raises(GrvtTestnetBracketError):
        request_filled_strategy_close(venue.vault,client,parent,max_attempts=1,
            deadline_ns=clock[0]+120_000_000_000,public_post=public,clock_ns=lambda:clock[0],prepared_evidence=evidence)
    assert read_strategy_close(venue.vault,parent) is None
    assert venue.cancel_sends==venue.close_sends==0
    venue.vault.lock()


def test_v43_migration_preserves_the_native_entry_and_archives_the_v2_format_under_current_schema(tmp_path):
    from app.portfolio.vault import SCHEMA_VERSION
    from app.live_execution.grvt_filled_close import request_filled_strategy_close
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    venue,client,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    original=read_bracket(venue.vault,parent)
    with venue.vault.session() as db:
        db.connection().exec_driver_sql('PRAGMA user_version=43'); db.commit()
    venue.vault.lock(); venue.vault.unlock(PASSWORD)
    assert venue.vault.last_migration_backup is not None and read_bracket(venue.vault,parent)==original
    with venue.vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    request_filled_strategy_close(venue.vault,client,parent,max_attempts=1,
        deadline_ns=clock[0]+120_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    venue.vault.backup(tmp_path/'migrated-v44.db'); venue.vault.lock()


def test_backup_replays_the_financial_decision_even_if_the_outer_hash_was_recomputed(tmp_path):
    import json
    from app.live_execution.grvt_filled_close import request_filled_strategy_close,filled_close_id
    from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_oco import _encode
    from app.portfolio.models import GrvtTestnetStrategyClose
    from app.portfolio.vault import VaultError
    venue,client,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    request_filled_strategy_close(venue.vault,client,parent,max_attempts=1,
        deadline_ns=clock[0]+120_000_000_000,public_post=public,clock_ns=lambda:clock[0])
    with venue.vault.session() as db:
        row=db.get(GrvtTestnetStrategyClose,parent); changed=json.loads(row.payload_json)
        changed['evidence']['decision']['stop_price']='1'
        row.payload_json,row.payload_digest=_encode(changed)
        row.close_client_order_id=filled_close_id(parent,changed['evidence']); db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_strategy_close(venue.vault,parent)
    with pytest.raises(VaultError): venue.vault.backup(tmp_path/'invalid-decision.db')
    venue.vault.lock()
