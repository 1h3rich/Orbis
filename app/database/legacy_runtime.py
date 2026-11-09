"""Conexión SQLAlchemy a un almacén PAPER heredado cifrado con SQLCipher.

No convierte bases existentes ni acepta contraseñas por URL. La clave se
mantiene sólo en este proceso mientras el motor esté activo.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable

import sqlcipher3
from sqlalchemy import create_engine


class LegacyDatabaseError(ValueError):
    pass


SQLITE_HEADER = b"SQLite format 3\x00"


def legacy_database_format(path: Path) -> str:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise LegacyDatabaseError("La ruta PAPER heredada no puede ser un enlace")
    if not path.exists():
        return "missing"
    if not path.is_file():
        raise LegacyDatabaseError("La ruta PAPER heredada no es un archivo regular")
    with path.open("rb") as file:
        header = file.read(16)
    if not header:
        return "empty"
    return "plain" if header == SQLITE_HEADER else "encrypted_or_invalid"


def inspect_legacy_sibling_copies(path: Path) -> dict:
    """Detecta copias vecinas conocidas sin abrir ni seguir enlaces externos.

    Es un inventario limitado al directorio de la base activa, no un análisis
    de todo el disco ni una garantía de que no existan otras exportaciones.
    """
    path = Path(path).expanduser().absolute()
    cleartext: list[str] = []
    unverified: list[str] = []
    complete = True
    try:
        siblings = path.parent.iterdir()
        for sibling in siblings:
            name = sibling.name
            suffix = name[len(path.name):] if name.startswith(path.name) else ""
            if not (suffix == ".bak" or suffix.startswith(".bak-")
                    or suffix == ".orig" or suffix.startswith(".orig-")):
                continue
            if len(cleartext) + len(unverified) >= 100:
                complete = False
                break
            try:
                if sibling.is_symlink() or not sibling.is_file():
                    unverified.append(name)
                    continue
                with sibling.open("rb") as file:
                    header = file.read(16)
            except OSError:
                unverified.append(name)
                continue
            if header == SQLITE_HEADER:
                cleartext.append(name)
            else:
                unverified.append(name)
    except OSError:
        complete = False
    return {"plaintext_sibling_copies": sorted(cleartext),
            "unverified_sibling_copies": sorted(unverified),
            "sibling_scan_complete": complete}


def _key(db: sqlcipher3.Connection, passphrase: str) -> None:
    db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")


class LegacyPaperAccess:
    """Clave PAPER temporal; no se persiste ni se comparte con otras bóvedas."""

    def __init__(self, path: Path, *, timeout_seconds: int = 900,
                 clock=time.monotonic):
        self.path = Path(path).expanduser().absolute()
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._key_value: str | None = None
        self._expires_at = 0.0
        self._intent = 0

    def get(self) -> str | None:
        with self._lock:
            if self._key_value is not None and self._clock() >= self._expires_at:
                self._key_value = None
            return self._key_value

    def status(self) -> dict:
        with self._lock:
            key = self.get()
            remaining = max(0, int(self._expires_at - self._clock())) if key else 0
        return {"format": legacy_database_format(self.path),
                "unlocked": bool(key), "remaining_seconds": remaining}

    def reserve_unlock(self) -> int:
        with self._lock:
            self._intent += 1
            self._key_value = None
            self._expires_at = 0.0
            return self._intent

    def require_attempt(self,attempt: int) -> None:
        with self._lock:
            if attempt != self._intent:
                raise LegacyDatabaseError("Desbloqueo PAPER cancelado por una solicitud posterior")

    def discard_attempt(self,attempt: int) -> None:
        with self._lock:
            if attempt == self._intent:
                self._intent += 1;self._key_value = None;self._expires_at = 0.0

    def unlock(self, passphrase: str, *, attempt: int|None=None) -> dict:
        intent = self.reserve_unlock() if attempt is None else attempt
        with self._lock:
            if intent != self._intent:
                raise LegacyDatabaseError("Desbloqueo PAPER cancelado por una solicitud posterior")
        if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
            raise LegacyDatabaseError("La contraseña PAPER debe tener entre 12 y 1024 caracteres")
        if legacy_database_format(self.path) != "encrypted_or_invalid":
            raise LegacyDatabaseError("La base PAPER no está cifrada")
        try:
            with closing(sqlcipher3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                _key(db, passphrase)
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise LegacyDatabaseError("La base PAPER no supera la comprobación de integridad")
        except sqlcipher3.DatabaseError:
            raise LegacyDatabaseError("No se pudo desbloquear la base PAPER cifrada") from None
        with self._lock:
            if intent != self._intent:
                raise LegacyDatabaseError("Desbloqueo PAPER cancelado por una solicitud posterior")
            self._key_value = passphrase
            self._expires_at = self._clock() + self.timeout_seconds
        return self.status()

    def lock(self) -> dict:
        with self._lock:
            self._intent += 1
            self._key_value = None
            self._expires_at = 0.0
        return self.status()


def create_new_encrypted_legacy_database(path: Path, passphrase: str, *,
                                         initialize: Callable[[Path,str],None]|None=None) -> None:
    """Publica una base PAPER cifrada vacía sin exponer una SQLite en claro."""
    path = Path(path).expanduser().absolute()
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise LegacyDatabaseError("La contraseña PAPER debe tener entre 12 y 1024 caracteres")
    if legacy_database_format(path) != "missing":
        raise LegacyDatabaseError("PAPER ya tiene una base; no se sobrescribirá")
    if path.parent.is_symlink():
        raise LegacyDatabaseError("La carpeta PAPER no puede ser un enlace")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(prefix=".legacy-init-", suffix=".db", dir=path.parent)
    staged = Path(staged_name)
    os.close(descriptor)
    try:
        with closing(sqlcipher3.connect(staged)) as db:
            _key(db, passphrase)
            db.execute("PRAGMA user_version = 1")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LegacyDatabaseError("No se pudo verificar el nuevo PAPER cifrado")
            db.commit()
        if initialize is not None:
            initialize(staged,passphrase)
            with closing(sqlcipher3.connect(staged.as_uri()+"?mode=ro",uri=True)) as check:
                _key(check,passphrase)
                if check.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise LegacyDatabaseError('El nuevo PAPER no supera la comprobación del esquema')
        if legacy_database_format(staged) != "encrypted_or_invalid":
            raise LegacyDatabaseError("El nuevo PAPER no quedó cifrado")
        try:
            os.link(staged, path, follow_symlinks=False)
        except FileExistsError:
            raise LegacyDatabaseError("PAPER cambió durante la creación; no se reemplazó") from None
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        staged.unlink(missing_ok=True)


def create_encrypted_legacy_engine(database_url: str, path: Path,
                                   passphrase: str | Callable[[], str | None]):
    """Crea un engine cuyo *cada* conexión DB-API queda desbloqueada primero."""
    path = Path(path).expanduser().absolute()
    if legacy_database_format(path) != "encrypted_or_invalid":
        raise LegacyDatabaseError("La base PAPER heredada no está cifrada")
    provider = passphrase if callable(passphrase) else lambda: passphrase
    initial = provider()
    if not isinstance(initial, str) or not 12 <= len(initial) <= 1024:
        raise LegacyDatabaseError("La contraseña PAPER debe tener entre 12 y 1024 caracteres")
    try:
        with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as check:
            _key(check, initial)
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LegacyDatabaseError("La base PAPER cifrada no supera la comprobación de integridad")
    except sqlcipher3.DatabaseError:
        raise LegacyDatabaseError("No se pudo desbloquear la base PAPER cifrada") from None

    def connect():
        current = provider()
        if current is None:
            raise LegacyDatabaseError("PAPER cifrado está bloqueado")
        # FastAPI ejecuta dependencias síncronas en su pool de hilos: la
        # sesión puede cerrarse desde un hilo distinto al de su apertura.
        db = sqlcipher3.connect(str(path), timeout=30, check_same_thread=False)
        try:
            _key(db, current)
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return db
        except Exception:
            db.close()
            raise

    return create_engine(database_url, creator=connect)


def consume_passphrase_fd() -> str | None:
    """Lee una clave de un descriptor heredado, nunca de argv ni del entorno."""
    value = os.environ.pop("ORBIS_DATABASE_KEY_FD", None)
    if value is None:
        return None
    try:
        descriptor = int(value)
        if descriptor < 3:
            raise ValueError
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 4096:
                raise LegacyDatabaseError("Contraseña PAPER demasiado larga")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        raise LegacyDatabaseError("Descriptor de contraseña PAPER inválido") from None
    finally:
        if "descriptor" in locals() and descriptor >= 3:
            os.close(descriptor)
