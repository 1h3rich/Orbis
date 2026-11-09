from datetime import datetime, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest
from app.portfolio.domain import CALC_PRECISION, EventInput, PortfolioError
from app.portfolio.repository import (
    account_snapshot, append_event, create_account, list_events,
    transfer_asset_between_accounts,
    transfer_cash_between_accounts,
)
from app.portfolio.vault import RealVault, VaultError


PASSPHRASE = "local test passphrase only"


def test_vault_is_encrypted_persistent_and_locks(tmp_path):
    path = tmp_path / "real-vault.db"
    vault = RealVault(path)
    with pytest.raises(VaultError, match="locked"):
        with vault.session():
            pass
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA journal_mode").scalar_one() == "delete"
        account = create_account(db, "Personal")
        append_event(
            db, account.id, str(uuid4()),
            EventInput("BUY", "EUR", datetime.now(timezone.utc), "BTC", Decimal("0.1"), Decimal("100")),
        )
        account_id = account.id
    vault.lock()
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert b"Personal" not in path.read_bytes()
    with pytest.raises(VaultError, match="locked"):
        with vault.session():
            pass
    with pytest.raises(VaultError, match="Cannot unlock"):
        vault.unlock("wrong passphrase 123")
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        snapshot = account_snapshot(db, account_id)
        assert snapshot.positions[("BTC", "EUR")].quantity == Decimal("0.1")
    vault.lock()


def test_encrypted_vault_reopens_scientific_decimal_inputs_as_exact_fixed_values(tmp_path):
    vault = RealVault(tmp_path / "real-vault.db")
    vault.unlock(PASSPHRASE, create=True)
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with vault.session() as db:
        account_id = create_account(db, "Extremos").id
        append_event(db, account_id, str(uuid4()), EventInput(
            "CONTRIBUTION", "EUR", when, amount=Decimal("9e40")))
        append_event(db, account_id, str(uuid4()), EventInput(
            "BUY", "EUR", when, "BTC", Decimal("1e-40"), Decimal("9e40"),
            fee=Decimal("1e-80"), fee_asset="BTC"))
    vault.lock()
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        snapshot = account_snapshot(db, account_id)
        with localcontext() as context:
            context.prec = CALC_PRECISION
            assert snapshot.cash["EUR"] == Decimal("9e40") - Decimal("9")
            assert snapshot.positions[("BTC", "EUR")].quantity == Decimal("1e-40") - Decimal("1e-80")
        assert [event.input.kind for event in list_events(db, account_id)] == ["CONTRIBUTION", "BUY"]
    vault.lock()


def test_managed_backup_can_be_checked_without_restoring_or_mutating_source(tmp_path):
    vault = RealVault(tmp_path / "real-vault.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Principal").id
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    saved = vault.backup(backup_dir / "real-check.db")
    original_bytes = saved.read_bytes()
    with vault.session() as db:
        append_event(db, account_id, str(uuid4()), EventInput(
            "CONTRIBUTION", "EUR", datetime.now(timezone.utc), amount=Decimal("25")))
    checked = vault.verify_managed_backup(saved)
    assert checked["verified"] is True
    assert checked["name"] == saved.name
    assert (checked["accounts"], checked["events"]) == (1, 0)
    assert saved.read_bytes() == original_bytes
    assert not list(backup_dir.glob(".verify-*"))
    with vault.session() as db:
        assert len(list_events(db, account_id)) == 1

    damaged = backup_dir / "real-damaged.db"
    damaged.write_bytes(b"not a SQLCipher backup")
    with pytest.raises(VaultError):
        vault.verify_managed_backup(damaged)
    assert damaged.read_bytes() == b"not a SQLCipher backup"
    assert not list(backup_dir.glob(".verify-*"))
    link = backup_dir / "real-link.db"
    link.symlink_to(saved)
    with pytest.raises(VaultError, match="regular file"):
        vault.verify_managed_backup(link)
    external = vault.backup(tmp_path / "external.db")
    with pytest.raises(VaultError, match="managed backups"):
        vault.verify_managed_backup(external)
    vault.lock()
    with pytest.raises(VaultError, match="locked"):
        vault.verify_managed_backup(saved)


