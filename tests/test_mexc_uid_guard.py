"""Identidad duradera: una clave rotada no reinicia los límites REAL."""

from datetime import datetime, timezone

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.mexc_uid_guard import MexcUidGuardError, bind_uid_for_spot_submission
from app.portfolio.repository import create_account
from app.portfolio.vault import RealVault


PASSWORD = "MEXC UID migration fixture"
FIRST = "a" * 64
ROTATED = "b" * 64


def test_uid_binding_survives_restart_and_rejects_key_rotation(tmp_path):
    path = tmp_path / "vault.db"
    vault = RealVault(path)
    vault.unlock(PASSWORD, create=True)
    bind_uid_for_spot_submission(vault, FIRST, "123456789")
    vault.lock()
    vault.unlock(PASSWORD)
    bind_uid_for_spot_submission(vault, FIRST, "123456789")
    with pytest.raises(MexcUidGuardError, match="Otra clave"):
        bind_uid_for_spot_submission(vault, ROTATED, "123456789")
    with pytest.raises(MexcUidGuardError, match="cambió de UID"):
        bind_uid_for_spot_submission(vault, FIRST, "987654321")
    with vault.session() as db:
        rows = db.connection().exec_driver_sql(
            "SELECT account_ref, uid_digest FROM real_mexc_uid_links"
        ).all()
        assert len(rows) == 1 and rows[0][0] == FIRST
        assert rows[0][1] != "123456789" and len(rows[0][1]) == 64
    assert b"123456789" not in path.read_bytes()
    vault.lock()


def test_uid_binding_rejects_bad_inputs_without_adding_rows(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSWORD, create=True)
    for reference, uid in (("bad", "123"), (FIRST, "0"), (FIRST, "not-a-uid")):
        with pytest.raises(MexcUidGuardError):
            bind_uid_for_spot_submission(vault, reference, uid)
    with pytest.raises(MexcUidGuardError, match="zona horaria"):
        bind_uid_for_spot_submission(vault, FIRST, "123", now=datetime(2026, 1, 1))
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_mexc_uid_links"
        ).scalar_one() == 0
    vault.lock()


def test_v15_migrates_uid_tables_on_encrypted_copy(tmp_path):
    path = tmp_path / "vault.db"
    vault = RealVault(path)
    vault.unlock(PASSWORD, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Conservar v15").id
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE real_mexc_uid_links")
        connection.exec_driver_sql("DROP TABLE real_mexc_identity_salt")
        connection.exec_driver_sql("PRAGMA user_version = 15")
    vault.lock()

    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_accounts WHERE id=?", (account_id,)
        ).scalar_one() == 1
    bind_uid_for_spot_submission(vault, FIRST, "123456789",
                                 now=datetime.now(timezone.utc))
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v15-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"
    assert RealVault._verify_portfolio_file(backups[0], PASSWORD, expected_version=15) == (1, 0)
