"""Ampliaciones UX mínimas: resumen del Ledger, limit/order y pesos de cartera.

* ``GET /paper/operations/summary`` acepta los mismos filtros que
  ``GET /paper/operations`` y devuelve conteos, importes, volumen y
  comisiones (todo Decimal del servidor).
* ``GET /paper/operations?limit&order`` es aditivo: por defecto el orden y
  el contenido no cambian.
* ``GET /paper/portfolio`` incluye ``cash_weight`` y ``weight_percentage``
  por posición (Decimal, solo presentación).
"""

from decimal import Decimal
from uuid import uuid4


def _buy(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "100",
        "price": "100000",
        "estimated_fee": "0",
    }
    body.update(overrides)
    return client.post("/paper/buy", json=body)


def _sell(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "quantity": "0.0005",
        "price": "100000",
        "fee": "0",
    }
    body.update(overrides)
    return client.post("/paper/sell", json=body)


def test_operations_summary_counts_and_totals(client, create_scenario):
    create_scenario()
    assert _buy(client).status_code == 200
    assert _sell(client).status_code == 200

    summary = client.get("/paper/operations/summary").json()
    assert summary["operations_count"] == 2
    assert summary["buys_count"] == 1
    assert summary["sells_count"] == 1
    assert Decimal(str(summary["bought_amount"])) == Decimal("100")
    assert Decimal(str(summary["sold_amount"])) == Decimal("50")
    assert Decimal(str(summary["volume"])) == Decimal("150")
    assert Decimal(str(summary["total_fees"])) == Decimal("0")


def test_operations_summary_respects_same_filters(client, create_scenario):
    create_scenario()
    assert _buy(client).status_code == 200
    assert _sell(client).status_code == 200

    buys = client.get("/paper/operations/summary?side=BUY").json()
    assert buys["operations_count"] == 1
    assert buys["buys_count"] == 1
    assert buys["sells_count"] == 0

    empty = client.get("/paper/operations/summary?asset=DOGE").json()
    assert empty["operations_count"] == 0
    assert Decimal(str(empty["volume"])) == Decimal("0")

    date_filtered = client.get("/paper/operations/summary?date=2000-01-01").json()
    assert date_filtered["operations_count"] == 0


def test_operations_summary_rejects_bad_date(client):
    response = client.get("/paper/operations/summary?date=05-10-2026")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid date; expected YYYY-MM-DD"


def test_operations_limit_and_order_are_additive(client, create_scenario):
    create_scenario()
    assert _buy(client, order_amount="100").status_code == 200
    assert _buy(client, order_amount="200").status_code == 200

    default = client.get("/paper/operations").json()
    assert len(default) == 2

    limited = client.get("/paper/operations?limit=1").json()
    assert len(limited) == 1
    assert limited[0]["id"] == default[0]["id"]

    desc = client.get("/paper/operations?order=desc").json()
    assert [item["id"] for item in desc] == [item["id"] for item in reversed(default)]

    desc_limited = client.get("/paper/operations?order=desc&limit=1").json()
    assert len(desc_limited) == 1
    assert desc_limited[0]["id"] == default[-1]["id"]


def test_portfolio_weights_are_decimal_and_sum(client, create_scenario):
    create_scenario()
    assert _buy(client).status_code == 200

    portfolio = client.get("/paper/portfolio?quote_currency=EUR").json()
    assert "cash_weight" in portfolio
    cash_weight = Decimal(str(portfolio["cash_weight"]))
    assert Decimal("0") <= cash_weight <= Decimal("100")

    total_weight = cash_weight
    for position in portfolio["positions"]:
        assert "weight_percentage" in position
        weight = Decimal(str(position["weight_percentage"]))
        assert Decimal("0") <= weight <= Decimal("100")
        total_weight += weight
    # Pesos de caja + posiciones = 100% (tolerancia de serialización).
    assert abs(total_weight - Decimal("100")) < Decimal("0.0001")