def test_encrypted_backup_restores_both_sides_of_paired_transfer(tmp_path):
    path = tmp_path / "real-vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with vault.session() as db:
        source = create_account(db, "Wallet", "EUR").id
        target = create_account(db, "MEXC", "EUR").id
        append_event(db, source, str(uuid4()), EventInput(
            "BUY", "EUR", when, "BTC", Decimal("2"), Decimal("100")))
        transfer_asset_between_accounts(
            db, source, target, str(uuid4()), asset="BTC", quote_currency="EUR",
            quantity=Decimal("0.5"), occurred_at=when,
        )
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    saved = vault.backup(backup_dir / "paired.db")
    assert saved.read_bytes()[:16] != b"SQLite format 3\x00"
    with vault.session() as db:
        append_event(db, source, str(uuid4()), EventInput(
            "SELL", "EUR", when, "BTC", Decimal("0.5"), Decimal("150")))
    vault.restore_backup(saved)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        left = account_snapshot(db, source).positions[("BTC", "EUR")]
        right = account_snapshot(db, target).positions[("BTC", "EUR")]
        assert (left.quantity, left.cost_basis, right.quantity, right.cost_basis) == (
            Decimal("1.5"), Decimal("150"), Decimal("0.5"), Decimal("50"))
        assert left.realized_pnl == 0
        assert [event.input.kind for event in list_events(db, target)] == ["TRANSFER_IN"]
    damaged = vault.backup(backup_dir / "damaged.db")
    separate = RealVault(damaged)
    separate.unlock(PASSPHRASE)
    with separate.session() as db:
        db.connection().exec_driver_sql(
            "DELETE FROM real_events WHERE account_id=? AND kind='TRANSFER_IN'", (target,))
        db.commit()
    separate.lock()
    with pytest.raises(PortfolioError, match="incomplete"):
        vault.restore_backup(damaged)
    with vault.session() as db:
        assert account_snapshot(db, target).positions[("BTC", "EUR")].quantity == Decimal("0.5")
    vault.lock()


def test_encrypted_backup_rejects_missing_cash_transfer_side(tmp_path):
    vault = RealVault(tmp_path / "real-vault.db")
    vault.unlock(PASSPHRASE, create=True)
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with vault.session() as db:
        source = create_account(db, "Wallet").id
        target = create_account(db, "MEXC").id
        append_event(db, source, str(uuid4()), EventInput(
            "CONTRIBUTION", "USDT", when, amount=Decimal("100")))
        transfer_cash_between_accounts(db, source, target, str(uuid4()),
                                       currency="USDT", amount=Decimal("25"), occurred_at=when)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    saved = vault.backup(backup_dir / "cash-pair.db")
    vault.restore_backup(saved)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert account_snapshot(db, source).cash["USDT"] == Decimal("75")
        assert account_snapshot(db, target).cash["USDT"] == Decimal("25")
    damaged = vault.backup(backup_dir / "cash-damaged.db")
    separate = RealVault(damaged)
    separate.unlock(PASSPHRASE)
    with separate.session() as db:
        db.connection().exec_driver_sql(
            "DELETE FROM real_events WHERE account_id=? AND kind='CASH_TRANSFER_IN'", (target,))
        db.commit()
    separate.lock()
    with pytest.raises(PortfolioError, match="incomplete"):
        vault.restore_backup(damaged)
    vault.lock()


def test_vault_never_overwrites_existing_data(tmp_path):
    path = tmp_path / "real-vault.db"
    path.write_bytes(b"SQLite format 3\x00" + b"fake")
    vault = RealVault(path)
    with pytest.raises(VaultError, match="already exists"):
        vault.unlock(PASSPHRASE, create=True)
    with pytest.raises(VaultError, match="unencrypted"):
        vault.unlock(PASSPHRASE)


def test_v17_adds_fee_asset_on_encrypted_copy_and_keeps_legacy_trade(tmp_path):
    path = tmp_path / "real-vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Preimagen v17").id
        append_event(db, account_id, str(uuid4()), EventInput(
            "BUY", "EUR", datetime.now(timezone.utc), "BTC", Decimal("0.01"),
            Decimal("65000"), fee=Decimal("0.10"),
            gross_override=Decimal("650.00000001"),
        ))
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE real_events DROP COLUMN fee_asset")
        connection.exec_driver_sql("PRAGMA user_version = 17")
    vault.lock()
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert account_snapshot(db, account_id).positions[("BTC", "EUR")].cost_basis == Decimal("650.10000001")
        assert list_events(db, account_id)[0].input.effective_fee_asset == "EUR"
    backup = vault.last_migration_backup
    assert backup is not None and backup.is_file()
    assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert RealVault._verify_portfolio_file(backup, PASSPHRASE, expected_version=17) == (1, 1)
    vault.lock()


def test_vault_rejects_short_password_and_unknown_schema(tmp_path):
    path = tmp_path / "real-vault.db"
    vault = RealVault(path)
    with pytest.raises(VaultError, match="at least 12"):
        vault.unlock("short", create=True)
    assert not path.exists()
    # An unknown encrypted file remains untouched. A wrong key has the same
    # safe result: no schema reset or data migration is attempted.
    path.write_bytes(b"not a database")
    original = path.read_bytes()
    with pytest.raises(VaultError, match="Cannot unlock"):
        vault.unlock(PASSPHRASE)
    assert path.read_bytes() == original


def _downgrade_fixture_to_v1(vault: RealVault) -> None:
    """Sólo en una bóveda de prueba: reproduce el esquema anterior."""
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE real_live_order_observations")
        connection.exec_driver_sql("DROP TABLE mexc_transfer_snapshots")
        connection.exec_driver_sql("DROP TABLE mexc_observation_batches")
        connection.exec_driver_sql("DROP TABLE mexc_observed_fills")
        connection.exec_driver_sql("PRAGMA user_version = 1")
    vault.lock()


