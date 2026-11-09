"""Ventas PAPER: inventario, caja neta, Guard e idempotencia por `side`."""

from decimal import Decimal
from uuid import uuid4


def buy(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "300",
        "price": "100000",
        "estimated_fee": "1",
    }
    body.update(overrides)
    response = client.post("/paper/buy", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def sell(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "quantity": "0.001",
        "price": "110000",
        "fee": "1",
    }
    body.update(overrides)
    response = client.post("/paper/sell", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def operations(client):
    response = client.get("/paper/operations")
    assert response.status_code == 200
    return response.json()


def test_sell_credits_net_amount_and_records_operation(client, create_scenario):
    create_scenario()
    bought = buy(client)
    assert bought["executed"] is True

    result = sell(client)

    assert result["executed"] is True
    assert result["side"] == "SELL"
    assert result["reason"] == "Paper trade executed"
    # Neto acreditado: 0.001 * 110000 − 1 = 109
    assert Decimal(str(result["amount_spent"])) == Decimal("109")
    assert Decimal(str(result["asset_received"])) == Decimal("0.001")
    assert result["operation_id"] is not None

    recorded = [item for item in operations(client) if item["operation_type"] == "SELL"]
    assert len(recorded) == 1
    operation = recorded[0]
    assert operation["mode"] == "PAPER"
    assert operation["status"] == "EXECUTED"
    assert operation["source"] == "MANUAL"
    assert operation["exchange"] is None
    assert Decimal(str(operation["amount_spent"])) == Decimal("109")
    assert Decimal(str(operation["asset_received"])) == Decimal("0.001")
    assert Decimal(str(operation["trading_fee"])) == Decimal("1")


def test_sell_without_position_is_blocked(client, create_scenario):
    create_scenario()

    result = sell(client, asset="ETH")

    assert result["executed"] is False
    assert result["reason"] == "No open position for asset"
    assert Decimal(str(result["amount_spent"])) == Decimal("0")
    assert Decimal(str(result["asset_received"])) == Decimal("0")
    assert result["operation_id"] is None
    assert operations(client) == []


def test_blocked_sell_retry_returns_same_decision_without_operation(client, create_scenario):
    create_scenario()
    body = {"request_id": str(uuid4()), "asset": "ETH", "quote_currency": "EUR",
            "quantity": "1", "price": "1000", "fee": "0"}

    first = client.post("/paper/sell", json=body)
    repeated = client.post("/paper/sell", json=body)

    assert first.json()["executed"] is False
    assert repeated.json() == first.json()
    assert operations(client) == []


def test_partial_sell_keeps_position_and_average_cost(client, create_scenario):
    create_scenario()
    buy(client)

    before = client.get("/paper/portfolio?quote_currency=EUR").json()["positions"][0]
    sell(client)
    after = client.get("/paper/portfolio?quote_currency=EUR").json()["positions"][0]

    # Coste medio constante durante una venta parcial
    assert Decimal(str(after["average_cost"])) == Decimal(str(before["average_cost"]))
    # El inventario baja y el coste vivo se reduce al precio medio
    assert Decimal(str(after["quantity"])) == Decimal(str(before["quantity"])) - Decimal("0.001")
    assert Decimal(str(after["invested"])) < Decimal(str(before["invested"]))
    assert Decimal(str(after["realized_pnl"])) > Decimal("0")


def test_full_sell_leaves_no_position(client, create_scenario):
    create_scenario()
    bought = buy(client)
    quantity = Decimal(str(bought["asset_received"]))

    result = sell(client, quantity=str(quantity), price="100000", fee="0")

    assert result["executed"] is True

    portfolio = client.get("/paper/portfolio?quote_currency=EUR").json()
    assert portfolio["positions"] == []
    assert Decimal(str(portfolio["cash_balance"])) == Decimal("1000") - Decimal("300") + quantity * Decimal("100000")
    assert Decimal(str(portfolio["positions_value"])) == Decimal("0")


def test_sell_more_than_available_is_blocked(client, create_scenario):
    create_scenario()
    bought = buy(client)
    quantity = Decimal(str(bought["asset_received"]))

    result = sell(client, quantity=str(quantity + Decimal("0.0001")))

    assert result["executed"] is False
    assert result["reason"] == "Sell quantity exceeds available position"
    assert len(operations(client)) == 1  # solo la compra


def test_cash_balance_is_initial_minus_buys_plus_sells(client, create_scenario):
    create_scenario()
    buy(client)
    sell(client)

    scenario = client.get("/paper/scenarios/EUR").json()
    cash = Decimal(str(scenario["cash_balance"]))
    assert cash == Decimal("1000") - Decimal("300") + Decimal("109")
    # get_total_spent sigue midiendo solo compras
    assert Decimal(str(scenario["total_spent"])) == Decimal("300")
    assert Decimal(str(scenario["available_balance"])) == Decimal("700")


def test_sell_rejects_invalid_input(client, create_scenario):
    create_scenario()
    base = {"request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
            "quantity": "0.001", "price": "110000", "fee": "1"}

    invalid = [
        {**base, "quantity": "0"},
        {**base, "quantity": "-1"},
        {**base, "price": "0"},
        {**base, "price": "-5"},
        {**base, "fee": "-1"},
        {**base, "strategy_id": 7},
        {**base, "mode": "LIVE"},
        {**base, "available_quantity": "10"},
    ]
    for body in invalid:
        response = client.post("/paper/sell", json=body)
        assert response.status_code == 422, body

    assert operations(client) == []


def test_sell_guard_blocks_fee_above_share(client, create_scenario):
    create_scenario()
    buy(client, order_amount="500", estimated_fee="0")

    result = sell(client, quantity="0.001", price="100000", fee="999999")

    assert result["executed"] is False
    assert result["reason"] in (
        "Estimated fee must be lower than order amount",
        "Estimated fee exceeds allowed percentage",
    )
    assert len(operations(client)) == 1


def test_sell_requires_open_scenario(client, create_scenario):
    create_scenario()
    response = client.post("/paper/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "USD",
        "quantity": "1", "price": "1", "fee": "0",
    })
    assert response.status_code == 404


def test_sell_retry_same_payload_reuses_result(client, create_scenario):
    create_scenario()
    buy(client)
    body = {"request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
            "quantity": "0.001", "price": "110000", "fee": "1"}

    first = client.post("/paper/sell", json=body)
    repeated = client.post("/paper/sell", json=body)
    changed = client.post("/paper/sell", json={**body, "price": "111000"})

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert changed.status_code == 409
    assert len([o for o in operations(client) if o["operation_type"] == "SELL"]) == 1


def test_request_id_cannot_move_from_buy_to_sell(client, create_scenario):
    create_scenario()
    request_id = str(uuid4())

    bought = client.post("/paper/buy", json={
        "request_id": request_id, "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "100", "price": "100000", "estimated_fee": "0",
    })
    sold = client.post("/paper/sell", json={
        "request_id": request_id, "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.001", "price": "100000", "fee": "0",
    })

    assert bought.status_code == 200
    assert sold.status_code == 409
    assert len(operations(client)) == 1


def test_request_id_cannot_move_across_quote_currencies(client, create_scenario):
    """El digest incluye `quote_currency`: mismo request_id en EUR y USD -> 409 (C-05)."""
    create_scenario()
    create_scenario(quote_currency="USD")
    request_id = str(uuid4())

    eur = client.post("/paper/buy", json={
        "request_id": request_id, "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "100", "price": "100000", "estimated_fee": "0",
    })
    usd = client.post("/paper/buy", json={
        "request_id": request_id, "asset": "BTC", "quote_currency": "USD",
        "order_amount": "100", "price": "100000", "estimated_fee": "0",
    })

    assert eur.status_code == 200
    assert eur.json()["executed"] is True
    assert usd.status_code == 409
    assert len(operations(client)) == 1

    sell_id = str(uuid4())
    eur_sell = client.post("/paper/sell", json={
        "request_id": sell_id, "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.0005", "price": "110000", "fee": "0",
    })
    usd_sell = client.post("/paper/sell", json={
        "request_id": sell_id, "asset": "BTC", "quote_currency": "USD",
        "quantity": "0.0005", "price": "110000", "fee": "0",
    })

    assert eur_sell.status_code == 200
    assert eur_sell.json()["executed"] is True
    assert usd_sell.status_code == 409
    assert len([o for o in operations(client) if o["operation_type"] == "SELL"]) == 1


def test_legacy_blocked_buy_decision_is_preserved_for_sells(client, create_scenario):
    """Un `request_id` de compra bloqueada no puede reutilizarse para vender."""
    create_scenario()
    request_id = str(uuid4())

    blocked = client.post("/paper/buy", json={
        "request_id": request_id, "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "900", "price": "100000", "estimated_fee": "0",
    })
    reused = client.post("/paper/sell", json={
        "request_id": request_id, "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.001", "price": "100000", "fee": "0",
    })

    assert blocked.json()["executed"] is False
    assert reused.status_code == 409
    assert operations(client) == []


def test_legacy_digest_without_side_is_still_accepted(client, create_scenario, db):
    """Los `paper_requests` guardados antes de añadir `side` siguen reutilizables."""
    import hashlib
    import json

    from app.database.models import PaperRequest

    create_scenario()
    request_id = str(uuid4())
    body = {
        "request_id": request_id,
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "100",
        "price": "100000",
        "estimated_fee": "0",
        "exchange": None,
    }
    legacy_payload = {
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "100",
        "price": "100000",
        "estimated_fee": "0",
        "exchange": None,
    }
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    db.add(PaperRequest(
        request_id=request_id,
        payload_digest=legacy_digest,
        executed=True,
        reason="Paper trade executed",
        asset="BTC",
        amount_spent=Decimal("100"),
        asset_received=Decimal("0.001"),
        price=Decimal("100000"),
        fee=Decimal("0"),
        operation_id=None,
    ))
    db.commit()

    response = client.post("/paper/buy", json=body)

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert response.json()["operation_id"] is None
    assert operations(client) == []
