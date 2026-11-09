"""Copia cifrada, sin sustitución, de la base SQLite PAPER heredada.

La copia es un paso preparatorio para migrar el motor SQLAlchemy. No cambia el
formato de la base activa ni elimina los ficheros heredados en claro.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import pickle
import sqlite3
import stat
import tempfile
from uuid import uuid4

import sqlcipher3


class LegacyBackupError(ValueError):
    pass


def _key(db: sqlcipher3.Connection, passphrase: str) -> None:
    db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")


def _inventory(db: sqlcipher3.Connection) -> dict[str, int]:
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise LegacyBackupError("La base PAPER heredada no supera la comprobación de integridad")
    names = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {name: int(db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0])
            for name in names}


def _fingerprint(db: sqlcipher3.Connection) -> str:
    """Huella lógica, independiente del orden físico de filas y del cifrado."""
    schema = list(db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"))
    digest = hashlib.sha256(pickle.dumps(schema, protocol=4))
    for name, in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        row_hashes = [hashlib.sha256(pickle.dumps(row, protocol=4)).digest()
                      for row in db.execute(f"SELECT * FROM {quoted}")]
        digest.update(name.encode("utf-8"))
        for row_hash in sorted(row_hashes):
            digest.update(row_hash)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        digest.update(pickle.dumps(list(db.execute(
            "SELECT name,seq FROM sqlite_sequence ORDER BY name")), protocol=4))
    return digest.hexdigest()


def verify_legacy_encrypted_backup(path: Path, passphrase: str) -> dict:
    """Reabre una copia PAPER cifrada y resume su integridad y contenido."""
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise LegacyBackupError("La contraseña debe tener entre 12 y 1024 caracteres")
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise LegacyBackupError("La copia PAPER no es un archivo regular")
    with path.open("rb") as file:
        if file.read(16) == b"SQLite format 3\x00":
            raise LegacyBackupError("La copia PAPER no está cifrada")
    try:
        with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            _key = "PRAGMA key = '" + passphrase.replace("'", "''") + "'"
            db.execute(_key)
            tables = _inventory(db)
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            fingerprint = _fingerprint(db)
        return {"tables": tables, "user_version": version, "fingerprint": fingerprint}
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
        raise LegacyBackupError("La copia PAPER cifrada no se pudo abrir o verificar") from None


def create_legacy_encrypted_backup(source: Path, passphrase: str,
                                   destination_dir: Path | None = None,
                                   *, source_passphrase: str | None = None) -> dict:
    """Exporta un snapshot SQLCipher verificado desde PAPER plano o cifrado.

    La contraseña sólo llega como argumento en memoria, nunca como opción de
    línea de comandos. El archivo se publica con enlace exclusivo tras la
    verificación, por lo que una copia previa no puede ser sobrescrita. Una
    fuente cifrada exige su propia clave de desbloqueo; la clave de la copia
    puede ser distinta. La fuente no se modifica.
    """
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise LegacyBackupError("La contraseña debe tener entre 12 y 1024 caracteres")
    source = Path(source).expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise LegacyBackupError("La base PAPER heredada debe ser un archivo regular")
    if not stat.S_ISREG(source.stat().st_mode):
        raise LegacyBackupError("La base PAPER heredada debe ser un archivo regular")
    with source.open("rb") as file:
        source_is_encrypted = file.read(16) != b"SQLite format 3\x00"
    if source_is_encrypted and (not isinstance(source_passphrase, str)
                                or not 12 <= len(source_passphrase) <= 1024):
        raise LegacyBackupError(
            "La base PAPER no es SQLite en claro; si está cifrada requiere la contraseña activa de desbloqueo")
    if not source_is_encrypted and source_passphrase is not None:
        raise LegacyBackupError("PAPER en claro no admite una contraseña activa")
    directory = (Path(destination_dir).expanduser().absolute() if destination_dir
                 else source.parent / "legacy-backups")
    if directory.is_symlink():
        raise LegacyBackupError("La carpeta de copias no puede ser un enlace")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    if not directory.is_dir():
        raise LegacyBackupError("La carpeta de copias no es un directorio")
    descriptor, staged_name = tempfile.mkstemp(prefix=".legacy-pending-", suffix=".db", dir=directory)
    staged = Path(staged_name)
    os.close(descriptor)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    published = directory / f"legacy-paper-{stamp}-{uuid4().hex[:12]}.db"
    try:
        with closing(sqlcipher3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
            if source_is_encrypted:
                _key(db, source_passphrase)
            db.execute("ATTACH DATABASE ? AS encrypted KEY ?", (str(staged), passphrase))
            try:
                db.execute("BEGIN")
                before = _inventory(db)
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                fingerprint = _fingerprint(db)
                db.execute("SELECT sqlcipher_export('encrypted')").fetchone()
                db.execute(f"PRAGMA encrypted.user_version = {version}")
                db.execute("COMMIT")
            except Exception:
                db.rollback()
                raise
            finally:
                db.execute("DETACH DATABASE encrypted")
        with staged.open("rb") as file:
            if file.read(16) == b"SQLite format 3\x00":
                raise LegacyBackupError("La copia PAPER no quedó cifrada")
            os.fsync(file.fileno())
        verified = verify_legacy_encrypted_backup(staged, passphrase)
        if (before != verified["tables"] or version != verified["user_version"]
                or fingerprint != verified["fingerprint"]):
            raise LegacyBackupError("La copia PAPER no coincide con el snapshot de origen")
        os.link(staged, published, follow_symlinks=False)
        parent_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return {"path": str(published), **verified, "encrypted": True}
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError) as exc:
        raise LegacyBackupError("No se pudo crear o verificar la copia PAPER cifrada") from exc
    finally:
        staged.unlink(missing_ok=True)
