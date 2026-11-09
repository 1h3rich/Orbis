#!/usr/bin/env python3
"""Restaura una copia DCA SQLCipher con Orbis cerrado y preimagen cifrada."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automation.migration import restore_secure_automation  # noqa: E402
from app.automation.store import AutomationStore  # noqa: E402
from app.lab.engine import LabError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--automation-db", default=os.environ.get(
        "ORBIS_AUTOMATION_PATH", "~/.local/share/orbis/automation.db"))
    parser.add_argument("--restore", required=True, help="Nombre de la copia gestionada")
    parser.add_argument("--confirm", default="", help="Escribir exactamente RESTAURAR DCA")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="Leer contraseña desde stdin")
    args = parser.parse_args(argv)
    try:
        if args.confirm != "RESTAURAR DCA":
            raise LabError("Debes confirmar exactamente RESTAURAR DCA")
        if args.passphrase_stdin:
            passphrase = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            passphrase = getpass.getpass("Contraseña de copia DCA: ")
        else:
            raise LabError("Se requiere --passphrase-stdin o una terminal interactiva")
        result = restore_secure_automation(AutomationStore(Path(args.automation_db)),
                                           args.restore, args.confirm, passphrase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except LabError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
