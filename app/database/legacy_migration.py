"""Cifrado offline de la base PAPER SQLAlchemy, con recuperación SQLCipher.

No ejecutarlo mientras Orbis esté abierto. El fichero original deja de existir
en claro tras el reemplazo, pero no se promete borrado físico en SSD ni se
eliminan otras copias antiguas del usuario.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from uuid import uuid4

import sqlcipher3

from app.database.legacy_backup import (
    LegacyBackupError, _fingerprint, _inventory,
    create_legacy_encrypted_backup, verify_legacy_encrypted_backup)
from app.database.legacy_lease import exclusive_legacy_lease
from app.database.legacy_runtime import LegacyDatabaseError, legacy_database_format


def _identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError:
        raise LegacyDatabaseError("PAPER dejó de estar disponible") from None
    if not stat.S_ISREG(info.st_mode):
        raise LegacyDatabaseError("La base PAPER no es un archivo regular")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _no_journals(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise LegacyDatabaseError("PAPER conserva archivos de journal; cierra ORBIS antes de cifrar")


def _snapshot(path: Path) -> dict:
    with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return {"tables": _inventory(db),
                "user_version": int(db.execute("PRAGMA user_version").fetchone()[0]),
                "fingerprint": _fingerprint(db)}


def check_legacy_migration_readiness(path: Path) -> None:
    """Comprueba bloqueos básicos sin pedir una clave ni alterar la base.

    La migración vuelve a comprobarlo después de pedir la contraseña: este
    preflight sólo evita solicitar un secreto cuando ya se sabe que fallará.
    """
    path = Path(path).expanduser().absolute()
    with exclusive_legacy_lease(path):
        if legacy_database_format(path) != "plain":
            raise LegacyDatabaseError("La migración requiere una base PAPER SQLite en claro")
        _no_journals(path)
        _identity(path)


def encrypt_active_legacy_database(path: Path, confirmation: str, passphrase: str) -> dict:
    """Verifica, respalda y reemplaza una SQLite PAPER plana bajo bloqueo."""
    if confirmation != "CIFRAR PAPER":
        raise LegacyDatabaseError("Debes confirmar exactamente CIFRAR PAPER")
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise LegacyDatabaseError("La contraseña PAPER debe tener entre 12 y 1024 caracteres")
    path = Path(path).expanduser().absolute()
    with exclusive_legacy_lease(path):
        if legacy_database_format(path) != "plain":
            raise LegacyDatabaseError("La migración requiere una base PAPER SQLite en claro")
        _no_journals(path)
        original = _identity(path)
        try:
            before = _snapshot(path)
            backup = create_legacy_encrypted_backup(path, passphrase)
            verified = verify_legacy_encrypted_backup(Path(backup["path"]), passphrase)
        except (LegacyBackupError, sqlcipher3.DatabaseError, OSError) as exc:
            raise LegacyDatabaseError("No se pudo verificar la copia PAPER; no se cifró la base activa") from exc
        if before != verified:
            raise LegacyDatabaseError("PAPER cambió durante el respaldo; no se sustituyó")
        staged = path.with_name(f".{path.name}.encrypt-{uuid4().hex[:12]}")
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(staged, flags, 0o600)
            with Path(backup["path"]).open("rb") as source, os.fdopen(descriptor, "wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if verify_legacy_encrypted_backup(staged, passphrase) != before:
                raise LegacyDatabaseError("PAPER cifrado difiere del original verificado")
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                             timeout=0, isolation_level=None)) as guard:
                    guard.execute("BEGIN EXCLUSIVE")
                    _no_journals(path)
                    if _identity(path) != original:
                        raise LegacyDatabaseError("PAPER cambió durante la migración; no se sustituyó")
                    # La identidad de archivo no prueba el contenido: un escritor
                    # externo puede restaurar tamaño y mtime. El lock exclusivo
                    # fija la última huella lógica justo antes del reemplazo.
                    try:
                        final = {
                            "tables": _inventory(guard),
                            "user_version": int(guard.execute("PRAGMA user_version").fetchone()[0]),
                            "fingerprint": _fingerprint(guard),
                        }
                    except LegacyBackupError:
                        raise LegacyDatabaseError(
                            "PAPER ya no supera la comprobación de integridad; no se sustituyó") from None
                    if final != before:
                        raise LegacyDatabaseError("PAPER cambió durante la migración; no se sustituyó")
                    replaced = False
                    try:
                        os.replace(staged, path)
                        replaced = True
                        path.chmod(0o600)
                        parent_fd = os.open(path.parent, os.O_RDONLY)
                        try:
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                    except OSError as exc:
                        if replaced:
                            raise LegacyDatabaseError(
                                "PAPER se cifró, pero falló la sincronización final; revisa la copia") from exc
                        raise LegacyDatabaseError(
                            "No se pudo sustituir PAPER; el original permanece") from exc
            except sqlite3.Error:
                raise LegacyDatabaseError("Otro proceso usa PAPER; no se sustituyó") from None
            return {"encrypted": True, "path": str(path), "backup": backup["path"],
                    "verified": before}
        except LegacyBackupError as exc:
            raise LegacyDatabaseError("No se pudo verificar PAPER cifrado; no se sustituyó") from exc
        except OSError as exc:
            raise LegacyDatabaseError("No se pudo preparar PAPER cifrado; no se sustituyó") from exc
        finally:
            staged.unlink(missing_ok=True)
