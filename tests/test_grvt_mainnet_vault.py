"""Migración y verificación de evidencia MAINNET en bóvedas cifradas."""

import pytest

from app.portfolio.vault import RealVault,VaultError,SCHEMA_VERSION
from tests.test_grvt_mainnet_submit import live_case,arguments,PASSWORD


def test_v49_migration_preserves_existing_testnet_group_and_creates_separate_mainnet_journal(tmp_path):
    from tests.test_grvt_filled_strategy_close import filled_case
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    venue,*_=filled_case(tmp_path)
    parent=venue.source['client_order_id']; before=read_bracket(venue.vault,parent)
    with venue.vault.session() as db:
        for table in ('grvt_mainnet_observations','grvt_mainnet_legs','grvt_mainnet_controls','grvt_mainnet_brackets'):
            db.connection().exec_driver_sql('DROP TABLE '+table)
        db.connection().exec_driver_sql('PRAGMA user_version=49'); db.commit()
    venue.vault.lock(); venue.vault.unlock(PASSWORD)
    assert venue.vault.last_migration_backup.is_file()
    assert read_bracket(venue.vault,parent) == before
    with venue.vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() ==SCHEMA_VERSION
        assert db.connection().exec_driver_sql('SELECT count(*) FROM grvt_mainnet_brackets').scalar_one() == 0
    backup=tmp_path/'migrated-v50.db'; venue.vault.backup(backup); venue.vault.lock()
    recovered=RealVault(backup); recovered.unlock(PASSWORD)
    assert read_bracket(recovered,parent) == before
    recovered.lock()


def test_backup_refuses_truncated_mainnet_observation_tail(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
    from app.portfolio.models import GrvtMainnetObservation
    case=live_case(tmp_path); parent=str(2**63+950)
    submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    observe_mainnet_bracket(case.vault,parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    with case.vault.session() as db:
        db.delete(db.get(GrvtMainnetObservation,(parent,1))); db.commit()
    with pytest.raises(VaultError): case.vault.backup(tmp_path/'truncated-mainnet.db')
    case.vault.lock()


def test_v50_migration_preserves_an_active_mainnet_group_without_inventing_a_receipt(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=live_case(tmp_path);parent=str(2**63+970)
    before=submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    with case.vault.session() as db:
        for table in ('grvt_mainnet_audits','grvt_mainnet_audit_heads','grvt_mainnet_reconciliations','grvt_mainnet_settlement_controls'):
            db.connection().exec_driver_sql('DROP TABLE '+table)
        db.connection().exec_driver_sql('PRAGMA user_version=50');db.commit()
    case.vault.lock();case.vault.unlock(PASSWORD)
    assert case.vault.last_migration_backup.is_file()
    assert read_mainnet_bracket(case.vault,parent)==before
    assert before['financially_reconciled'] is False and before['authorization'] is False
    case.vault.backup(tmp_path/'mainnet-v51-migrated.db');case.vault.lock()


def test_v51_migration_preserves_a_sealed_native_receipt_and_creates_no_recovery_authority(tmp_path):
    from tests.test_grvt_mainnet_finance import closed_case,reconcile_case
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=closed_case(tmp_path);reconcile_case(case);before=read_mainnet_bracket(case.vault,case.parent)
    with case.vault.session() as db:
        for table in ('grvt_mainnet_reductions','grvt_mainnet_recovery_events','grvt_mainnet_recoveries'):
            db.connection().exec_driver_sql('DROP TABLE '+table)
        db.connection().exec_driver_sql('PRAGMA user_version=51');db.commit()
    case.vault.lock();case.vault.unlock(PASSWORD)
    assert case.vault.last_migration_backup.is_file()
    assert read_mainnet_bracket(case.vault,case.parent)==before
    assert before['recovery'] is None and before['authorization'] is False
    case.vault.backup(tmp_path/'mainnet-v52-migrated.db');case.vault.lock()
