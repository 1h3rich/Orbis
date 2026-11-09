#!/usr/bin/env python3
"""Cifra Spot PAPER con Orbis cerrado y copia SQLCipher verificada."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automation.spot_paper_access import SpotPaperAccessError  # noqa: E402
from app.automation.spot_paper_migration import encrypt_active_spot_paper  # noqa: E402
from app.automation.spot_strategy_paper import SpotStrategyPaper  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-db", default=os.environ.get(
        "ORBIS_SPOT_STRATEGY_PATH", "~/.local/share/orbis/spot-strategy-paper.db"))
    parser.add_argument("--lab-db", default=os.environ.get(
        "ORBIS_LAB_PATH", "~/.local/share/orbis/lab.db"))
    parser.add_argument("--confirm", default="", help="Escribir exactamente CIFRAR SPOT PAPER")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="Leer contraseña y confirmación (dos líneas) desde stdin")
    args = parser.parse_args(argv)
    try:
        if args.confirm != "CIFRAR SPOT PAPER":
            raise SpotPaperAccessError("Debes confirmar exactamente CIFRAR SPOT PAPER")
        if args.passphrase_stdin:
            passphrase = sys.stdin.readline().rstrip("\r\n")
            repeated = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            passphrase = getpass.getpass("Nueva contraseña Spot PAPER: ")
            repeated = getpass.getpass("Repetir contraseña Spot PAPER: ")
        else:
            raise SpotPaperAccessError("Se requiere --passphrase-stdin o una terminal interactiva")
        if passphrase != repeated:
            raise SpotPaperAccessError("Las dos contraseñas Spot PAPER no coinciden")
        result = encrypt_active_spot_paper(
            SpotStrategyPaper(Path(args.paper_db), Path(args.lab_db)),
            args.confirm, passphrase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SpotPaperAccessError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
