"""Desbloqueo efímero de LAB activo SQLCipher para el proceso local.

No persiste claves. El vencimiento es fijo desde el desbloqueo: la actividad
de los workers no prolonga la sesión del usuario.
"""

from __future__ import annotations

from app.lab.schema import DATASET_READ_VERSIONS

from contextlib import closing, contextmanager
from pathlib import Path
import os
import tempfile
import threading
import time

import sqlcipher3

from app.security.local_session import DEFAULT_SESSION_SECONDS, fixed_session_duration, pending_unlock

from app.lab.engine import LabError


SQLITE_HEADER = b"SQLite format 3\x00"


def lab_format(path: Path) -> str:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise LabError("La ruta LAB no puede ser un enlace")
    if not path.exists():
        return "missing"
    if not path.is_file():
        raise LabError("La ruta LAB no es un archivo regular")
    with path.open("rb") as file:
        header = file.read(16)
    if not header:
        return "empty"
    return "plain" if header == SQLITE_HEADER else "encrypted_or_invalid"


class LabAccess:
    def __init__(self, *, timeout_seconds: int = DEFAULT_SESSION_SECONDS, clock=time.monotonic):
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._keys: dict[Path, tuple[str, float, int]] = {}
        self._unlock_attempts: dict[Path, object] = {}
        self._epochs: dict[Path, object] = {}

    def _duration(self, session_seconds: int | None) -> int:
        try:
            return fixed_session_duration(session_seconds, default=self.timeout_seconds)
        except ValueError as exc:
            raise LabError(str(exc)) from None

    def get(self, path: Path) -> str | None:
        path = Path(path).expanduser().absolute()
        with self._lock:
            entry = self._keys.get(path)
            if entry is None:
                return None
            if self._clock() >= entry[1]:
                self._keys.pop(path, None)
                self._epochs.pop(path, None)
                return None
            return entry[0]

    def epoch(self, path: Path) -> object | None:
        """Opaque identity: locking/reunlocking the same key cancels old work."""
        path = Path(path).expanduser().absolute()
        with self._lock:
            return self._epochs.get(path) if self.get(path) is not None else None

    @contextmanager
    def guard(self, path: Path, epoch: object | None):
        """Serialize a short commit/read against lock or reauthentication."""
        with self._lock:
            if self.epoch(path) is not epoch:
                raise LabError('La sesión LAB cambió durante la operación')
            yield

    def status(self, path: Path) -> dict:
        path = Path(path).expanduser().absolute()
        kind = lab_format(path)
        with self._lock:
            key = self.get(path)
            entry = self._keys.get(path)
            remaining = max(0, int(entry[1] - self._clock())) if key and entry else 0
        return {"format": kind, "unlocked": bool(key),
                "remaining_seconds": remaining, "session_seconds": entry[2] if key and entry else 0}

    def unlock(self, path: Path, passphrase: str, *, session_seconds: int | None = None) -> dict:
        duration = self._duration(session_seconds)
        path = Path(path).expanduser().absolute()
        if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
            raise LabError("La contraseña LAB debe tener entre 12 y 1024 caracteres")
        if lab_format(path) != "encrypted_or_invalid":
            raise LabError("LAB activo no está cifrado o no existe")
        with pending_unlock(self._lock, self._unlock_attempts, path) as attempt:
            try:
                with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
                    if db.execute("PRAGMA user_version").fetchone()[0] not in DATASET_READ_VERSIONS:
                        raise LabError("Versión de LAB cifrado no compatible")
                    db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except sqlcipher3.DatabaseError:
                raise LabError("LAB no se pudo desbloquear; contraseña o formato incompatibles") from None
            with self._lock:
                if self._unlock_attempts.get(path) is not attempt:
                    raise LabError("Desbloqueo cancelado por un bloqueo o una solicitud posterior")
                self._keys[path] = (passphrase, self._clock() + duration, duration)
                self._epochs[path] = object()
        return self.status(path)

    def create(self, path: Path, passphrase: str, *, session_seconds: int | None = None) -> dict:
        """Publica una base LAB nueva ya cifrada sin reemplazar históricos."""
        duration = self._duration(session_seconds)
        path = Path(path).expanduser().absolute()
        if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
            raise LabError("La contraseña LAB debe tener entre 12 y 1024 caracteres")
        with self._lock:
            if lab_format(path) != "missing":
                raise LabError("LAB ya tiene una base; no se sobrescribirá")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, staged_name = tempfile.mkstemp(prefix=".lab-init-", suffix=".db", dir=path.parent)
            staged = Path(staged_name)
            os.close(descriptor)
            from app.lab.store import LabStore
            try:
                with LabStore(staged, passphrase=passphrase).connect():
                    pass
                if lab_format(staged) != "encrypted_or_invalid":
                    raise LabError("No se pudo verificar el cifrado de LAB")
                try:
                    os.link(staged, path, follow_symlinks=False)
                except FileExistsError:
                    raise LabError("LAB cambió durante la creación; vuelve a comprobar su estado") from None
            finally:
                staged.unlink(missing_ok=True)
            self._keys[path] = (passphrase, self._clock() + duration, duration)
            self._epochs[path] = object()
        return self.status(path)

    def lock(self, path: Path) -> dict:
        path = Path(path).expanduser().absolute()
        with self._lock:
            self._unlock_attempts.pop(path, None)
            self._keys.pop(path, None)
            self._epochs.pop(path, None)
        return self.status(path)


lab_access = LabAccess()
