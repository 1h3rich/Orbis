"""La API no debe degradar importes Decimal a float en JSON."""

from decimal import Decimal

from app.ledger.repository import create_operation


LARGE_TEXT = "123456789012345678.123456789012"


def test_manual_operation_and_summaries_emit_exact_decimal_strings(client, db):
    created = client.post("/operations", json={
        "operation_type": "BUY", "asset": "BTC", "quote_currency": "EUR",
        "amount_spent": LARGE_TEXT, "asset_received": LARGE_TEXT,
        "price": "1", "trading_fee": "0.000000000001",
    })
    assert created.status_code == 200, created.text
    assert created.json()["amount_spent"] == LARGE_TEXT

    operations = client.get("/operations")
    assert operations.status_code == 200
    assert operations.json()[0]["amount_spent"] == LARGE_TEXT

    buys = client.get("/portfolio/buys")
    assert buys.status_code == 200
    assert buys.json()[0]["total_invested"] == LARGE_TEXT

    summary = client.get("/paper/operations/summary")
    assert summary.status_code == 200
    assert summary.json()["bought_amount"] == LARGE_TEXT


def test_operation_summary_volume_keeps_last_decimal(client, db):
    create_operation(db, "BUY", "BTC", "EUR", Decimal(LARGE_TEXT), Decimal("1"), Decimal("1"))
    create_operation(db, "SELL", "BTC", "EUR", Decimal("0.000000000001"), Decimal("1"), Decimal("1"))
    summary = client.get("/paper/operations/summary")
    assert summary.status_code == 200
    assert summary.json()["volume"] == "123456789012345678.123456789013"


def test_scenario_portfolio_price_and_pnl_emit_decimal_strings(client, db):
    scenario = client.post("/paper/scenarios", json={
        "quote_currency": "EUR", "initial_balance": LARGE_TEXT,
        "max_order_amount": LARGE_TEXT, "max_fee_percentage": "2",
    })
    assert scenario.status_code == 201, scenario.text
    assert scenario.json()["initial_balance"] == LARGE_TEXT
    assert scenario.json()["cash_balance"] == LARGE_TEXT
    assert client.get("/paper/scenarios/EUR").json()["initial_balance"] == LARGE_TEXT

    quote = client.post("/paper/prices", json={
        "asset": "BTC", "quote_currency": "EUR", "price": LARGE_TEXT,
    })
    assert quote.status_code == 201, quote.text
    assert quote.json()["price"] == LARGE_TEXT
    assert client.get("/paper/prices").json()[0]["price"] == LARGE_TEXT

    create_operation(db, "BUY", "BTC", "EUR", Decimal("1"), Decimal("1"), Decimal("1"))
    portfolio = client.get("/paper/portfolio?quote_currency=EUR")
    assert portfolio.status_code == 200, portfolio.text
    assert portfolio.json()["initial_balance"] == LARGE_TEXT
    pnl = client.get("/paper/operations?pnl=true")
    assert pnl.status_code == 200, pnl.text
    assert pnl.json()[0]["amount_spent"] == "1"
    assert isinstance(pnl.json()[0]["cost_basis"], str)
