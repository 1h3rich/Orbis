"""Copia LAB SQLCipher con contraseña independiente y restauración offline.

LAB activo sigue siendo SQLite sin cifrar. Los snapshots de trabajo en claro
son privados y temporales; no se promete borrado físico en SSD.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import uuid4

import sqlcipher3

from app.core.local_sqlite import sqlite_file_digest
from app.lab.backup import _lab_lease, verify_lab_backup
from app.lab.engine import LabError
from app.lab.store import LabStore


SECURE_NAME = re.compile(r"lab-secure-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}\.db\Z")
SECURE_RECOVERY_NAME = re.compile(
    r"pre-restore-lab-secure-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}\.db\Z")


def _password(passphrase: str) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < 12 or len(passphrase) > 1024:
        raise LabError("La contraseña de la copia LAB debe tener entre 12 y 1024 caracteres")


def _private_empty(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)


def _export(source: Path, destination: Path, *, source_password: str | None,
            destination_password: str | None) -> None:
    """SQLCipher exporta entre dos ficheros privados; sólo un esquema se adjunta."""
    with closing(sqlcipher3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        if source_password is not None:
            # PRAGMA key no admite parámetros; escapamos el literal SQL.
            db.execute("PRAGMA key = '" + source_password.replace("'", "''") + "'")
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        source_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        db.execute("ATTACH DATABASE ? AS exported KEY ?",
                   (str(destination), destination_password or ""))
        try:
            db.execute("SELECT sqlcipher_export('exported')").fetchone()
            # sqlcipher_export no copia el user_version de SQLite.
            db.execute(f"PRAGMA exported.user_version = {source_version}")
        finally:
            db.execute("DETACH DATABASE exported")
    with destination.open("rb") as file:
        os.fsync(file.fileno())


def verify_secure_lab_backup(path: Path, passphrase: str) -> dict:
    """Descifra en un directorio temporal privado y reutiliza el verificador LAB."""
    _password(passphrase)
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise LabError("La copia LAB cifrada no es un archivo regular")
    try:
        with tempfile.TemporaryDirectory(prefix=".lab-check-", dir=path.parent) as directory:
            plain = Path(directory) / "snapshot.db"
            _private_empty(plain)
            _export(path, plain, source_password=passphrase, destination_password=None)
            verified = verify_lab_backup(plain)
        return verified
    except LabError:
        raise
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
        raise LabError("La copia LAB cifrada no se pudo abrir o verificar") from None


def _create_secure_lab_backup(store: LabStore, passphrase: str, *, recovery: bool) -> dict:
    _password(passphrase)
    if store.path.is_symlink() or not store.path.is_file():
        raise LabError("No existe una base LAB que copiar")
    directory = store.path.parent / "lab-backups"
    if directory.is_symlink():
        raise LabError("La carpeta de copias LAB no puede ser un enlace")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    nonce = uuid4().hex[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = "pre-restore-lab-secure" if recovery else "lab-secure"
    destination = directory / f"{prefix}-{stamp}-{nonce}.db"
    plain = directory / f".lab-plain-{nonce}.db"
    encrypted = directory / f".lab-secure-pending-{nonce}.db"
    try:
        _private_empty(plain)
        if store.encrypted:
            # La API backup() copia páginas cifradas; export descifra el
            # snapshot privado antes de volver a cifrarlo con la clave de copia.
            _export(store.path, plain, source_password=store.current_passphrase(),
                    destination_password=None)
        else:
            with store.connect() as source, closing(sqlcipher3.connect(plain)) as target:
                source.backup(target, pages=256, sleep=0.05)
        before = verify_lab_backup(plain)
        _private_empty(encrypted)
        _export(plain, encrypted, source_password=None,
                destination_password=passphrase)
        after = verify_secure_lab_backup(encrypted, passphrase)
        if after != before:
            raise LabError("La copia LAB cifrada difiere del snapshot verificado")
        with encrypted.open("rb") as file:
            if file.read(16) == b"SQLite format 3\x00":
                raise LabError("La copia LAB no quedó cifrada")
        os.link(encrypted, destination)
        destination.chmod(0o600)
        parent_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return {"name": destination.name, "path": str(destination),
                "size_bytes": destination.stat().st_size, "encrypted": True,
                "verified": after}
    except LabError:
        raise
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
        raise LabError("No se pudo crear la copia LAB cifrada") from None
    finally:
        plain.unlink(missing_ok=True)
        encrypted.unlink(missing_ok=True)


def create_secure_lab_backup(store: LabStore, passphrase: str) -> dict:
    return _create_secure_lab_backup(store, passphrase, recovery=False)


def restore_secure_lab_backup(store: LabStore, name: str, confirmation: str,
                              passphrase: str) -> dict:
    """Restaura offline y conserva una preimagen cifrada con la misma clave."""
    _password(passphrase)
    if confirmation != "RESTAURAR":
        raise LabError("Debes confirmar exactamente RESTAURAR")
    if not (SECURE_NAME.fullmatch(name) or SECURE_RECOVERY_NAME.fullmatch(name)):
        raise LabError("Selecciona una copia LAB cifrada gestionada por nombre")
    directory = store.path.parent / "lab-backups"
    if directory.is_symlink() or not directory.is_dir():
        raise LabError("No existe la carpeta de copias LAB")
    source = directory / name
    with _lab_lease(store.path, exclusive=True):
        if store.path.is_symlink() or (store.path.exists() and not store.path.is_file()):
            raise LabError("La ruta LAB actual no es una base regular")
        if store.path.exists():
            with store.path.open("rb") as active:
                plain_active = active.read(16) == b"SQLite format 3\x00"
            if plain_active == store.encrypted:
                raise LabError("LAB activo no coincide con el formato esperado; no se sustituirá")
        if any(Path(str(store.path) + suffix).exists()
               for suffix in ("-wal", "-shm", "-journal")):
            raise LabError("LAB conserva archivos de journal; cierra Orbis antes de restaurar")
        verified = verify_secure_lab_backup(source, passphrase)
        staged = store.path.with_name(f".{store.path.name}.restore-{uuid4().hex[:12]}")
        previous = None
        try:
            _private_empty(staged)
            active_passphrase = store.current_passphrase()
            _export(source, staged, source_password=passphrase,
                    destination_password=active_passphrase)
            staged_verification = (verify_secure_lab_backup(staged, active_passphrase)
                                   if store.encrypted else verify_lab_backup(staged))
            if staged_verification != verified:
                raise LabError("LAB descifrado difiere de la copia verificada")
            if store.path.exists():
                original_file = sqlite_file_digest(store.path)
                previous = _create_secure_lab_backup(store, passphrase, recovery=True)
                driver = sqlcipher3 if store.encrypted else sqlite3
                try:
                    with closing(driver.connect(store.path.as_uri() + "?mode=rw", uri=True,
                                                timeout=0, isolation_level=None)) as guard:
                        if store.encrypted:
                            guard.execute("PRAGMA key = '" + active_passphrase.replace("'", "''") + "'")
                        guard.execute("BEGIN EXCLUSIVE")
                        if any(Path(str(store.path) + suffix).exists()
                               for suffix in ("-wal", "-shm", "-journal")):
                            raise LabError("LAB conserva journals; no se sustituyó")
                        if sqlite_file_digest(store.path) != original_file:
                            raise LabError("LAB cambió durante la restauración; no se sustituyó")
                        os.replace(staged, store.path)
                        store.path.chmod(0o600)
                        parent_fd = os.open(store.path.parent, os.O_RDONLY)
                        try:
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                except (sqlite3.DatabaseError, sqlcipher3.DatabaseError):
                    raise LabError("Otro proceso usa LAB; no se sustituyó") from None
            else:
                os.replace(staged, store.path)
                store.path.chmod(0o600)
                parent_fd = os.open(store.path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            return {"restored": True, "backup": name,
                    "previous_lab_backup": previous["name"] if previous else None,
                    "verified": verified}
        except LabError:
            raise
        except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
            raise LabError("No se pudo restaurar LAB cifrado; revisa la copia previa") from None
        finally:
            staged.unlink(missing_ok=True)
