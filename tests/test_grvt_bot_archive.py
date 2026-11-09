"""Un permiso previo no se convierte en autoridad después de reiniciar."""

from copy import deepcopy
from decimal import Decimal
import time

import pytest

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from tests.test_grvt_testnet_bracket_submit import live_group, PASSWORD


def setup_run(tmp_path):
    from app.live_execution.grvt_bot_source import bot_source
    from app.live_execution.grvt_bot_archive import begin_bot_run
    venue, origin, vault, *rest = live_group(tmp_path)
    source = bot_source(venue.paper, venue.lab, origin['session_id'])
    at = time.time_ns()
    run = begin_bot_run(vault, source, sub_account_id=origin['sub_account_id'],
        entry_cap=Decimal('50'), exit_cap=Decimal('100'), wait_seconds=900,
        exit_seconds=120, max_attempts=2, max_spread_bps=25, max_slippage_bps=25, now_ns=at)
    return venue, origin, vault, run, at


def test_one_reserved_episode_per_subaccount_and_reopening_grants_nothing(tmp_path):
    from app.live_execution.grvt_bot_archive import begin_bot_run, read_bot_run
    venue, origin, vault, run, at = setup_run(tmp_path)
    saved = read_bot_run(vault, run)
    assert saved['status'] == 'INTERRUPTED' and saved['authorization'] is False
    assert saved['max_entries'] == 1 and saved['selection'] is None
    with pytest.raises(GrvtTestnetBracketError):
        begin_bot_run(vault, saved['source'], sub_account_id=origin['sub_account_id'],
            entry_cap=Decimal('50'), exit_cap=Decimal('100'), wait_seconds=900,
            exit_seconds=120, max_attempts=2, max_spread_bps=25, max_slippage_bps=25, now_ns=at+1)
    vault.lock(); vault.unlock(PASSWORD)
    assert read_bot_run(vault, run) == saved
    vault.backup(tmp_path/'bot-backup.db'); vault.lock()


def test_selection_is_once_and_cannot_copy_an_entry_from_before_activation(tmp_path):
    from app.live_execution.grvt_bot_archive import select_bot_open, read_bot_run
    venue, origin, vault, run, at = setup_run(tmp_path)
    candidate = {'action_request_id': origin['action_request_id'],
        'parent_client_order_id': origin['client_order_id'], 'recorded_at_ns': at-1}
    with pytest.raises(GrvtTestnetBracketError): select_bot_open(vault, run, candidate, now_ns=at+1)
    candidate['recorded_at_ns'] = at+1
    select_bot_open(vault, run, candidate, now_ns=at+2)
    assert read_bot_run(vault, run)['selection']['parent_client_order_id'] == origin['client_order_id']
    with pytest.raises(GrvtTestnetBracketError): select_bot_open(vault, run, candidate, now_ns=at+3)
    vault.lock()


@pytest.mark.parametrize('change', ['header', 'selection', 'slot', 'terminal'])
def test_backup_rejects_changed_bot_record(change, tmp_path):
    from app.live_execution.grvt_bot_archive import select_bot_open, finish_bot_run
    from app.portfolio.models import GrvtTestnetBotRun
    from app.portfolio.vault import VaultError
    venue, origin, vault, run, at = setup_run(tmp_path)
    select_bot_open(vault, run, {'action_request_id': origin['action_request_id'],
        'parent_client_order_id': origin['client_order_id'], 'recorded_at_ns': at+1}, now_ns=at+2)
    finish_bot_run(vault, run, status='STOPPED', cause='USER_STOP', now_ns=at+3)
    with vault.session() as db:
        row = db.get(GrvtTestnetBotRun, run)
        if change == 'header': row.payload_json = row.payload_json.replace('"50"', '"99"')
        if change == 'selection': row.selection_digest = '0'*64
        if change == 'slot': row.active_slot = origin['sub_account_id']
        if change == 'terminal': row.terminal_json = row.terminal_json.replace('STOPPED', 'COMPLETE')
        db.commit()
    with pytest.raises(VaultError): vault.backup(tmp_path/'invalid.db')
    vault.lock()


def test_cannot_record_complete_before_a_financial_receipt(tmp_path):
    from app.live_execution.grvt_bot_archive import finish_bot_run, read_bot_run
    venue, origin, vault, run, at = setup_run(tmp_path)
    with pytest.raises(GrvtTestnetBracketError):
        finish_bot_run(vault, run, status='COMPLETE', cause='NONE', now_ns=at+1)
    assert read_bot_run(vault, run)['status'] == 'INTERRUPTED'
    finish_bot_run(vault, run, status='STOPPED', cause='USER_STOP', now_ns=at+2)
    assert read_bot_run(vault, run)['status'] == 'STOPPED'
    vault.lock()


def test_v45_migration_keeps_entries_and_adds_no_bot_authority(tmp_path):
    from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.portfolio.vault import SCHEMA_VERSION
    venue, origin, vault, orders, cap, instrument, client, public, recorded = live_group(tmp_path)
    _, before = submit_bracket_signal_once(venue.paper, venue.lab, vault, client, orders, cap, cap,
        instrument, session_id=origin['session_id'], action_request_id=origin['action_request_id'], public_post=public)
    with vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bot_runs')
        db.connection().exec_driver_sql('PRAGMA user_version=45'); db.commit()
    vault.lock(); vault.unlock(PASSWORD)
    assert vault.last_migration_backup is not None
    assert read_bracket(vault, origin['client_order_id']) == before
    with vault.session() as db:
        assert db.connection().exec_driver_sql('SELECT count(*) FROM grvt_testnet_bot_runs').scalar_one() == 0
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() == SCHEMA_VERSION
    vault.backup(tmp_path/'v46.db'); vault.lock()
