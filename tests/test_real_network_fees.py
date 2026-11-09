"""Costes de red reales: gasto de unidades, sin retiradas ni conversiones inventadas."""

from datetime import datetime, timezone
from decimal import Decimal

from app.portfolio.domain import EventInput, PortfolioEvent, fold_events, realized_pnl_by_currency


NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def event(identifier, item):
    return PortfolioEvent(identifier, "wallet", identifier, NOW, item)


def test_native_cash_fee_reduces_balance_and_profit_not_contributed_or_withdrawn_capital():
    events = [event("1", EventInput("CONTRIBUTION", "BNB", NOW, amount=Decimal("0.1"))),
              event("2", EventInput("FEE", "BNB", NOW, amount=Decimal("0.000123456789012345"), note="Gas BNB"))]
    result = fold_events("wallet", events)
    assert result.cash["BNB"] == Decimal("0.099876543210987655")
    assert result.contributions == {"BNB": Decimal("0.1")} and result.withdrawals == {}
    assert realized_pnl_by_currency(result) == {"BNB": Decimal("-0.000123456789012345")}


def test_native_inventory_fee_consumes_units_at_book_cost_without_inventing_sale_or_fx():
    from app.portfolio.valuation import ReferencePrice, value_snapshot
    events = [event("1", EventInput("CONTRIBUTION", "USDT", NOW, amount=Decimal("1000"))),
              event("2", EventInput("BUY", "USDT", NOW, asset="BNB", quantity=Decimal("3"), unit_price=Decimal("300"))),
              event("3", EventInput("FEE", "USDT", NOW, asset="BNB", quantity=Decimal("0.0001"), note="Gas BNB"))]
    result = fold_events("wallet", events)
    position = result.positions[("BNB", "USDT")]
    assert position.quantity == Decimal("2.9999") and position.cost_basis == Decimal("899.97")
    assert position.realized_pnl == Decimal("-0.03") and position.fees_by_asset == {"BNB": Decimal("0.0001")}
    assert result.cash["USDT"] == Decimal("100") and result.withdrawals == {}
    values = value_snapshot(result, {("BNB", "USDT"): ReferencePrice(Decimal("350"), "TEST", NOW.isoformat(), False)}, now=NOW)
    assert values["currencies"][0]["equity"] == Decimal("1149.965")
    assert values["currencies"][0]["total_pnl"] == Decimal("149.965")


def test_cash_and_inventory_fees_survive_sqlcipher_reopen_and_verified_backup(tmp_path):
    from uuid import uuid4
    from app.portfolio.repository import append_event, create_account, account_snapshot, list_events
    from app.portfolio.vault import RealVault
    password = "isolated fee persistence password"
    vault = RealVault(tmp_path / "fees.db"); vault.unlock(password, create=True)
    with vault.session() as db:
        account = create_account(db, "Wallet BNB").id
    items = [EventInput("CONTRIBUTION", "BNB", NOW, amount=Decimal("0.1")),
             EventInput("FEE", "BNB", NOW, amount=Decimal("0.0001"), note="Gas en caja"),
             EventInput("ADJUSTMENT_IN", "USDT", NOW, asset="BNB", quantity=Decimal("1"), unit_price=Decimal("300"), note="Inventario previo"),
             EventInput("FEE", "USDT", NOW, asset="BNB", quantity=Decimal("0.0002"), note="Gas en inventario")]
    for item in items:
        with vault.session() as db:
            append_event(db, account, str(uuid4()), item)
    vault.backup(tmp_path / "backup.db"); vault.lock(); vault.unlock(password)
    with vault.session() as db:
        book = account_snapshot(db, account)
        assert [record.input for record in list_events(db, account)] == items
        assert book.cash["BNB"] == Decimal("0.0999")
        assert book.positions[("BNB", "USDT")].quantity == Decimal("0.9998")
    vault.lock()


def test_csv_v3_preserves_standalone_cash_and_asset_fees():
    import csv
    from io import StringIO
    from uuid import uuid4
    import pytest
    from app.portfolio.csv_import import COLUMNS, parse_real_events_csv
    from app.portfolio.domain import PortfolioError
    stream = StringIO(); writer = csv.writer(stream); writer.writerow(COLUMNS)
    account = str(uuid4())
    for asset, quantity, amount, quote in [("", "", "0.0001", "BNB"), ("BNB", "0.0002", "", "USDT")]:
        row = dict(schema_version="3", environment="REAL", account_id=account, event_id=str(uuid4()), request_id=str(uuid4()),
            kind="FEE", asset=asset, quantity=quantity, amount=amount, quote_currency=quote,
            gross_amount=amount or "0", fee="0", note="Gas BNB", occurred_at=NOW.isoformat(), recorded_at=NOW.isoformat())
        writer.writerow([row.get(column, "") for column in COLUMNS])
    content = stream.getvalue()
    source, items = parse_real_events_csv(content)
    assert source == account and len(items) == 2
    assert items[0][1].amount == Decimal("0.0001") and items[1][1].quantity == Decimal("0.0002")
    with pytest.raises(PortfolioError):
        parse_real_events_csv(content.replace("3,REAL", "2,REAL"))
