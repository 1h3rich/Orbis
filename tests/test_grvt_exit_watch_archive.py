"""Diario de seguimiento cifrado: conservación, interrupciones y copias."""

from tests.test_grvt_filled_strategy_close import filled_case
from tests.test_grvt_testnet_bracket_submit import PASSWORD
import pytest


def test_watch_observations_survive_reopening_without_restoring_permission(tmp_path):
    from app.live_execution.grvt_exit_watch_archive import begin_exit_watch,append_exit_watch,exit_watch_history
    venue,reader,public,clock,state=filled_case(tmp_path)
    parent=venue.source['client_order_id']; started=clock[0]
    run=begin_exit_watch(venue.vault,parent,max_attempts=2,deadline_ns=started+120_000_000_000,clock_ns=lambda:clock[0])
    clock[0]+=1_000_000_000
    append_exit_watch(venue.vault,run,'OBSERVATION',{'event_time_ns':clock[0],
        'mark_price':'100','action':'HOLD','reason':'POSITION_OPEN'},clock_ns=lambda:clock[0])
    venue.vault.lock(); venue.vault.unlock(PASSWORD)
    history=exit_watch_history(venue.vault,parent)
    saved=history['items'][0]
    assert saved['id']==run and saved['status']=='INTERRUPTED' and saved['authorization'] is False
    assert saved['started_at_ns']==started and saved['deadline_ns']==started+120_000_000_000
    assert saved['observations']==1 and saved['last_market_event_ns']==clock[0]
    assert saved['unobserved_since_ns']==clock[0] and saved['event_count']==2
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    def forbidden(*args): raise AssertionError('Un historial no reconecta el proveedor')
    service=GrvtStrategyCloseService(venue.vault,client_factory=forbidden,public_post=forbidden)
    recovered=service.snapshot(parent)
    assert recovered['run']['status']=='INTERRUPTED' and recovered['run']['active'] is False
    assert recovered['run']['permission_remaining_seconds']==0
    venue.vault.backup(tmp_path/'watch-backup.db')
    assert b'SQLite format 3' not in (tmp_path/'watch-backup.db').read_bytes()[:16]
    venue.vault.lock()


def test_backup_rejects_a_deleted_middle_event(tmp_path):
    from app.live_execution.grvt_exit_watch_archive import begin_exit_watch,append_exit_watch
    from app.portfolio.models import GrvtTestnetExitWatchEvent
    from app.portfolio.vault import VaultError
    venue,reader,public,clock,state=filled_case(tmp_path)
    run=begin_exit_watch(venue.vault,venue.source['client_order_id'],max_attempts=2,
        deadline_ns=clock[0]+120_000_000_000,clock_ns=lambda:clock[0])
    for _ in range(2):
        clock[0]+=1_000_000_000
        append_exit_watch(venue.vault,run,'OBSERVATION',{'event_time_ns':clock[0],
            'mark_price':'100','action':'HOLD','reason':'POSITION_OPEN'},clock_ns=lambda:clock[0])
    with venue.vault.session() as db:
        db.delete(db.get(GrvtTestnetExitWatchEvent,(run,2))); db.commit()
    with pytest.raises(VaultError): venue.vault.backup(tmp_path/'truncated.db')
    venue.vault.lock()


def test_v44_migration_preserves_entries_and_adds_empty_watch_history(tmp_path):
    from app.live_execution.grvt_exit_watch_archive import exit_watch_history
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.portfolio.vault import SCHEMA_VERSION
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    before=read_bracket(venue.vault,parent)
    with venue.vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_exit_watch_events')
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_exit_watches')
        db.connection().exec_driver_sql('PRAGMA user_version=44'); db.commit()
    venue.vault.lock(); venue.vault.unlock(PASSWORD)
    assert venue.vault.last_migration_backup is not None
    assert read_bracket(venue.vault,parent)==before and exit_watch_history(venue.vault,parent)['items']==[]
    with venue.vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    venue.vault.backup(tmp_path/'v45.db'); venue.vault.lock()


def test_events_are_paginated_and_terminal_records_cannot_accept_more_ticks(tmp_path):
    from app.live_execution.grvt_exit_watch_archive import begin_exit_watch,append_exit_watch,read_exit_watch
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue,reader,public,clock,state=filled_case(tmp_path); parent=venue.source['client_order_id']
    run=begin_exit_watch(venue.vault,parent,max_attempts=2,deadline_ns=clock[0]+120_000_000_000,clock_ns=lambda:clock[0])
    for _ in range(101):
        clock[0]+=500_000_000
        append_exit_watch(venue.vault,run,'OBSERVATION',{'event_time_ns':clock[0],
            'mark_price':'100.000000000000000001','action':'HOLD','reason':'POSITION_OPEN'},clock_ns=lambda:clock[0])
    append_exit_watch(venue.vault,run,'TERMINAL',{'status':'STOPPED','cause':'USER_STOP'},clock_ns=lambda:clock[0])
    first=read_exit_watch(venue.vault,parent,run); second=read_exit_watch(venue.vault,parent,run,offset=100)
    assert len(first['events'])==100 and first['has_more'] and not second['has_more']
    assert [item['sequence'] for item in second['events']]==[101,102,103]
    assert first['events'][1]['data']['mark_price']=='100.000000000000000001'
    with pytest.raises(GrvtTestnetBracketError):
        append_exit_watch(venue.vault,run,'OBSERVATION',{'event_time_ns':clock[0]+1,
            'mark_price':'100','action':'HOLD','reason':'POSITION_OPEN'},clock_ns=lambda:clock[0]+1)
    assert read_exit_watch(venue.vault,parent,run)['event_count']==103
    venue.vault.lock()


def test_an_archive_cannot_claim_complete_while_the_position_is_unreconciled(tmp_path):
    from app.live_execution.grvt_exit_watch_archive import begin_exit_watch,append_exit_watch
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue,reader,public,clock,state=filled_case(tmp_path)
    run=begin_exit_watch(venue.vault,venue.source['client_order_id'],max_attempts=2,
        deadline_ns=clock[0]+120_000_000_000,clock_ns=lambda:clock[0])
    with pytest.raises(GrvtTestnetBracketError):
        append_exit_watch(venue.vault,run,'TERMINAL',{'status':'COMPLETE','cause':'NONE'},clock_ns=lambda:clock[0])
    venue.vault.lock()
