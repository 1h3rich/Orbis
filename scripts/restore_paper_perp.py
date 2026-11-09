#!/usr/bin/env python3
"""Restaura copia GRVT PAPER SQLCipher con Orbis cerrado y preimagen."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.derivatives.paper_migration import restore_secure_paper_perp  # noqa: E402
from app.derivatives.paper_store import PaperPerpStore  # noqa: E402
from app.derivatives.paper_math import PaperPerpError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-db", default=os.environ.get(
        "ORBIS_PAPER_PERP_PATH", "~/.local/share/orbis/paper-perp.db"))
    parser.add_argument("--restore", required=True, help="Nombre de copia gestionada")
    parser.add_argument("--confirm", default="", help="Escribir exactamente RESTAURAR GRVT PAPER")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="Leer contraseña desde stdin")
    args = parser.parse_args(argv)
    try:
        if args.confirm != "RESTAURAR GRVT PAPER":
            raise PaperPerpError("Debes confirmar exactamente RESTAURAR GRVT PAPER")
        if args.passphrase_stdin:
            passphrase = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            passphrase = getpass.getpass("Contraseña de copia GRVT PAPER: ")
        else:
            raise PaperPerpError("Se requiere --passphrase-stdin o una terminal interactiva")
        result = restore_secure_paper_perp(PaperPerpStore(Path(args.paper_db)),
                                           args.restore, args.confirm, passphrase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PaperPerpError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
