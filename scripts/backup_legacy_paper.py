"""Copia cifrada offline del ``orbis.db`` heredado.

Ejecutar con Orbis cerrado desde la raíz del proyecto:
python -m scripts.backup_legacy_paper --source /ruta/orbis.db
"""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path

from app.database.legacy_backup import LegacyBackupError, create_legacy_encrypted_backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="Ruta explícita de orbis.db; Orbis debe estar cerrado")
    parser.add_argument("--output-dir", type=Path,
                        help="Directorio de copias (por defecto: legacy-backups junto a la base)")
    args = parser.parse_args()
    first = getpass("Contraseña nueva para la copia cifrada: ")
    second = getpass("Repite la contraseña: ")
    if first != second:
        parser.error("Las contraseñas no coinciden")
    try:
        result = create_legacy_encrypted_backup(args.source, first, args.output_dir)
    except LegacyBackupError as exc:
        parser.error(str(exc))
    print(f"Copia cifrada y verificada: {result['path']}")
    print(f"Tablas copiadas: {len(result['tables'])}")
    print("La base activa no se ha modificado ni cifrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
