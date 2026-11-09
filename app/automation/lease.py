"""Lease de proceso para impedir migración offline con Orbis abierto."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from app.lab.engine import LabError


@contextmanager
def automation_lease(path: Path, *, exclusive: bool = False):
    path = Path(path).expanduser().absolute()
    if fcntl is None:
        if exclusive:
            raise LabError("Migración offline de automatizaciones no disponible en esta plataforma")
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
        raise LabError("No se pudo abrir el cerrojo de automatizaciones") from exc
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            raise LabError("Cerrojo de automatizaciones inválido")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LabError("Orbis sigue usando automatizaciones; ciérralo antes de migrar") from exc
        yield
    finally:
        os.close(descriptor)
