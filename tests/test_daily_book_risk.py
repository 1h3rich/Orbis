from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.portfolio.daily_book_risk import calculate_daily_book_risk
from app.portfolio.domain import EventInput, PortfolioError, PortfolioEvent


ACCOUNT = str(uuid4())
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def event(kind, at, *, quote="USDT", asset=None, qty=None, price=None,
          amount=None, fee="0"):
    item = EventInput(kind=kind, quote_currency=quote, occurred_at=at,
                      asset=asset, quantity=Decimal(qty) if qty else None,
                      unit_price=Decimal(price) if price else None,
                      amount=Decimal(amount) if amount else None,
                      fee=Decimal(fee))
    return PortfolioEvent(str(uuid4()), ACCOUNT, str(uuid4()), at, item)


def test_daily_risk_replays_cost_basis_and_excludes_cash_flows():
    yesterday = NOW - timedelta(days=1)
    rows = [
        event("CONTRIBUTION", yesterday - timedelta(minutes=2), amount="1000"),
        event("BUY", yesterday - timedelta(minutes=1), asset="BTC", qty="2", price="100"),
        event("SELL", yesterday, asset="BTC", qty="1", price="120"),
        event("CONTRIBUTION", NOW, amount="500"),
        event("WITHDRAWAL", NOW, amount="50"),
        event("CASH_TRANSFER_OUT", NOW, amount="20"),
        event("BUY", NOW - timedelta(minutes=2), asset="BTC", qty="1", price="200"),
        event("SELL", NOW - timedelta(minutes=1), asset="BTC", qty="1", price="180"),
    ]
    risk = calculate_daily_book_risk(ACCOUNT, rows, "USDT", now=NOW, base_asset="BTC")
    assert risk.day_start == datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert risk.trade_count == 2
    assert risk.turnover_quote == Decimal("380")
    assert risk.realized_pnl_quote == Decimal("30")
    assert risk.base_asset == "BTC"
    assert risk.position_quantity_base == Decimal("1")
    assert risk.position_cost_basis_quote == Decimal("150")
    assert risk.average_cost_quote_per_base == Decimal("150")
    assert risk.source == "REAL_BOOK_ONLY"
    assert risk.reconciled_with_venue is False and risk.execution_ready is False


def test_daily_risk_uses_utc_day_ignores_future_and_keeps_quote_separate():
    prior = datetime(2026, 10, 17, 23, 59, tzinfo=timezone.utc)
    rows = [event("CONTRIBUTION", prior, amount="500"),
            event("BUY", prior, asset="BTC", qty="1", price="100"),
            event("SELL", NOW, asset="BTC", qty="0.5", price="80", fee="1"),
            event("BUY", NOW + timedelta(seconds=1), asset="ETH", qty="1", price="999"),
            event("CONTRIBUTION", NOW, quote="EUR", amount="20"),
            event("BUY", NOW, quote="EUR", asset="SOL", qty="1", price="10")]
    usdt = calculate_daily_book_risk(ACCOUNT, rows, "USDT", now=NOW)
    eur = calculate_daily_book_risk(ACCOUNT, rows, "EUR", now=NOW)
    assert usdt.trade_count == 1 and usdt.turnover_quote == Decimal("40")
    assert usdt.realized_pnl_quote == Decimal("-11")
    assert eur.trade_count == 1 and eur.turnover_quote == Decimal("10")
    assert eur.realized_pnl_quote == 0


def test_daily_risk_rejects_mixed_accounts_or_invalid_book():
    row = event("BUY", NOW, asset="BTC", qty="1", price="100")
    foreign = PortfolioEvent(row.id, str(uuid4()), row.request_id, row.recorded_at, row.input)
    with pytest.raises(PortfolioError, match="otra cuenta"):
        calculate_daily_book_risk(ACCOUNT, [foreign], "USDT", now=NOW)
    with pytest.raises(PortfolioError, match="inválida"):
        calculate_daily_book_risk(ACCOUNT, [row], "usdt", now=NOW)
    with pytest.raises(PortfolioError, match="timezone"):
        calculate_daily_book_risk(ACCOUNT, [row], "USDT", now=NOW.replace(tzinfo=None))
    with pytest.raises(PortfolioError, match="Activo base"):
        calculate_daily_book_risk(ACCOUNT, [row], "USDT", now=NOW, base_asset="USDT")


def test_daily_book_basis_is_zero_without_position_and_never_fills_from_venue():
    risk = calculate_daily_book_risk(ACCOUNT, [], "USDT", now=NOW, base_asset="BTC")
    assert risk.position_quantity_base == 0
    assert risk.position_cost_basis_quote == 0
    assert risk.average_cost_quote_per_base is None
    assert risk.reconciled_with_venue is False and risk.execution_ready is False


def test_daily_book_distinguishes_zero_cost_inventory_from_no_inventory():
    zero_cost = EventInput(kind="ADJUSTMENT_IN", quote_currency="USDT", occurred_at=NOW,
                           asset="BTC", quantity=Decimal("1"), unit_price=Decimal("0"),
                           note="Ajuste manual sin coste")
    row = PortfolioEvent(str(uuid4()), ACCOUNT, str(uuid4()), NOW, zero_cost)
    held = calculate_daily_book_risk(ACCOUNT, [row], "USDT", now=NOW, base_asset="BTC")
    assert held.position_quantity_base == Decimal("1")
    assert held.average_cost_quote_per_base == Decimal("0")

    sold_at = NOW + timedelta(seconds=1)
    sold = event("SELL", sold_at, asset="BTC", qty="1", price="1")
    empty = calculate_daily_book_risk(ACCOUNT, [row, sold], "USDT",
                                      now=sold_at, base_asset="BTC")
    assert empty.position_quantity_base == Decimal("0")
    assert empty.average_cost_quote_per_base is None
