"""Comparación entre el esquema esperado por los modelos y el real de la BD.

Se adopta en lugar de Alembic en v1: la base local está vacía en las tablas
operativas (operations, paper_scenarios, paper_requests), así que migrar
aportaría coste sin ganancia. Lo que sí hace falta es **detectar** los cambios
que `Base.metadata.create_all` no aplica a tablas ya creadas (create_all es
no-op sobre lo existente), y documentar el DDL manual que hay que ejecutar.

Uso desde la CLI: ``python scripts/check_schema.py``.
Se invoca al arrancar la aplicación y al inicio de la suite de pruebas.
"""

from __future__ import annotations

import logging

from sqlalchemy import Inspector, inspect
from sqlalchemy.schema import Index, UniqueConstraint

# Importar los modelos registra todas las tablas en `Base.metadata`; sin esto
# la comparación vería un metadato vacío y daría un falso "esquema correcto".
import app.database.models  # noqa: F401,E402
from app.database.database import Base  # noqa: E402

logger = logging.getLogger("orbis.schema")

#: DDL manual documentado para los cambios pendientes conocidos de v1.
#: Clave: tabla afectada; valor: sentencias a ejecutar a mano sobre orbis.db.
SCHEMA_DDL_HINTS: dict[str, list[str]] = {
    "paper_scenarios": [
        "-- Sesiones PAPER (create_all no altera tablas existentes):",
        "ALTER TABLE paper_scenarios ADD COLUMN generation INTEGER NOT NULL DEFAULT 1;",
        "ALTER TABLE paper_scenarios ADD COLUMN closed_at DATETIME;",
        "-- La UNIQUE (quote_currency) antigua impide abrir una segunda sesión",
        "-- por moneda; en SQLite hay que recrear la tabla sin esa UNIQUE",
        "-- (copy de datos + drop + create) y aplicar después el índice parcial:",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_scenarios_open_quote "
        "ON paper_scenarios (quote_currency) WHERE closed_at IS NULL;",
    ],
    "price_quotes": [
        "-- Cotizaciones manuales para valoración (nueva tabla):",
        "CREATE TABLE IF NOT EXISTS price_quotes (",
        "    id INTEGER NOT NULL PRIMARY KEY,",
        "    asset VARCHAR NOT NULL,",
        "    quote_currency VARCHAR(12) NOT NULL,",
        "    price NUMERIC(30, 12) NOT NULL,",
        "    source VARCHAR NOT NULL,",
        "    updated_at DATETIME NOT NULL,",
        "    UNIQUE (asset, quote_currency)",
        ");",
    ],
    "ui_market_state": [
        "-- Estado del Estudio por mercado+provider (dibujos/indicadores/config):",
        "CREATE TABLE IF NOT EXISTS ui_market_state (",
        "    id INTEGER NOT NULL PRIMARY KEY,",
        "    market VARCHAR(24) NOT NULL,",
        "    provider VARCHAR(24) NOT NULL DEFAULT 'kraken',",
        "    timeframe VARCHAR(8) NOT NULL DEFAULT '1h',",
        "    kind VARCHAR(24) NOT NULL DEFAULT 'drawings',",
        "    payload JSON NOT NULL DEFAULT '{}',",
        "    updated_at DATETIME NOT NULL",
        ");",
        "CREATE INDEX IF NOT EXISTS ix_ui_market_state_market ON ui_market_state (market, provider, timeframe, kind);",
    ],
    "ui_watchlist": [
        "-- Watchlist persistente del usuario:",
        "CREATE TABLE IF NOT EXISTS ui_watchlist (",
        "    id INTEGER NOT NULL PRIMARY KEY,",
        "    market VARCHAR(24) NOT NULL,",
        "    provider VARCHAR(24) NOT NULL DEFAULT 'kraken',",
        "    position INTEGER NOT NULL DEFAULT 0,",
        "    created_at DATETIME NOT NULL",
        ");",
    ],
    "ui_preferences": [
        "-- Preferencias UI por clave:",
        "CREATE TABLE IF NOT EXISTS ui_preferences (",
        "    key VARCHAR(64) NOT NULL PRIMARY KEY,",
        "    value TEXT NOT NULL DEFAULT '',",
        "    updated_at DATETIME NOT NULL",
        ");",
    ],
}

LEGACY_UNIQUE_NOTE = (
    "La restricción UNIQUE (quote_currency) de paper_scenarios es anterior al "
    "soporte de sesiones múltiples: hay que eliminarla para poder abrir la "
    "segunda sesión de una moneda."
)


def _normalize_type(value: str) -> str:
    """Compara tipos ignorando mayúsculas y espacios: ``NUMERIC(30, 12)``."""
    return "".join(str(value).upper().split())


