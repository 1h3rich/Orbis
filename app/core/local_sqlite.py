"""Preparación de archivos SQLite locales con permisos privados desde su creación.

Esto reduce exposición a otros usuarios del equipo, pero NO cifra los datos.
"""

from __future__ import annotations

import os
from pathlib import Path
import hashlib
import stat


def ensure_private_sqlite(path: Path) -> None:
    """Precrea una base 0600 antes de que SQLite la abra; no toca su contenido."""
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.parent.is_dir():
        raise OSError("SQLite parent is not a directory")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError("SQLite path is not a regular file") from None
        path.chmod(0o600)
    else:
        os.close(descriptor)


def sqlite_file_digest(path: Path) -> tuple[int, int, int, int, str]:
    """Huella del mismo inode abierto, sin seguir symlinks ni cargarlo en RAM.

    Para migraciones offline: comparar antes del respaldo y de nuevo dentro
    de BEGIN EXCLUSIVE evita sustituir escrituras que restauren tamaño/mtime.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("SQLite path is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest.hexdigest()
    finally:
        os.close(descriptor)
