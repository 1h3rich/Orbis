#!/usr/bin/env python3
"""Comprueba que el esquema de la base de datos coincide con los modelos.

Compara ``Base.metadata`` (lo que la aplicación espera) con
``inspect(engine)`` (lo que hay realmente). Si faltan tablas, columnas,
índices o restricciones, sale con código 1 listando cada ausencia y el DDL
manual que hay que ejecutar: ``Base.metadata.create_all`` no modifica tablas
ya existentes y en v1 no se usa Alembic.

    python scripts/check_schema.py               # orbis.db (o $ORBIS_DATABASE_URL)
    python scripts/check_schema.py --url sqlite:///otra.db

Sale con 0 si todo cuadra, 1 si hay cambios pendientes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.database.schema_check import schema_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("ORBIS_DATABASE_URL", "sqlite:///./orbis.db"),
        help="URL SQLAlchemy de la base a comprobar (por defecto orbis.db)",
    )
    args = parser.parse_args(argv)

    engine = create_engine(args.url)
    try:
        pending, notes = schema_report(engine)
    finally:
        engine.dispose()

    if not pending:
        print(f"Esquema correcto: {args.url} coincide con los modelos.")
        return 0

    print(f"CAMBIOS PENDIENTES ({len(pending)}) en {args.url}:")
    for entry in pending:
        print(f"  - {entry}")

    if notes:
        print("\nDDL manual documentado (create_all es no-op sobre tablas existentes):")
        for note in notes:
            print(f"  {note}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
