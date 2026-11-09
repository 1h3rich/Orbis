"""Detecta código Python cambiado desde el arranque, sin revelar archivos.

No reinicia procesos ni toca las bóvedas. Una sesión PAPER cifrada puede
necesitar una contraseña al reiniciar, por lo que sólo informa al usuario.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class RuntimeSourceMonitor:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._loaded_digest = self._digest()

    def _digest(self) -> bytes:
        digest = hashlib.sha256()
        for folder in ("app", "scripts"):
            source = self.root / folder
            for path in sorted(source.rglob("*.py")) if source.exists() else ():
                if not path.is_file() or path.is_symlink():
                    continue
                digest.update(path.relative_to(self.root).as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
        return digest.digest()

    def restart_required(self) -> bool:
        try:
            return self._digest() != self._loaded_digest
        except OSError:
            # Un archivo ilegible o desaparecido no demuestra que el proceso
            # siga representando el código en disco.
            return True
