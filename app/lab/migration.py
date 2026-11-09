"""Migración offline de LAB plano a SQLCipher con copia cifrada recuperable."""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4

from app.core.local_sqlite import sqlite_file_digest
from app.lab.backup import _copy_exclusive, _lab_lease, verify_lab_backup
from app.lab.engine import LabError
from app.lab.secure_backup import create_secure_lab_backup, verify_secure_lab_backup
from app.lab.store import LabStore


SQLITE_HEADER = b"SQLite format 3\x00"


def _identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError:
        raise LabError("LAB dejó de estar disponible durante la migración") from None
    if not stat.S_ISREG(info.st_mode):
        raise LabError("La ruta LAB no es un archivo regular")
    # Abrir LAB estrecha permisos a 0600 y puede cambiar ctime sin tocar datos.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _no_journals(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise LabError("LAB conserva archivos de journal; cierra Orbis antes de cifrar")


def encrypt_active_lab(store: LabStore, confirmation: str, passphrase: str) -> dict:
    """Verifica, respalda y reemplaza LAB sólo con lease exclusiva y misma identidad."""
    if confirmation != "CIFRAR LAB":
        raise LabError("Debes confirmar exactamente CIFRAR LAB")
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise LabError("La contraseña LAB debe tener entre 12 y 1024 caracteres")
    if store.encrypted:
        raise LabError("La migración requiere un LAB plano sin clave activa")
    path = store.path
    with _lab_lease(path, exclusive=True):
        if not path.exists() or path.is_symlink() or not path.is_file():
            raise LabError("No existe una base LAB plana regular que cifrar")
        with path.open("rb") as source:
            if source.read(16) != SQLITE_HEADER:
                raise LabError("LAB activo no está en formato SQLite plano")
        _no_journals(path)
        original = _identity(path)
        original_file = sqlite_file_digest(path)
        if original_file[:4] != original:
            raise LabError("LAB cambió antes del respaldo; no se sustituyó")
        verified = verify_lab_backup(path)
        backup = create_secure_lab_backup(store, passphrase)
        if backup["verified"] != verified:
            raise LabError("LAB cambió durante el respaldo; no se sustituyó")
        staged = path.with_name(f".{path.name}.encrypt-{uuid4().hex[:12]}")
        try:
            _copy_exclusive(Path(backup["path"]), staged)
            if verify_secure_lab_backup(staged, passphrase) != verified:
                raise LabError("LAB cifrado difiere del original verificado")
            with staged.open("rb") as encrypted:
                if encrypted.read(16) == SQLITE_HEADER:
                    raise LabError("La nueva base LAB no quedó cifrada")
            # Esta segunda barrera detecta clientes SQLite externos que no
            # conocen la lease de Orbis y evita una escritura entre la última
            # comprobación y el reemplazo.
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                             timeout=0, isolation_level=None)) as guard:
                    guard.execute("BEGIN EXCLUSIVE")
                    _no_journals(path)
                    if _identity(path) != original:
                        raise LabError("LAB cambió durante la migración; no se sustituyó")
                    if sqlite_file_digest(path) != original_file:
                        raise LabError("LAB cambió durante la migración; no se sustituyó")
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
                            raise LabError("LAB se cifró, pero falló la sincronización final; revisa la copia") from exc
                        raise LabError("No se pudo sustituir LAB; el original permanece") from exc
            except sqlite3.Error:
                raise LabError("Otro proceso usa LAB o no se pudo obtener bloqueo exclusivo") from None
            return {"encrypted": True, "backup": backup["name"], "verified": verified,
                    "lab_path": str(path)}
        finally:
            staged.unlink(missing_ok=True)
