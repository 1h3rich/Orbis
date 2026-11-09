"""Los importes nuevos no deben pasar por REAL de SQLite."""

from decimal import Decimal

from sqlalchemy import text

from app.database.models import Operation, PaperScenario, PaperRequest, PriceQuote
from app.core.paper_scenarios import paper_scenario_snapshot
from app.core.paper_portfolio import build_paper_portfolio
from app.math.positions import build_positions
from app.math.calculations import calculate_average_cost, calculate_buy_summaries
from app.ledger.repository import create_operation, get_total_spent


LARGE = Decimal("123456789012345678.123456789012")


def test_average_cost_accepts_mixed_integer_and_decimal_inputs():
    assert calculate_average_cost(3, Decimal("2")) == Decimal("1.5")


def test_operation_roundtrip_keeps_all_30_digits_and_raw_sqlite_text(db):
    operation = create_operation(
        db, "BUY", "BTC", "EUR", LARGE, Decimal("0.000000000001"),
        Decimal("123456789012345678.123456789012"),
        trading_fee=Decimal("0.000000000001"),
    )
    db.expire_all()
    saved = db.get(Operation, operation.id)
    assert saved.amount_spent == LARGE
    assert saved.price == LARGE
    assert get_total_spent(db, "PAPER", "EUR") == LARGE
    raw = db.execute(text(
        "SELECT typeof(amount_spent), amount_spent FROM operations WHERE id=:id"
    ), {"id": operation.id}).one()
    assert raw[0] == "text"
    assert str(LARGE) in raw[1]


def test_operation_total_fees_keeps_small_component(db):
    operation = create_operation(
        db, "BUY", "BTC", "EUR", LARGE, Decimal("1"), Decimal("1"),
        trading_fee=LARGE, withdrawal_fee=Decimal("0.000000000001"),
    )
    assert operation.total_fees == Decimal("123456789012345678.123456789013")


def test_scenario_request_and_quote_roundtrip_keep_exact_decimals(db):
    scenario = PaperScenario(
        quote_currency="EUR", initial_balance=LARGE,
        max_order_amount=LARGE, max_fee_percentage=Decimal("2.0001"),
        daily_limit=LARGE, monthly_limit=LARGE,
    )
    request = PaperRequest(
        request_id="a" * 36, payload_digest="b" * 64,
        executed=False, reason="test", asset="BTC",
        amount_spent=LARGE, asset_received=LARGE, price=LARGE, fee=LARGE,
    )
    quote = PriceQuote(
        asset="BTC", quote_currency="EUR", price=LARGE, source="TEST",
    )
    db.add_all([scenario, request, quote])
    db.commit()
    db.expire_all()
    assert db.get(PaperScenario, scenario.id).initial_balance == LARGE
    assert db.get(PaperScenario, scenario.id).daily_limit == LARGE
    assert db.get(PaperRequest, request.request_id).fee == LARGE
    assert db.get(PriceQuote, quote.id).price == LARGE
    assert db.execute(text(
        "SELECT typeof(initial_balance) FROM paper_scenarios WHERE id=:id"
    ), {"id": scenario.id}).scalar_one() == "text"


def test_preexisting_numeric_rows_remain_readable(db):
    db.execute(text(
        "INSERT INTO operations (timestamp, operation_type, mode, source, status,"
        " asset, quote_currency, amount_spent, asset_received, price,"
        " trading_fee, withdrawal_fee, network_fee)"
        " VALUES ('2026-01-01 00:00:00', 'BUY', 'PAPER', 'MANUAL', 'EXECUTED',"
        " 'BTC', 'EUR', 70.25, 0.001, 70250, 0, 0, 0)"
    ))
    db.commit()
    operation = db.query(Operation).one()
    assert operation.amount_spent == Decimal("70.25")
    assert operation.asset_received == Decimal("0.001")


def test_scenario_cash_snapshot_keeps_last_decimal(db):
    scenario = PaperScenario(
        quote_currency="EUR", initial_balance=LARGE,
        max_order_amount=LARGE, max_fee_percentage=Decimal("2"),
    )
    db.add(scenario)
    db.commit()
    create_operation(
        db, "BUY", "BTC", "EUR", Decimal("0.000000000001"),
        Decimal("0.000000000001"), Decimal("1"),
    )
    snapshot = paper_scenario_snapshot(db, scenario)
    assert snapshot["cash_balance"] == Decimal("123456789012345678.123456789011")
    assert snapshot["available_balance"] == Decimal("123456789012345678.123456789011")


def test_position_and_portfolio_keep_last_decimal_after_two_buys(db):
    scenario = PaperScenario(
        quote_currency="EUR", initial_balance=Decimal("246913578024691356.246913578024"),
        max_order_amount=LARGE, max_fee_percentage=Decimal("2"),
    )
    db.add(scenario)
    db.commit()
    create_operation(db, "BUY", "BTC", "EUR", LARGE, LARGE, Decimal("1"))
    create_operation(
        db, "BUY", "BTC", "EUR", Decimal("0.000000000001"),
        Decimal("0.000000000001"), Decimal("1"),
    )
    operations = db.query(Operation).order_by(Operation.id).all()
    position = build_positions(operations)[0]
    expected = Decimal("123456789012345678.123456789013")
    assert position.quantity == expected
    assert position.invested == expected
    buy_summary = calculate_buy_summaries(operations, "PAPER")[0]
    assert buy_summary["total_invested"] == expected
    assert buy_summary["total_asset_received"] == expected

    class FixedPrice:
        def get_price(self, _asset, _quote):
            return Decimal("1")

    portfolio = build_paper_portfolio(db, scenario, FixedPrice())
    assert portfolio["positions"][0]["quantity"] == expected
    assert portfolio["equity"] == scenario.initial_balance
    assert portfolio["total_pnl"] == Decimal("0")