def _column_diff(table, inspector: Inspector) -> list[str]:
    pending: list[str] = []
    db_columns = {column["name"]: column for column in inspector.get_columns(table.name)}

    for column in table.columns:
        found = db_columns.get(column.name)
        if found is None:
            pending.append(
                f"tabla {table.name}: falta la columna {column.name}"
            )
            continue

        expected_type = _normalize_type(column.type.compile(dialect=inspector.bind.dialect))
        found_type = _normalize_type(found["type"])
        if expected_type != found_type:
            pending.append(
                f"tabla {table.name}.{column.name}: tipo esperado "
                f"{expected_type}, en BD {found_type}"
            )
        if bool(column.nullable) != bool(found["nullable"]):
            pending.append(
                f"tabla {table.name}.{column.name}: nullable esperado "
                f"{bool(column.nullable)}, en BD {bool(found['nullable'])}"
            )

    return pending


def _constraint_diff(table, inspector: Inspector) -> list[str]:
    pending: list[str] = []

    expected_uniques = {
        frozenset(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    found_uniques = {
        frozenset(entry["column_names"])
        for entry in inspector.get_unique_constraints(table.name)
        if entry.get("column_names")
    }
    for missing in sorted(expected_uniques - found_uniques, key=sorted):
        pending.append(
            f"tabla {table.name}: falta la restricción UNIQUE ({', '.join(sorted(missing))})"
        )
    for stale in sorted(found_uniques - expected_uniques, key=sorted):
        pending.append(
            f"tabla {table.name}: queda la restricción UNIQUE antigua "
            f"({', '.join(sorted(stale))}) que hay que eliminar"
        )

    expected_indexes = {
        index.name: bool(index.unique)
        for index in table.indexes
        if isinstance(index, Index) and index.name
    }
    found_indexes = {
        entry["name"]: bool(entry["unique"])
        for entry in inspector.get_indexes(table.name)
        if entry.get("name")
    }
    for name, unique in sorted(expected_indexes.items()):
        if name not in found_indexes:
            pending.append(
                f"tabla {table.name}: falta el índice {name}"
                + (" único" if unique else "")
            )
        elif found_indexes[name] != unique:
            pending.append(
                f"tabla {table.name}: el índice {name} debería ser "
                + ("único" if unique else "no único")
            )
    for name, unique in sorted(found_indexes.items()):
        if name not in expected_indexes:
            pending.append(
                f"tabla {table.name}: el índice {name} existe en BD pero no en los "
                "modelos (¿cambio manual pendiente?)"
            )

    return pending


def collect_schema_diff(engine) -> list[str]:
    """
    Devuelve la lista de cambios de esquema pendientes (vacía = todo cuadra).

    Compara ``Base.metadata`` (lo que la aplicación espera) con
    ``inspect(engine)`` (lo que hay realmente en la base de datos).
    """
    inspector = inspect(engine)
    pending: list[str] = []

    db_tables = set(inspector.get_table_names())
    for name in sorted(Base.metadata.tables):
        if name not in db_tables:
            pending.append(f"falta la tabla {name}")
            continue
        table = Base.metadata.tables[name]
        pending.extend(_column_diff(table, inspector))
        pending.extend(_constraint_diff(table, inspector))

    return pending


def schema_report(engine) -> tuple[list[str], list[str]]:
    """
    Devuelve ``(cambios pendientes, documentación DDL + avisos)``.

    Los cambios pendientes hacen fallar la comprobación; la documentación DDL
    explica cómo aplicarlos a mano, porque ``create_all`` es no-op sobre
    tablas ya creadas y en v1 no se usa Alembic.
    """
    pending = collect_schema_diff(engine)
    inspector = inspect(engine)
    known = set(Base.metadata.tables)

    notes: list[str] = []
    if pending:
        for table_name, statements in SCHEMA_DDL_HINTS.items():
            affected = any(
                entry == f"falta la tabla {table_name}"
                or entry.startswith(f"tabla {table_name}:")
                for entry in pending
            )
            if affected:
                notes.extend(statements)
        if any("UNIQUE antigua" in entry for entry in pending):
            notes.append(LEGACY_UNIQUE_NOTE)

    notes.extend(
        f"tabla ajena a los modelos: {name}"
        for name in sorted(set(inspector.get_table_names()) - known)
    )
    return pending, notes


def check_schema_or_log(engine) -> list[str]:
    """Se ejecuta al arrancar: no bloquea, pero deja constancia del DDL pendiente."""
    pending, notes = schema_report(engine)

    if pending:
        logger.warning(
            "Esquema de la base de datos desactualizado (%d cambios): ejecuta el "
            "DDL manual documentado en scripts/check_schema.py.\n%s",
            len(pending),
            "\n".join(f"  - {entry}" for entry in pending),
        )
    for note in notes:
        logger.info(note)

    return pending
