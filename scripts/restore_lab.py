#!/usr/bin/env python3
"""Inspecciona o restaura una copia LAB con Orbis completamente cerrado.

No se conecta a exchanges ni altera PAPER/REAL. Restaurar sustituye lab.db;
conserva una preimagen privada en lab-backups/ para deshacer manualmente.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lab.backup import list_lab_backups, restore_lab_backup, verify_lab_backup  # noqa: E402
from app.lab.secure_backup import (SECURE_NAME, SECURE_RECOVERY_NAME,
                                   verify_secure_lab_backup)  # noqa: E402
from app.lab.engine import LabError  # noqa: E402
from app.lab.store import LabStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-db", default=os.environ.get(
        "ORBIS_LAB_PATH", "~/.local/share/orbis/lab.db"),
        help="Ruta de la base LAB; debe coincidir con la usada por Orbis")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="Mostrar copias gestionadas")
    action.add_argument("--inspect", metavar="NOMBRE", help="Verificar una copia sin modificar LAB")
    action.add_argument("--restore", metavar="NOMBRE", help="Restaurar una copia gestionada")
    parser.add_argument("--confirm", default="", help="Para restaurar, escribir exactamente RESTAURAR")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="Leer contraseña de copia cifrada desde stdin, no desde argumentos")
    parser.add_argument("--active-passphrase-stdin", action="store_true",
                        help="Leer contraseña de LAB activo cifrado desde stdin, después de la de copia")
    args = parser.parse_args(argv)
    path = Path(args.lab_db).expanduser().absolute()
    store = LabStore(path)

    def backup_passphrase(name: str) -> str | None:
        if not (SECURE_NAME.fullmatch(name) or SECURE_RECOVERY_NAME.fullmatch(name)):
            return None
        if args.passphrase_stdin:
            return sys.stdin.readline().rstrip("\r\n")
        if not sys.stdin.isatty():
            raise LabError("La copia cifrada requiere --passphrase-stdin o una terminal interactiva")
        return getpass.getpass("Contraseña de la copia LAB: ")

    try:
        if args.list:
            print(json.dumps(list_lab_backups(store), ensure_ascii=False, indent=2))
            return 0
        if args.inspect:
            names = {row["name"] for row in list_lab_backups(store)}
            if args.inspect not in names:
                raise LabError("Selecciona una copia LAB gestionada por nombre")
            passphrase = backup_passphrase(args.inspect)
            verifier = (verify_secure_lab_backup if passphrase is not None else verify_lab_backup)
            arguments = ((store.path.parent / "lab-backups" / args.inspect, passphrase)
                         if passphrase is not None else
                         (store.path.parent / "lab-backups" / args.inspect,))
            print(json.dumps(verifier(*arguments),
                             ensure_ascii=False, indent=2))
            return 0
        if args.confirm != "RESTAURAR":
            raise LabError("Debes confirmar exactamente RESTAURAR")
        copy_passphrase = backup_passphrase(args.restore)
        if path.is_symlink():
            raise LabError("La ruta LAB activa no puede ser un enlace")
        active_encrypted = False
        if path.is_file():
            with path.open("rb") as active:
                header = active.read(16)
            active_encrypted = bool(header) and header != b"SQLite format 3\x00"
        if active_encrypted:
            if args.active_passphrase_stdin:
                active_passphrase = sys.stdin.readline().rstrip("\r\n")
            elif sys.stdin.isatty():
                active_passphrase = getpass.getpass("Contraseña de LAB activo: ")
            else:
                raise LabError("LAB activo cifrado requiere --active-passphrase-stdin o una terminal interactiva")
            store = LabStore(path, passphrase=active_passphrase)
        result = restore_lab_backup(store, args.restore, args.confirm, copy_passphrase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except LabError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
