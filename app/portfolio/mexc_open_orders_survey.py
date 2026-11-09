"""Sonda MEXC sin par: detecta riesgos, nunca certifica cobertura global."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re

from app.portfolio.domain import PortfolioError


_SYMBOL = re.compile(r"[A-Z0-9]{3,30}\Z")


def summarize_open_orders(snapshot: dict, *, account_id: str,
                          now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    if (not isinstance(snapshot, dict)
            or snapshot.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or snapshot.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or snapshot.get("coverage") != "UNFILTERED_COVERAGE_UNPROVEN"
            or snapshot.get("symbol") is not None
            or not isinstance(snapshot.get("orders"), list)
            or len(snapshot["orders"]) >= 1000):
        raise PortfolioError("La sonda MEXC no tiene cobertura comprobable")
    try:
        observed_at = datetime.fromisoformat(snapshot["observed_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PortfolioError("La fecha de la sonda MEXC no es válida") from exc
    if (observed_at.tzinfo is None or current.tzinfo is None
            or not 0 <= (current - observed_at).total_seconds() <= 30):
        raise PortfolioError("La sonda MEXC ya no es reciente")
    symbols: Counter[str] = Counter()
    for order in snapshot["orders"]:
        if (not isinstance(order, dict)
                or not isinstance(order.get("symbol"), str)
                or not _SYMBOL.fullmatch(order["symbol"])
                or order.get("status") not in ("NEW", "PARTIALLY_FILLED")):
            raise PortfolioError("La sonda MEXC contiene una orden inválida")
        symbols[order["symbol"]] += 1
    return {
        "account_id": account_id,
        "source": "MEXC_SPOT_PRIVATE_READONLY",
        "observed_at": observed_at.isoformat(),
        "coverage": "UNFILTERED_COVERAGE_UNPROVEN",
        "observed_open_orders": len(snapshot["orders"]),
        "symbols": [{"symbol": symbol, "count": count}
                    for symbol, count in sorted(symbols.items())],
        "status": "OPEN_ORDERS_OBSERVED" if symbols else "NO_ORDERS_SEEN_COVERAGE_UNPROVEN",
        "reconciled": False,
        "orders_enabled": False,
    }
