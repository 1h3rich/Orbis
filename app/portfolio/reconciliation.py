"""Comparación diagnóstica entre inventario manual y una lectura Spot externa.

No vincula identidades de cuentas, no atribuye transferencias ni crea eventos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from app.portfolio.domain import AccountSnapshot, PortfolioError, decimal_value


def compare_mexc_spot_inventory(snapshot: AccountSnapshot, observed: dict,
                                *, now: datetime | None = None) -> dict:
    if (not isinstance(observed, dict)
            or observed.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or observed.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or not isinstance(observed.get("balances"), list)):
        raise PortfolioError("Lectura MEXC Spot no verificable")
    try:
        observed_at = datetime.fromisoformat(observed["observed_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PortfolioError("Lectura MEXC sin fecha verificable") from exc
    if observed_at.tzinfo is None:
        raise PortfolioError("Lectura MEXC sin zona horaria")
    current = now or datetime.now(timezone.utc)
    age = (current - observed_at.astimezone(timezone.utc)).total_seconds()
    if not -2 <= age <= 60:
        raise PortfolioError("Lectura MEXC obsoleta o futura")

    manual: dict[str, Decimal] = {}
    negative_cash: list[str] = []
    with localcontext() as context:
        context.prec = 300
        for asset, amount in snapshot.cash.items():
            decimal_value(amount)
            manual[asset] = manual.get(asset, Decimal(0)) + amount
            if amount < 0:
                negative_cash.append(asset)
        for position in snapshot.positions.values():
            decimal_value(position.quantity)
            manual[position.asset] = manual.get(position.asset, Decimal(0)) + position.quantity

        venue: dict[str, Decimal] = {}
        for row in observed["balances"]:
            if not isinstance(row, dict) or not isinstance(row.get("asset"), str):
                raise PortfolioError("Saldo MEXC malformado")
            asset = row["asset"]
            if (not 2 <= len(asset) <= 24 or not asset.isascii() or not asset.isalnum()
                    or asset != asset.upper() or asset in venue):
                raise PortfolioError("Activo MEXC inválido o duplicado")
            try:
                free, locked, total = (Decimal(row[key]) for key in ("free", "locked", "total"))
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise PortfolioError("Saldo MEXC no decimal") from exc
            for value in (free, locked, total):
                decimal_value(value)
                if value < 0:
                    raise PortfolioError("Saldo MEXC negativo")
            if free + locked != total:
                raise PortfolioError("Saldo MEXC no coincide con libre más bloqueado")
            venue[asset] = total

        rows = []
        for asset in sorted(set(manual) | set(venue)):
            book = manual.get(asset, Decimal(0))
            external = venue.get(asset, Decimal(0))
            difference = external - book
            if difference == 0:
                difference = Decimal(0)
            if book == 0 and external == 0:
                continue
            rows.append({"asset": asset, "book_quantity": str(book),
                         "mexc_spot_quantity": str(external),
                         "difference_mexc_minus_book": str(difference),
                         "quantity_equal": difference == 0})
    return {"account_id": snapshot.account_id,
            "source": "MEXC_SPOT_PRIVATE_READONLY",
            "observed_at": observed_at.isoformat(),
            "comparison": "DIAGNOSTIC_ONLY_NOT_RECONCILED",
            "account_venue_binding_verified": False,
            "negative_book_cash_assets": sorted(negative_cash),
            "all_quantities_equal": all(row["quantity_equal"] for row in rows),
            "assets": rows}
