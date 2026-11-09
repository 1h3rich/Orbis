"""Lectura estricta del CSV REAL v1/v2/v3; v3 incorpora gastos FEE independientes."""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from io import StringIO
import re
from uuid import UUID

from app.portfolio.domain import EventInput, PortfolioError


COLUMNS_V1 = (
    "schema_version", "environment", "account_id", "event_id", "request_id",
    "kind", "asset", "quote_currency", "quantity", "unit_price", "amount",
    "gross_amount", "fee", "note", "occurred_at", "recorded_at",
)
COLUMNS = COLUMNS_V1[:13] + ("fee_asset",) + COLUMNS_V1[13:]
MAX_CSV_CHARS = 2_000_000
MAX_EVENTS = 10_000


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PortfolioError("Identificador CSV inválido") from exc


def _number(value: str, *, optional: bool = False) -> Decimal | None:
    if optional and value == "":
        return None
    if not value or len(value) > 100:
        raise PortfolioError("Decimal CSV inválido")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise PortfolioError("Decimal CSV inválido") from exc
    if not result.is_finite():
        raise PortfolioError("Decimal CSV no finito")
    return result


def parse_real_events_csv(content: str) -> tuple[str, list[tuple[str, EventInput]]]:
    """Devuelve cuenta de origen e inputs validados; rechaza CSV parcial/ajeno.

    `event_id` y `recorded_at` son metadatos de auditoría de origen; el
    destino genera nuevos IDs y fecha de registro. `request_id` se conserva
    para que un segundo intento sea idempotente dentro de la cuenta destino.
    """
    if not isinstance(content, str) or not content or len(content) > MAX_CSV_CHARS:
        raise PortfolioError("CSV REAL vacío o superior a 2 millones de caracteres")
    if "\x00" in content:
        raise PortfolioError("CSV REAL contiene datos binarios")
    try:
        reader = csv.reader(StringIO(content.lstrip("\ufeff"), newline=""), strict=True)
        header = next(reader, None)
        if header == list(COLUMNS):
            columns, version = COLUMNS, None
        elif header == list(COLUMNS_V1):
            columns, version = COLUMNS_V1, "1"
        else:
            raise PortfolioError("Cabecera CSV REAL incompatible")
        items: list[tuple[str, EventInput]] = []
        source_account: str | None = None
        for line, values in enumerate(reader, start=2):
            if line > MAX_EVENTS + 1:
                raise PortfolioError("CSV REAL supera 10.000 movimientos")
            if len(values) != len(columns):
                raise PortfolioError(f"Fila {line}: columnas incompletas o adicionales")
            row = dict(zip(columns, values))
            try:
                if version is None:
                    version = row["schema_version"] if row["schema_version"] in {"2", "3"} else "unsupported"
                if row["schema_version"] != version or row["environment"] != "REAL":
                    raise PortfolioError("Origen o versión no compatible")
                account = _uuid(row["account_id"])
                _uuid(row["event_id"])
                request_id = _uuid(row["request_id"])
                if source_account is None:
                    source_account = account
                elif source_account != account:
                    raise PortfolioError("El CSV mezcla cuentas de origen")
                recorded = datetime.fromisoformat(row["recorded_at"])
                if recorded.tzinfo is None:
                    raise PortfolioError("Fecha de registro sin zona horaria")
                note = row["note"] or None
                if note and re.match(r"^'[\s\x00-\x1f]*[=+\-@]", note):
                    note = note[1:]
                kind = row["kind"]
                if kind == "FEE" and version != "3":
                    raise PortfolioError("Las comisiones independientes requieren CSV REAL v3")
                gross = _number(row["gross_amount"])
                quantity = _number(row["quantity"], optional=True)
                unit_price = _number(row["unit_price"], optional=True)
                gross_override = None
                if kind in ("BUY", "SELL") and quantity is not None and unit_price is not None:
                    with localcontext() as context:
                        context.prec = 100
                        if gross != quantity * unit_price:
                            gross_override = gross
                item = EventInput(
                    kind=kind,
                    quote_currency=row["quote_currency"],
                    occurred_at=datetime.fromisoformat(row["occurred_at"]),
                    asset=row["asset"] or None,
                    quantity=quantity,
                    unit_price=unit_price,
                    amount=_number(row["amount"], optional=True),
                    fee=_number(row["fee"]),
                    fee_asset=row.get("fee_asset") or None,
                    note=note,
                    gross_override=gross_override,
                )
                if item.gross_amount != gross:
                    raise PortfolioError("Importe bruto no coincide con cantidad y precio")
                items.append((request_id, item))
            except (PortfolioError, ValueError, TypeError) as exc:
                raise PortfolioError(f"Fila {line}: {exc}") from exc
    except csv.Error as exc:
        raise PortfolioError("CSV REAL malformado") from exc
    if not items or source_account is None:
        raise PortfolioError("CSV REAL sin movimientos")
    return source_account, items
