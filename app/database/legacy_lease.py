"""Lease local del PAPER SQLAlchemy para coordinar uso y migración offline."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows aún no admite migración offline
    fcntl = None

from app.database.legacy_runtime import LegacyDatabaseError


def _open_lock(path: Path) -> int:
    if fcntl is None:
        raise LegacyDatabaseError("La lease PAPER requiere bloqueo de archivos del sistema")
    path = Path(path).expanduser().absolute()
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise LegacyDatabaseError("La lease PAPER no admite enlaces")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise LegacyDatabaseError("La lease PAPER no es un archivo regular")
    os.fchmod(descriptor, 0o600)
    return descriptor


def acquire_legacy_runtime_lease(path: Path) -> int:
    """Mantiene una lease compartida hasta que termine el proceso servidor."""
    descriptor = _open_lock(path)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise LegacyDatabaseError("PAPER está bloqueado por una migración offline") from None
    return descriptor


@contextmanager
def exclusive_legacy_lease(path: Path):
    descriptor = _open_lock(path)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LegacyDatabaseError("Cierra ORBIS antes de cifrar PAPER") from None
        yield
    finally:
        os.close(descriptor)
