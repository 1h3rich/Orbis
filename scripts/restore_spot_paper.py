#!/usr/bin/env python3
"""Restaura Spot PAPER SQLCipher con Orbis cerrado y preimagen cifrada."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automation.spot_paper_access import SpotPaperAccessError  # noqa: E402
from app.automation.spot_paper_migration import restore_secure_spot_paper  # noqa: E402
from app.automation.spot_strategy_paper import SpotStrategyPaper  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-db", default=os.environ.get(
        "ORBIS_SPOT_STRATEGY_PATH", "~/.local/share/orbis/spot-strategy-paper.db"))
    parser.add_argument("--lab-db", default=os.environ.get(
        "ORBIS_LAB_PATH", "~/.local/share/orbis/lab.db"))
    parser.add_argument("--restore", required=True, help="Nombre de copia gestionada")
    parser.add_argument("--confirm", default="", help="Escribir exactamente RESTAURAR SPOT PAPER")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="Leer contraseña desde stdin")
    args = parser.parse_args(argv)
    try:
        if args.confirm != "RESTAURAR SPOT PAPER":
            raise SpotPaperAccessError("Debes confirmar exactamente RESTAURAR SPOT PAPER")
        if args.passphrase_stdin:
            passphrase = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            passphrase = getpass.getpass("Contraseña de copia Spot PAPER: ")
        else:
            raise SpotPaperAccessError("Se requiere --passphrase-stdin o una terminal interactiva")
        result = restore_secure_spot_paper(
            SpotStrategyPaper(Path(args.paper_db), Path(args.lab_db)),
            args.restore, args.confirm, passphrase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SpotPaperAccessError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
