"""Una parada manual no se convierte en un rearme por cerrar después."""

from app.portfolio.vault import SCHEMA_VERSION

from datetime import datetime, timezone, timedelta
import pytest

from tests.test_grvt_filled_strategy_close import filled_case


def test_manual_stop_remains_latched_after_the_close_coordinator_requests_its_own_stop(tmp_path):
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.live_execution.grvt_stop_control import read_testnet_stop
    venue, client, public, clock, market = filled_case(tmp_path)
    sub = venue.cap.sub_account_id; parent = venue.source['client_order_id']
    now = datetime.now(timezone.utc)
    stop_testnet_subaccount(venue.vault, sub, now=now)
    stop_testnet_subaccount(venue.vault, sub, now=now+timedelta(seconds=1),
        reason='AUTO_EXIT', parent_client_order_id=parent)
    saved = read_testnet_stop(venue.vault, sub)
    assert saved['stopped'] is True and saved['reason'] == 'MANUAL'
    assert saved['parent_client_order_id'] is None and saved['revision'] == 2
    venue.vault.backup(tmp_path/'manual-latched.db'); venue.vault.lock()


def test_native_completion_records_the_automatic_stop_with_its_exact_parent(tmp_path):
    from tests.test_grvt_bot_service import bot_case, wait_for
    from app.live_execution.grvt_stop_control import read_testnet_stop
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path)
    run = activate()['id']; emit(); wait_for(opened.is_set); venue.trigger()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'COMPLETE'
    stop = read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    assert stop['reason'] == 'AUTO_EXIT' and stop['stopped']
    assert stop['parent_client_order_id'] == done['selection']['parent_client_order_id']
    venue.vault.backup(tmp_path/'native-stop.db'); venue.vault.lock()


def test_strategy_close_records_the_automatic_stop_for_the_exact_parent(tmp_path):
    from tests.test_grvt_bot_service import bot_case, wait_for
    from app.live_execution.grvt_stop_control import read_testnet_stop
    venue, origin, service, closes, activate, emit, trigger, opened, closed = bot_case(tmp_path)
    run = activate()['id']; emit(); wait_for(opened.is_set); trigger.set()
    done = wait_for(lambda: (item if not (item := service.snapshot(run))['active'] else None))
    assert done['status'] == 'COMPLETE'
    stop = read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    assert stop['reason'] == 'AUTO_EXIT' and stop['stopped']
    assert stop['parent_client_order_id'] == done['selection']['parent_client_order_id']
    venue.vault.backup(tmp_path/'strategy-stop.db'); venue.vault.lock()


def test_legacy_stop_survives_v46_migration_without_being_reclassified_as_automatic(tmp_path):
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.live_execution.grvt_stop_control import read_testnet_stop
    from app.portfolio.vault import SCHEMA_VERSION
    from tests.test_grvt_testnet_bracket_submit import PASSWORD
    venue, client, public, clock, market = filled_case(tmp_path)
    stop_testnet_subaccount(venue.vault,venue.cap.sub_account_id,now=datetime.now(timezone.utc))
    with venue.vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_stop_events')
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_stop_controls')
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bot_cycles')
        db.connection().exec_driver_sql('PRAGMA user_version=46'); db.commit()
    venue.vault.lock(); venue.vault.unlock(PASSWORD)
    saved = read_testnet_stop(venue.vault,venue.cap.sub_account_id)
    assert saved['stopped'] and saved['reason'] == 'LEGACY_STOP' and saved['revision'] == 0
    assert venue.vault.last_migration_backup is not None
    with venue.vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() == SCHEMA_VERSION
    venue.vault.backup(tmp_path/'legacy-stop.db'); venue.vault.lock()


def test_backup_rejects_a_truncated_stop_history(tmp_path):
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.portfolio.models import GrvtTestnetStopEvent
    from app.portfolio.vault import VaultError
    venue, client, public, clock, market = filled_case(tmp_path)
    for n in range(3):
        stop_testnet_subaccount(venue.vault,venue.cap.sub_account_id,now=datetime.now(timezone.utc)+timedelta(seconds=n))
    with venue.vault.session() as db:
        db.delete(db.get(GrvtTestnetStopEvent,(venue.cap.sub_account_id,2))); db.commit()
    with pytest.raises(VaultError): venue.vault.backup(tmp_path/'truncated-stop.db')
    venue.vault.lock()
