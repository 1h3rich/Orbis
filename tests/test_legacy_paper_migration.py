from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import text

from app.database import legacy_migration
from app.database.legacy_backup import verify_legacy_encrypted_backup
from app.database.legacy_lease import acquire_legacy_runtime_lease
from app.database.legacy_migration import (check_legacy_migration_readiness,
                                           encrypt_active_legacy_database)
from app.database.legacy_runtime import (
    LegacyDatabaseError, create_encrypted_legacy_engine, legacy_database_format)


PASSWORD = "clave-segura-migracion"


def _plain(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY, note TEXT)")
        db.execute("INSERT INTO operations (note) VALUES ('PAPER anterior')")
        db.execute("PRAGMA user_version = 4")
        db.commit()


def test_migration_replaces_active_with_verified_cipher_and_keeps_encrypted_backup(tmp_path):
    path = tmp_path / "orbis.db"
    _plain(path)
    result = encrypt_active_legacy_database(path, "CIFRAR PAPER", PASSWORD)

    assert result["encrypted"] is True
    assert legacy_database_format(path) == "encrypted_or_invalid"
    assert path.stat().st_mode & 0o777 == 0o600
    assert verify_legacy_encrypted_backup(path, PASSWORD) == result["verified"]
    assert verify_legacy_encrypted_backup(Path(result["backup"]), PASSWORD) == result["verified"]
    assert not list(tmp_path.glob(".orbis.db.encrypt-*"))
    engine = create_encrypted_legacy_engine(f"sqlite:///{path}", path, PASSWORD)
    try:
        with engine.connect() as db:
            assert db.execute(text("SELECT note FROM operations")).scalar_one() == "PAPER anterior"
    finally:
        engine.dispose()


def test_migration_requires_confirmation_and_preserves_plain_source(tmp_path):
    path = tmp_path / "orbis.db"
    _plain(path)
    original = path.read_bytes()
    with pytest.raises(LegacyDatabaseError, match="CIFRAR PAPER"):
        encrypt_active_legacy_database(path, "NO", PASSWORD)
    assert path.read_bytes() == original
    assert not (tmp_path / "legacy-backups").exists()


def test_migration_preflight_checks_without_password_or_source_changes(tmp_path):
    path = tmp_path / "orbis.db"
    _plain(path)
    original = path.read_bytes()
    check_legacy_migration_readiness(path)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.encrypt_legacy_paper",
         "--source", str(path), "--check"],
        input="", capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert "listo" in result.stdout
    assert "contraseña" not in result.stdout.lower() + result.stderr.lower()
    assert path.read_bytes() == original
    assert not (tmp_path / "legacy-backups").exists()


def test_migration_cli_rejects_open_server_before_asking_for_password(tmp_path):
    path = tmp_path / "orbis.db"
    _plain(path)
    original = path.read_bytes()
    descriptor = acquire_legacy_runtime_lease(path)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.encrypt_legacy_paper",
             "--source", str(path)],
            input="", capture_output=True, text=True, timeout=10, check=False)
    finally:
        os.close(descriptor)
    assert result.returncode != 0
    assert "Cierra ORBIS" in result.stderr
    assert "contraseña" not in result.stdout.lower() + result.stderr.lower()
    assert path.read_bytes() == original
    assert not (tmp_path / "legacy-backups").exists()


def test_runtime_lease_prevents_migration(tmp_path):
    path = tmp_path / "orbis.db"
    _plain(path)
    descriptor = acquire_legacy_runtime_lease(path)
    try:
        with pytest.raises(LegacyDatabaseError, match="Cierra ORBIS"):
            encrypt_active_legacy_database(path, "CIFRAR PAPER", PASSWORD)
    finally:
        os.close(descriptor)
    assert legacy_database_format(path) == "plain"


def test_migration_rejects_pending_journal_without_replacing_source(tmp_path):
    path = tmp_path / "orbis.db"
    _plain(path)
    Path(str(path) + "-wal").write_bytes(b"pending")
    with pytest.raises(LegacyDatabaseError, match="journal"):
        encrypt_active_legacy_database(path, "CIFRAR PAPER", PASSWORD)
    assert legacy_database_format(path) == "plain"


def test_migration_rejects_source_changed_after_backup(tmp_path, monkeypatch):
    path = tmp_path / "orbis.db"
    _plain(path)
    original_backup = legacy_migration.create_legacy_encrypted_backup

    def backup_then_external_write(source, passphrase):
        result = original_backup(source, passphrase)
        with closing(sqlite3.connect(source)) as db:
            db.execute("UPDATE operations SET note='cambio externo' WHERE id=1")
            db.commit()
        return result

    monkeypatch.setattr(legacy_migration, "create_legacy_encrypted_backup",
                        backup_then_external_write)
    with pytest.raises(LegacyDatabaseError, match="cambió durante la migración"):
        encrypt_active_legacy_database(path, "CIFRAR PAPER", PASSWORD)
    assert legacy_database_format(path) == "plain"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT note FROM operations").fetchone()[0] == "cambio externo"


def test_migration_rejects_same_size_changed_content_with_restored_mtime(tmp_path, monkeypatch):
    path = tmp_path / "orbis.db"
    _plain(path)
    original_backup = legacy_migration.create_legacy_encrypted_backup
    before = legacy_migration._identity(path)

    def backup_then_disguised_write(source, passphrase):
        result = original_backup(source, passphrase)
        info = source.stat()
        with closing(sqlite3.connect(source)) as db:
            db.execute("UPDATE operations SET note='PAPER alterado' WHERE id=1")
            db.commit()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert legacy_migration._identity(source) == before
        return result

    monkeypatch.setattr(legacy_migration, "create_legacy_encrypted_backup",
                        backup_then_disguised_write)
    with pytest.raises(LegacyDatabaseError, match="cambió durante la migración"):
        encrypt_active_legacy_database(path, "CIFRAR PAPER", PASSWORD)
    assert legacy_database_format(path) == "plain"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT note FROM operations").fetchone()[0] == "PAPER alterado"


def test_offline_cli_migrates_only_explicit_temporary_path(tmp_path):
    path = tmp_path / "orbis.db"
    _plain(path)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.encrypt_legacy_paper", "--source", str(path)],
        input=f"CIFRAR PAPER\n{PASSWORD}\n{PASSWORD}\n",
        capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert PASSWORD not in result.stdout + result.stderr
    assert legacy_database_format(path) == "encrypted_or_invalid"
