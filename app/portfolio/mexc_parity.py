"""Doble comprobación de cantidades MEXC; condición necesaria, no conciliación."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.portfolio.domain import AccountSnapshot, PortfolioError
from app.portfolio.reconciliation import compare_mexc_spot_inventory


def assess_mexc_spot_parity(
    book: AccountSnapshot,
    first: dict,
    second: dict,
    *,
    book_revision_before: str,
    book_revision_after: str,
    now: datetime | None = None,
) -> dict:
    """Compara dos lecturas completas y el mismo estado del libro.

    Una coincidencia de cantidades no cubre órdenes en tránsito, fills,
    depósitos o retiros externos; jamás autoriza ejecución.
    """
    checked_at = now or datetime.now(timezone.utc)
    before = compare_mexc_spot_inventory(book, first, now=checked_at)
    after = compare_mexc_spot_inventory(book, second, now=checked_at)
    first_at = datetime.fromisoformat(before["observed_at"])
    second_at = datetime.fromisoformat(after["observed_at"])
    if second_at < first_at:
        raise PortfolioError("La segunda lectura MEXC retrocedió en el tiempo")

    def balances(source: dict) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
        return {row["asset"]: (Decimal(row["free"]), Decimal(row["locked"]),
                                Decimal(row["total"])) for row in source["balances"]}

    first_balances, second_balances = balances(first), balances(second)
    book_stable = book_revision_before == book_revision_after
    venue_stable = first_balances == second_balances
    blockers = []
    if not book_stable:
        blockers.append("BOOK_CHANGED_DURING_CHECK")
    if not venue_stable:
        blockers.append("MEXC_CHANGED_DURING_CHECK")
    if not after["assets"]:
        blockers.append("NO_UNITS_TO_COMPARE")
    if after["negative_book_cash_assets"]:
        blockers.append("NEGATIVE_BOOK_CASH")
    if not after["all_quantities_equal"]:
        blockers.append("QUANTITY_DIFFERENCE")
    locked_assets = [{"asset": asset, "locked": str(locked)}
                     for asset, (_, locked, _) in sorted(second_balances.items()) if locked > 0]
    if locked_assets:
        blockers.append("MEXC_LOCKED_FUNDS")
    return {
        "account_id": book.account_id,
        "source": "MEXC_SPOT_PRIVATE_READONLY",
        "first_observed_at": before["observed_at"],
        "second_observed_at": after["observed_at"],
        "checked_at": checked_at.isoformat(),
        "book_revision": book_revision_after,
        "book_stable": book_stable,
        "mexc_balances_stable": venue_stable,
        "all_quantities_equal": after["all_quantities_equal"],
        "negative_book_cash_assets": after["negative_book_cash_assets"],
        "assets": after["assets"],
        "locked_assets": locked_assets,
        "blockers": blockers,
        "status": "PARITY_OBSERVED_NOT_RECONCILED" if not blockers else "BLOCKED",
        "reconciled": False,
        "orders_enabled": False,
        "coverage": "TWO_SPOT_BALANCE_READS_ONLY",
        "external_orders_or_transfers_excluded": True,
    }
