"""Clave efímera para automatizaciones cifradas; nunca persiste en disco."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import os
import tempfile
import threading
import time

import sqlcipher3

from app.security.local_session import DEFAULT_SESSION_SECONDS, fixed_session_duration, pending_unlock

from app.lab.engine import LabError


SQLITE_HEADER = b"SQLite format 3\x00"


def automation_format(path: Path) -> str:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise LabError("La ruta de automatizaciones no puede ser un enlace")
    if not path.exists():
        return "missing"
    if not path.is_file():
        raise LabError("La ruta de automatizaciones no es un archivo regular")
    with path.open("rb") as source:
        header = source.read(16)
    if not header:
        return "empty"
    return "plain" if header == SQLITE_HEADER else "encrypted_or_invalid"


class AutomationAccess:
    def __init__(self, *, timeout_seconds: int = DEFAULT_SESSION_SECONDS, clock=time.monotonic):
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._keys: dict[Path, tuple[str, float, int]] = {}
        self._unlock_attempts: dict[Path, object] = {}

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
                return None
            return entry[0]

    def status(self, path: Path) -> dict:
        path = Path(path).expanduser().absolute()
        kind = automation_format(path)
        with self._lock:
            key = self.get(path)
            entry = self._keys.get(path)
            remaining = max(0, int(entry[1] - self._clock())) if key and entry else 0
        return {"format": kind, "unlocked": bool(key), "remaining_seconds": remaining, "session_seconds": entry[2] if key and entry else 0}

    def unlock(self, path: Path, passphrase: str, *, session_seconds: int | None = None) -> dict:
        duration = self._duration(session_seconds)
        path = Path(path).expanduser().absolute()
        if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
            raise LabError("La contraseña de automatizaciones debe tener entre 12 y 1024 caracteres")
        if automation_format(path) != "encrypted_or_invalid":
            raise LabError("La base de automatizaciones no está cifrada o no existe")
        with pending_unlock(self._lock, self._unlock_attempts, path) as attempt:
            try:
                with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
                    if db.execute("PRAGMA user_version").fetchone()[0] not in (3, 4, 5, 6):
                        raise LabError("Versión cifrada de automatizaciones no compatible")
                    db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except sqlcipher3.DatabaseError:
                raise LabError("Automatizaciones no se pudo desbloquear; contraseña o formato incompatibles") from None
            with self._lock:
                if self._unlock_attempts.get(path) is not attempt:
                    raise LabError("Desbloqueo cancelado por un bloqueo o una solicitud posterior")
                self._keys[path] = (passphrase, self._clock() + duration, duration)
        return self.status(path)

    def create(self, path: Path, passphrase: str, *, session_seconds: int | None = None) -> dict:
        """Inicializa DCA cifrado sin sustituir una base preexistente."""
        duration = self._duration(session_seconds)
        path = Path(path).expanduser().absolute()
        if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
            raise LabError("La contraseña de automatizaciones debe tener entre 12 y 1024 caracteres")
        with self._lock:
            kind = automation_format(path)
            if kind != "missing":
                raise LabError("DCA ya tiene una base; no se sobrescribirá")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, staged_name = tempfile.mkstemp(prefix=".automation-init-", suffix=".db", dir=path.parent)
            staged = Path(staged_name)
            os.close(descriptor)
            from app.automation.store import AutomationStore
            try:
                with AutomationStore(staged, passphrase=passphrase).connect():
                    pass
                if automation_format(staged) != "encrypted_or_invalid":
                    raise LabError("No se pudo verificar el cifrado de DCA")
                try:
                    os.link(staged, path, follow_symlinks=False)
                except FileExistsError:
                    raise LabError("DCA cambió durante la creación; vuelve a comprobar su estado") from None
            finally:
                staged.unlink(missing_ok=True)
            self._keys[path] = (passphrase, self._clock() + duration, duration)
        return self.status(path)

    def lock(self, path: Path) -> dict:
        path = Path(path).expanduser().absolute()
        with self._lock:
            self._unlock_attempts.pop(path, None)
            self._keys.pop(path, None)
        return self.status(path)


automation_access = AutomationAccess()
