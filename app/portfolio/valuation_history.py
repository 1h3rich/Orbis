"""Daily portfolio reconstruction from the current effective local ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext

from app.marketdata.cache import TF_SECONDS
from app.marketdata.adapters.mexc import MEXC_HISTORICAL_PAGE_SIZE
from app.marketdata.normalize import canonical_symbol
from app.marketdata.service import candles
from app.portfolio.domain import (
    CALC_PRECISION,
    AccountSnapshot,
    PortfolioError,
    PortfolioEvent,
    apply_portfolio_event,
    decimal_value,
    realized_pnl_by_currency,
)

DAY = TF_SECONDS["1d"]
MAX_PAIRS = 24
MAX_VALUATION_DAYS = 3660
_PAGE_DAYS = {"bitstamp": 1000, "binance": 1000, "mexc": MEXC_HISTORICAL_PAGE_SIZE}
ZERO = Decimal(0)


@dataclass(frozen=True)
class DailyBook:
    start_t: int
    cash: Decimal
    realized_pnl: Decimal
    contributions: Decimal
    withdrawals: Decimal
    positions: tuple[tuple[str, Decimal, Decimal], ...]


def _daily_book(
    account_id: str, events: list[PortfolioEvent], currency: str, start_t: int, end_t: int,
) -> list[DailyBook]:
    ordered = iter(sorted(
        (event for event in events if event.financial_input.quote_currency == currency),
        key=lambda event: event.sort_key,
    ))
    book = AccountSnapshot(account_id)
    event = next(ordered, None)
    start_at = datetime.fromtimestamp(start_t, timezone.utc)
    result = []
    with localcontext() as context:
        context.prec = CALC_PRECISION
        if any(item.accounting is not None for item in events):
            context.rounding = ROUND_HALF_EVEN
        for day_t in range(start_t, end_t, DAY):
            close_at = datetime.fromtimestamp(day_t + DAY, timezone.utc)
            contributions = withdrawals = ZERO
            while event is not None and event.occurred_at < close_at:
                item = event.financial_input
                apply_portfolio_event(book, event)
                if event.occurred_at >= start_at:
                    if item.kind == "CONTRIBUTION":
                        contributions += item.amount
                    elif item.kind == "WITHDRAWAL":
                        withdrawals += item.amount
                event = next(ordered, None)
            if currency not in book.cash:
                continue
            result.append(DailyBook(
                day_t, book.cash[currency], realized_pnl_by_currency(book).get(currency, ZERO),
                contributions, withdrawals,
                tuple((position.asset, position.quantity, position.cost_basis)
                      for position in book.positions.values() if position.quantity),
            ))
    return result


def _close_page(symbol: str, provider: str, start_t: int, end_t: int, *, older: bool = False) -> dict[int, Decimal]:
    days = (end_t - start_t) // DAY
    # The previous-window endpoint excludes the right edge. The latest endpoint
    # can include the still-open bucket, so leave room for it.
    options = {"before": end_t, "limit": days} if older else {"since": start_t, "limit": days + 1}
    try:
        series = candles(symbol, provider=provider, timeframe="1d", **options)
    except ValueError:
        return {}
    if (series.get("available") is not True or series.get("symbol") != symbol
            or series.get("resolved_provider") != provider or series.get("timeframe") != "1d"):
        return {}

    closes: dict[int, Decimal] = {}
    for row in series["candles"]:
        stamp = row["t"]
        if (type(stamp) is not int or not start_t <= stamp < end_t
                or stamp % DAY != 0 or row.get("closed") is not True):
            continue
        try:
            price = decimal_value(Decimal(row["c"]), positive=True)
        except (InvalidOperation, TypeError, PortfolioError):
            continue
        if stamp in closes and closes[stamp] != price:
            return {}
        closes[stamp] = price
    return closes


def _daily_closes(symbol: str, provider: str, start_t: int, end_t: int, *, paged: bool) -> dict[int, Decimal]:
    size = _PAGE_DAYS.get(provider)
    if size is None or not paged:
        return _close_page(symbol, provider, start_t, end_t)
    closes = {}
    for right in range(end_t, start_t, -size * DAY):
        left = max(start_t, right - size * DAY)
        closes.update(_close_page(symbol, provider, left, right, older=True))
    return closes


def value_history(
    account_id: str, events: list[PortfolioEvent], *, currency: str, provider: str, days: int, now: datetime,
) -> dict:
    if type(days) is not int or not 7 <= days <= MAX_VALUATION_DAYS:
        raise PortfolioError("Periodo de valoración diario fuera de rango")
    end_t = int(now.timestamp()) // DAY * DAY
    start_t = end_t - days * DAY
    daily = _daily_book(account_id, events, currency, start_t, end_t)
    assets = sorted({asset for day in daily for asset, _, _ in day.positions})
    if len(assets) > MAX_PAIRS:
        raise PortfolioError(f"El periodo supera {MAX_PAIRS} pares. Elige un periodo más corto.")

    # For multi-year requests, avoid downloading years outside each holding's
    # first and last day. The shorter query keeps its existing UTC cache window.
    coverage = {}
    if days > 365:
        for day in daily:
            for asset, _, _ in day.positions:
                first, _ = coverage.get(asset, (day.start_t, day.start_t))
                coverage[asset] = (first, day.start_t + DAY)

    def load(asset: str) -> dict[int, Decimal]:
        first, last = coverage.get(asset, (start_t, end_t))
        return _daily_closes(canonical_symbol(asset, currency), provider, first, last, paged=days > 365)

    quotes = {}
    if assets:
        with ThreadPoolExecutor(max_workers=min(4, len(assets))) as pool:
            quotes = dict(zip(assets, pool.map(load, assets), strict=True))

    rows = []
    with localcontext() as context:
        context.prec = CALC_PRECISION
        for day in daily:
            missing = []
            market_value = basis = ZERO
            for asset, quantity, cost in day.positions:
                price = quotes[asset].get(day.start_t)
                if price is None:
                    missing.append(asset)
                else:
                    market_value += price * quantity
                    basis += cost
            complete = not missing and day.cash >= ZERO
            unrealized = market_value - basis if complete else None
            rows.append({
                "date_t": day.start_t,
                "cash": day.cash,
                "equity": day.cash + market_value if complete else None,
                "realized_pnl": day.realized_pnl,
                "unrealized_pnl": unrealized,
                "total_pnl": day.realized_pnl + unrealized if complete else None,
                "contributions": day.contributions,
                "withdrawals": day.withdrawals,
                "negative_cash": day.cash < ZERO,
                "missing_assets": sorted(missing),
            })
    return {
        "account_id": account_id, "currency": currency, "provider": provider,
        "timeframe": "1d", "days": days, "as_of": now, "end_t": end_t,
        "basis": "current_effective_ledger", "rows": rows,
    }
