"""Cerrojo de proceso para migrar Spot PAPER únicamente con Orbis cerrado."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from app.automation.spot_paper_access import SpotPaperAccessError


@contextmanager
def spot_paper_lease(path: Path, *, exclusive: bool = False):
    path = Path(path).expanduser().absolute()
    if fcntl is None:
        if exclusive:
            raise SpotPaperAccessError("Migración Spot PAPER no disponible en esta plataforma")
        yield
        return
    lock_path = path.with_name(path.name + ".app-lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SpotPaperAccessError("No se pudo abrir el cerrojo Spot PAPER") from exc
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            raise SpotPaperAccessError("Cerrojo Spot PAPER inválido")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SpotPaperAccessError("Orbis sigue usando Spot PAPER; ciérralo antes de migrar") from exc
        yield
    finally:
        os.close(descriptor)
