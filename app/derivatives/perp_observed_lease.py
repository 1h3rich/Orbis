"""Per-session OS ownership: live cursor readers versus one estimated closer."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from uuid import UUID

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from app.derivatives.perp_observed_types import ObservedError


class ObservedSessionLease:
    def __init__(self, path: Path, session_id: str, *, exclusive: bool):
        self.file = None
        if str(UUID(session_id)) != session_id:
            raise ObservedError('Sesión de exclusión no canónica')
        if fcntl is None:
            if exclusive:
                raise ObservedError('El cierre tras hueco requiere exclusión entre procesos no disponible aquí')
            return
        path = Path(path).expanduser().absolute()
        digest = hashlib.sha256(path.name.encode()).hexdigest()[:16]
        target = path.parent / ('.orbis-observed-' + digest + '-' + session_id + '.lock')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(target, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ObservedError('Cerrojo de sesión no regular')
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ObservedError('Detén la observación o espera al cierre ya en curso de esta sesión') from None
            self.file = os.fdopen(descriptor, 'r+b', closefd=True)
        except BaseException:
            os.close(descriptor)
            raise

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
