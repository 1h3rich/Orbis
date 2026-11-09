#!/usr/bin/env python3
"""Cifra LAB activo con Orbis cerrado; conserva una copia SQLCipher verificada."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lab.engine import LabError  # noqa: E402
from app.lab.migration import encrypt_active_lab  # noqa: E402
from app.lab.store import LabStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-db", default=os.environ.get(
        "ORBIS_LAB_PATH", "~/.local/share/orbis/lab.db"),
        help="Ruta de LAB; debe coincidir con la usada por Orbis")
    parser.add_argument("--confirm", default="", help="Escribir exactamente CIFRAR LAB")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="Leer contraseña y confirmación (dos líneas) desde stdin")
    args = parser.parse_args(argv)
    try:
        if args.confirm != "CIFRAR LAB":
            raise LabError("Debes confirmar exactamente CIFRAR LAB")
        if args.passphrase_stdin:
            passphrase = sys.stdin.readline().rstrip("\r\n")
            repeated = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            passphrase = getpass.getpass("Nueva contraseña LAB: ")
            repeated = getpass.getpass("Repetir contraseña LAB: ")
        else:
            raise LabError("Se requiere --passphrase-stdin o una terminal interactiva")
        if passphrase != repeated:
            raise LabError("Las dos contraseñas LAB no coinciden")
        result = encrypt_active_lab(LabStore(Path(args.lab_db)), args.confirm, passphrase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except LabError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
