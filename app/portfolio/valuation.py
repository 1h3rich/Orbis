"""Valoración pura del libro REAL a partir de precios públicos identificados."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Mapping

from app.portfolio.domain import AccountSnapshot, CALC_PRECISION, PortfolioError, decimal_value, realized_pnl_by_currency


@dataclass(frozen=True)
class ReferencePrice:
    price: Decimal | None
    source: str
    as_of: str | None
    stale: bool


MAX_REFERENCE_AGE = timedelta(minutes=5)
MAX_FUTURE_SKEW = timedelta(seconds=30)


def _usable_reference(quote: ReferencePrice | None, now: datetime) -> bool:
    if quote is None or quote.stale or quote.price is None:
        return False
    if (not isinstance(quote.source, str) or not quote.source.strip()
            or quote.source.strip().lower() in ("unknown", "unavailable", "none")):
        return False
    if not isinstance(quote.as_of, str):
        return False
    try:
        observed_at = datetime.fromisoformat(quote.as_of)
        if observed_at.utcoffset() is None:
            return False
        if not now - MAX_REFERENCE_AGE <= observed_at <= now + MAX_FUTURE_SKEW:
            return False
        decimal_value(quote.price, positive=True)
    except (TypeError, ValueError, OverflowError, PortfolioError):
        return False
    return True


def _allocation(cash: Decimal, positions: list[tuple[str, Decimal]]) -> list[dict]:
    """Reparto exacto de 10.000 puntos básicos con desempate reproducible."""
    items = [("ASSET", asset, value) for asset, value in positions if value > 0]
    if cash > 0:
        items.append(("CASH", None, cash))
    if not items:
        return []
    items.sort(key=lambda item: (-item[2], item[0], item[1] or ""))
    values = [Fraction(item[2]) for item in items]
    total = sum(values)
    quotas = [value * 10000 / total for value in values]
    weights = [int(quota) for quota in quotas]
    remainder_order = sorted(range(len(items)), key=lambda index: quotas[index] - weights[index], reverse=True)
    for index in remainder_order[:10000 - sum(weights)]:
        weights[index] += 1
    return [{"kind": kind, "asset": asset, "value": value, "weight_bps": weight}
            for (kind, asset, value), weight in zip(items, weights)]


def value_snapshot(
    snapshot: AccountSnapshot,
    prices: Mapping[tuple[str, str], ReferencePrice],
    *, now: datetime | None = None,
) -> dict:
    """No suma divisas ni confunde aportaciones con beneficio.

    Un total se publica sólo si todas las posiciones abiertas de su divisa
    tienen precio válido y actual. Coste medio no sustituye un precio ausente.
    """
    by_currency: dict[str, dict] = {}
    positions: list[dict] = []
    amounts: dict[str, list[tuple[str, Decimal]]] = {}
    realized = realized_pnl_by_currency(snapshot)
    valuation_time = now or datetime.now(timezone.utc)
    if valuation_time.utcoffset() is None:
        raise PortfolioError("Valuation time must include a timezone")
    with localcontext() as context:
        context.prec = CALC_PRECISION
        for currency, cash in snapshot.cash.items():
            by_currency[currency] = {
                "currency": currency,
                "cash": cash,
                "market_value": Decimal("0"),
                "realized_pnl": realized.get(currency, Decimal("0")),
                "unrealized_pnl": Decimal("0"), "total_pnl": None,
                "equity": None,
                "complete": cash >= 0,
                "reason": None if cash >= 0 else "negative_cash_or_missing_contribution",
            }
        for key, position in snapshot.positions.items():
            asset, currency = key
            state = by_currency.setdefault(currency, {
                "currency": currency, "cash": Decimal("0"), "market_value": Decimal("0"),
                "realized_pnl": realized.get(currency, Decimal("0")), "equity": None,
                "unrealized_pnl": Decimal("0"), "total_pnl": None,
                "complete": True, "reason": None,
            })
            if position.quantity == 0:
                continue
            quote = prices.get(key)
            usable = _usable_reference(quote, valuation_time)
            value = position.quantity * quote.price if usable else None
            unrealized = value - position.cost_basis if value is not None else None
            if value is None:
                state["complete"] = False
                state["unrealized_pnl"] = None
                if state["reason"] is None:
                    state["reason"] = "missing_or_stale_price"
            else:
                state["market_value"] += value
                amounts.setdefault(currency, []).append((asset, value))
                if state["unrealized_pnl"] is not None:
                    state["unrealized_pnl"] += unrealized
            positions.append({
                "asset": asset,
                "quote_currency": currency,
                "quantity": position.quantity,
                "cost_basis": position.cost_basis,
                "realized_pnl": position.realized_pnl,
                "reference_price": quote.price if quote is not None else None,
                "price_source": quote.source if quote is not None else None,
                "price_as_of": quote.as_of if quote is not None else None,
                "price_stale": quote.stale if quote is not None else None,
                "market_value": value,
                "unrealized_pnl": unrealized,
            })
        for currency, state in by_currency.items():
            state["contributions"] = snapshot.contributions.get(currency, Decimal("0"))
            state["withdrawals"] = snapshot.withdrawals.get(currency, Decimal("0"))
            state["allocation"] = None
            if state["complete"]:
                state["equity"] = state["cash"] + state["market_value"]
                state["total_pnl"] = state["realized_pnl"] + state["unrealized_pnl"]
                state["allocation"] = _allocation(state["cash"], amounts.get(currency, []))
    return {"currencies": list(by_currency.values()), "positions": positions}
