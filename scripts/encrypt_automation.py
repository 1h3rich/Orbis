#!/usr/bin/env python3
"""Cifra DCA/PAPER con Orbis cerrado; conserva copia SQLCipher verificada."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automation.migration import encrypt_active_automation  # noqa: E402
from app.automation.store import AutomationStore  # noqa: E402
from app.lab.engine import LabError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--automation-db", default=os.environ.get(
        "ORBIS_AUTOMATION_PATH", "~/.local/share/orbis/automation.db"),
        help="Ruta de automatizaciones; debe coincidir con la usada por Orbis")
    parser.add_argument("--confirm", default="", help="Escribir exactamente CIFRAR DCA")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="Leer contraseña y confirmación (dos líneas) desde stdin")
    args = parser.parse_args(argv)
    try:
        if args.confirm != "CIFRAR DCA":
            raise LabError("Debes confirmar exactamente CIFRAR DCA")
        if args.passphrase_stdin:
            passphrase = sys.stdin.readline().rstrip("\r\n")
            repeated = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            passphrase = getpass.getpass("Nueva contraseña DCA: ")
            repeated = getpass.getpass("Repetir contraseña DCA: ")
        else:
            raise LabError("Se requiere --passphrase-stdin o una terminal interactiva")
        if passphrase != repeated:
            raise LabError("Las dos contraseñas DCA no coinciden")
        result = encrypt_active_automation(AutomationStore(Path(args.automation_db)),
                                           args.confirm, passphrase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except LabError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
