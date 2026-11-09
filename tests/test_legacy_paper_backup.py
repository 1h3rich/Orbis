from contextlib import closing
from pathlib import Path
import sqlite3

import pytest
import sqlcipher3

from app.database.legacy_backup import (
    LegacyBackupError, create_legacy_encrypted_backup, verify_legacy_encrypted_backup)
from app.database.legacy_runtime import create_new_encrypted_legacy_database


def _source(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY, note TEXT)")
        db.execute("INSERT INTO operations (note) VALUES (?)", ("prueba PAPER",))
        db.execute("PRAGMA user_version = 3")
        db.commit()


def test_backup_is_encrypted_verified_and_keeps_source(tmp_path):
    source = tmp_path / "orbis.db"
    _source(source)
    original = source.read_bytes()

    result = create_legacy_encrypted_backup(source, "clave-segura-para-prueba")

    assert result["encrypted"] is True
    assert result["tables"] == {"operations": 1}
    assert result["user_version"] == 3
    assert source.read_bytes() == original
    backup = Path(result["path"])
    assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert backup.stat().st_mode & 0o777 == 0o600
    with closing(sqlcipher3.connect(backup)) as db:
        db.execute("PRAGMA key = 'clave-segura-para-prueba'")
        assert db.execute("SELECT note FROM operations").fetchone()[0] == "prueba PAPER"
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not list(backup.parent.glob(".legacy-pending-*"))


def test_unlocked_encrypted_source_gets_independent_verified_backup(tmp_path):
    source = tmp_path / "orbis.db"
    active_key = "clave-paper-activa-prueba"
    backup_key = "clave-copia-independiente"
    create_new_encrypted_legacy_database(source, active_key)
    with closing(sqlcipher3.connect(source)) as db:
        db.execute("PRAGMA key = 'clave-paper-activa-prueba'")
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)")
        db.execute("INSERT INTO operations (note) VALUES ('saldo ficticio')")
        db.execute("PRAGMA user_version = 5")
        db.commit()
    original = source.read_bytes()

    with pytest.raises(LegacyBackupError, match="contraseña activa"):
        create_legacy_encrypted_backup(source, backup_key)
    with pytest.raises(LegacyBackupError, match="verificar"):
        create_legacy_encrypted_backup(source, backup_key,
                                       source_passphrase="contraseña-incorrecta")
    result = create_legacy_encrypted_backup(source, backup_key,
                                            source_passphrase=active_key)

    assert source.read_bytes() == original
    assert result["tables"] == {"operations": 1}
    assert result["user_version"] == 5
    backup = Path(result["path"])
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert verify_legacy_encrypted_backup(backup, backup_key)["fingerprint"] == result["fingerprint"]
    with pytest.raises(LegacyBackupError, match="verificar"):
        verify_legacy_encrypted_backup(backup, active_key)
    assert not list(backup.parent.glob(".legacy-pending-*"))


def test_backup_rejects_invalid_input_without_creating_output(tmp_path):
    source = tmp_path / "orbis.db"
    _source(source)
    with pytest.raises(LegacyBackupError, match="12"):
        create_legacy_encrypted_backup(source, "corta")
    wrong = tmp_path / "wrong.db"
    wrong.write_text("no es sqlite")
    with pytest.raises(LegacyBackupError, match="SQLite en claro"):
        create_legacy_encrypted_backup(wrong, "clave-segura-para-prueba")
    assert not (tmp_path / "legacy-backups").exists()


def test_backup_does_not_follow_source_symlink(tmp_path):
    source = tmp_path / "orbis.db"
    _source(source)
    alias = tmp_path / "alias.db"
    alias.symlink_to(source)
    with pytest.raises(LegacyBackupError, match="archivo regular"):
        create_legacy_encrypted_backup(alias, "clave-segura-para-prueba")


def test_fingerprint_detects_changed_content_with_same_row_count(tmp_path):
    source = tmp_path / "orbis.db"
    _source(source)
    result = create_legacy_encrypted_backup(source, "clave-segura-para-prueba")
    backup = Path(result["path"])
    with closing(sqlcipher3.connect(backup)) as db:
        db.execute("PRAGMA key = 'clave-segura-para-prueba'")
        db.execute("UPDATE operations SET note = 'alterado' WHERE id = 1")
        db.commit()
    checked = verify_legacy_encrypted_backup(backup, "clave-segura-para-prueba")
    assert checked["tables"] == result["tables"]
    assert checked["fingerprint"] != result["fingerprint"]


def test_backup_preserves_autoincrement_sequence(tmp_path):
    source = tmp_path / "orbis.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)")
        db.execute("INSERT INTO operations (id,note) VALUES (50,'anterior')")
        db.execute("DELETE FROM operations")
        db.commit()
    result = create_legacy_encrypted_backup(source, "clave-segura-para-prueba")
    with closing(sqlcipher3.connect(result["path"])) as db:
        db.execute("PRAGMA key = 'clave-segura-para-prueba'")
        db.execute("INSERT INTO operations (note) VALUES ('siguiente')")
        assert db.execute("SELECT id FROM operations").fetchone()[0] == 51