def test_v1_migrates_on_encrypted_copy_and_keeps_preimage(tmp_path):
    path = tmp_path / "real-vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Antes de migrar").id
        append_event(db, account_id, str(uuid4()),
                     EventInput("BUY", "EUR", datetime.now(timezone.utc), "BTC",
                                Decimal("0.123456789012345678"), Decimal("65000.01")))
    _downgrade_fixture_to_v1(vault)

    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert account_snapshot(db, account_id).positions[("BTC", "EUR")].quantity == Decimal("0.123456789012345678")
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observed_fills").scalar_one() == 0
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v1-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"
    assert b"Antes de migrar" not in backups[0].read_bytes()
    assert RealVault._verify_portfolio_file(backups[0], PASSPHRASE, expected_version=1) == (1, 1)


def test_failed_v1_replace_preserves_original_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "real-vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        create_account(db, "Conservar")
    _downgrade_fixture_to_v1(vault)

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("app.portfolio.vault.os.replace", fail_replace)
    with pytest.raises(VaultError, match="original file was preserved"):
        vault.unlock(PASSPHRASE)
    assert not vault.unlocked
    assert RealVault._verify_portfolio_file(path, PASSPHRASE, expected_version=1) == (1, 0)
    assert len(list((tmp_path / "backups").glob("pre-migrate-v1-*.db"))) == 1


def test_v8_migrates_grvt_evidence_table_without_changing_portfolio(tmp_path):
    path = tmp_path / "real-vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Conservar v8").id
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE grvt_order_evidence")
        connection.exec_driver_sql("PRAGMA user_version = 8")
    vault.lock()

    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql("SELECT count(*) FROM grvt_order_evidence").scalar_one() == 0
        assert account_snapshot(db, account_id).account_id == account_id
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v8-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"
    assert RealVault._verify_portfolio_file(backups[0], PASSPHRASE, expected_version=8) == (1, 0)


def test_encrypted_backup_can_be_reopened_and_never_overwrites(tmp_path):
    vault = RealVault(tmp_path / "live.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account = create_account(db, "Private account")
        account_id = account.id
    copy = tmp_path / "backup.db"
    assert vault.backup(copy) == copy
    assert copy.read_bytes()[:16] != b"SQLite format 3\x00"
    assert b"Private account" not in copy.read_bytes()
    with pytest.raises(VaultError, match="already exists"):
        vault.backup(copy)
    vault.lock()
    restored = RealVault(copy)
    restored.unlock(PASSPHRASE)
    with restored.session() as db:
        assert account_snapshot(db, account_id).account_id == account_id
    restored.lock()


def test_restore_replaces_only_real_vault_and_preserves_previous_version(tmp_path):
    vault = RealVault(tmp_path / "live.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Original").id
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = vault.backup(backup_dir / "before-change.db")
    with vault.session() as db:
        append_event(
            db, account_id, str(uuid4()),
            EventInput("CONTRIBUTION", "EUR", datetime.now(timezone.utc), amount=Decimal("100")),
        )
        assert len(list_events(db, account_id)) == 1

    recovery = vault.restore_backup(backup)
    assert not vault.unlocked
    assert recovery.is_file()
    assert recovery.read_bytes()[:16] != b"SQLite format 3\x00"
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert len(list_events(db, account_id)) == 0
    vault.lock()
    old = RealVault(recovery)
    old.unlock(PASSPHRASE)
    with old.session() as db:
        assert len(list_events(db, account_id)) == 1
    old.lock()


def test_invalid_or_external_backup_never_replaces_live_vault(tmp_path):
    vault = RealVault(tmp_path / "live.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Keep me").id
    directory = tmp_path / "backups"
    directory.mkdir()
    corrupt = directory / "corrupt.db"
    corrupt.write_bytes(b"not an encrypted backup")
    with pytest.raises(VaultError):
        vault.restore_backup(corrupt)
    external = tmp_path / "external.db"
    vault.backup(external)
    with pytest.raises(VaultError, match="managed backups"):
        vault.restore_backup(external)
    with vault.session() as db:
        assert account_snapshot(db, account_id).account_id == account_id
    assert vault.unlocked
    vault.lock()


def test_replace_failure_keeps_current_vault_and_recovery_copy(tmp_path, monkeypatch):
    vault = RealVault(tmp_path / "live.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        account_id = create_account(db, "Still here").id
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = vault.backup(backup_dir / "before.db")
    with vault.session() as db:
        append_event(
            db, account_id, str(uuid4()),
            EventInput("CONTRIBUTION", "EUR", datetime.now(timezone.utc), amount=Decimal("5")),
        )

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("app.portfolio.vault.os.replace", fail_replace)
    with pytest.raises(VaultError, match="current vault was preserved"):
        vault.restore_backup(backup)
    assert not vault.unlocked
    assert list(backup_dir.glob("pre-restore-*.db"))
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert len(list_events(db, account_id)) == 1
    vault.lock()
