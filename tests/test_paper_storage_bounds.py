"""El contrato NUMERIC(30,12) no acepta importes fuera de su escala."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import StatementError

from app.database.models import PriceQuote


@pytest.mark.parametrize("field,value", [
    ("initial_balance", "1000000000000000000"),
    ("max_order_amount", "0.0000000000001"),
    ("max_fee_percentage", "2.00001"),
])
def test_scenario_rejects_out_of_contract_amounts(client, field, value):
    body = {
        "quote_currency": "EUR", "initial_balance": "1000",
        "max_order_amount": "500", "max_fee_percentage": "2",
    }
    body[field] = value
    response = client.post("/paper/scenarios", json=body)
    assert response.status_code == 422, response.text
    assert client.get("/paper/scenarios").json() == []


@pytest.mark.parametrize("path,body", [
    ("/paper/prices", {"asset": "BTC", "quote_currency": "EUR", "price": "1.0000000000001"}),
    ("/operations", {
        "operation_type": "BUY", "asset": "BTC", "quote_currency": "EUR",
        "amount_spent": "1000000000000000000", "asset_received": "1", "price": "1",
    }),
])
def test_manual_financial_routes_reject_out_of_contract_amounts(client, path, body):
    response = client.post(path, json=body)
    assert response.status_code == 422, response.text
    assert client.get("/operations").json() == []


def test_storage_type_rejects_direct_out_of_contract_write(db):
    db.add(PriceQuote(asset="BTC", quote_currency="EUR", price=Decimal("1.0000000000001")))
    with pytest.raises(StatementError):
        db.commit()
    db.rollback()
    assert db.query(PriceQuote).count() == 0
