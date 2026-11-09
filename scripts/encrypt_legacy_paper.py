"""Migra offline una base PAPER heredada a SQLCipher, preservando copia cifrada.

Ejemplo (con Orbis cerrado):
  python -m scripts.encrypt_legacy_paper --source /ruta/orbis.db --check
  python -m scripts.encrypt_legacy_paper --source /ruta/orbis.db
"""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path

from app.database.legacy_migration import (check_legacy_migration_readiness,
                                           encrypt_active_legacy_database)
from app.database.legacy_runtime import LegacyDatabaseError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="Ruta exacta a orbis.db; cierra ORBIS antes de migrar")
    parser.add_argument("--check", action="store_true",
                        help="Comprobar formato, bloqueo y journals sin pedir contraseña ni cifrar")
    args = parser.parse_args()
    try:
        check_legacy_migration_readiness(args.source)
    except LegacyDatabaseError as exc:
        parser.error(str(exc))
    if args.check:
        print("PAPER listo para iniciar la migración offline; aún no se ha cifrado nada.")
        return 0
    confirmation = input("Escribe CIFRAR PAPER para continuar: ")
    first = getpass("Contraseña nueva para PAPER cifrado: ")
    second = getpass("Repite la contraseña: ")
    if first != second:
        parser.error("Las contraseñas no coinciden")
    try:
        result = encrypt_active_legacy_database(args.source, confirmation, first)
    except LegacyDatabaseError as exc:
        parser.error(str(exc))
    print(f"PAPER cifrado y verificado: {result['path']}")
    print(f"Copia SQLCipher recuperable: {result['backup']}")
    print("Arranca con scripts.run_orbis_local para introducir la contraseña.")
    print("Revisa manualmente otras copias antiguas en claro; este comando no las elimina.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
