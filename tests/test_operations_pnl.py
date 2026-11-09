"""M2: PnL realizado por operación (``GET /paper/operations?pnl=true``).

El PnL lo calcula el servidor reutilizando el pliegue de coste medio de
``app/math/positions.py`` (``apply_operation``/``Position``); el Ledger solo
lo muestra. Todo en ``Decimal`` exacto a nivel de dominio; en el borde HTTP
los ``Decimal`` se serializan a número JSON (float), así que ahí se compara
con tolerancia mínima de serialización.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.ledger.repository import create_operation, list_paper_operations
from app.math.positions import build_positions, operation_pnl

BASE = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def seed(db, minute, **overrides):
    """Operación directa en el Ledger con instante controlado."""
    body = {
        "operation_type": "BUY",
        "asset": "BTC",
        "quote_currency": "EUR",
        "amount_spent": Decimal("100"),
        "asset_received": Decimal("0.001"),
        "price": Decimal("100000"),
        "mode": "PAPER",
    }
    body.update(overrides)
    operation = create_operation(db=db, **body)
    operation.timestamp = BASE + timedelta(minutes=minute)
    db.commit()
    return operation


def buy(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "100",
        "price": "100000",
        "estimated_fee": "0",
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
        "price": "90000",
        "fee": "0",
    }
    body.update(overrides)
    response = client.post("/paper/sell", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_partial_sell_formula_is_exact(db):
    """BUY×2 a precios distintos + SELL parcial con comisión.

    realized = (venta − coste_medio) × qty − comisiones, en Decimal exacto.
    """
    seed(db, 0, amount_spent=Decimal("100"), asset_received=Decimal("0.001"),
         price=Decimal("100000"))
    seed(db, 1, amount_spent=Decimal("160"), asset_received=Decimal("0.002"),
         price=Decimal("80000"))
    sale = seed(db, 2, operation_type="SELL", amount_spent=Decimal("89"),
                asset_received=Decimal("0.001"), price=Decimal("90000"),
                trading_fee=Decimal("1"))

    annotated = operation_pnl(list_paper_operations(db))

    average = Decimal("260") / Decimal("0.003")
    expected = (Decimal("90000") - average) * Decimal("0.001") - Decimal("1")
    assert annotated[sale.id]["realized_pnl"] == expected
    # Coherencia interna exacta: proceeds − coste − comisiones.
    assert annotated[sale.id]["realized_pnl"] == (
        annotated[sale.id]["proceeds"]
        - annotated[sale.id]["cost_basis"] * Decimal("0.001")
        - annotated[sale.id]["fees"]
    )
    assert annotated[sale.id]["cost_basis"] == average
    assert annotated[sale.id]["proceeds"] == Decimal("90")
    assert annotated[sale.id]["fees"] == Decimal("1")

    cost = average * Decimal("0.001")
    assert annotated[sale.id]["pnl_pct"] == expected / cost * Decimal("100")


def test_sell_rounding_residual_reduces_realized_pnl_in_ledger_annotation(db):
    seed(db, 0, amount_spent=Decimal("1"), asset_received=Decimal("1"),
         price=Decimal("1"))
    sale = seed(db, 1, operation_type="SELL",
                amount_spent=Decimal("0.000000000001"),
                asset_received=Decimal("0.000000000002"),
                price=Decimal("0.8"))
    annotated = operation_pnl(list_paper_operations(db))[sale.id]
    assert annotated["proceeds"] == Decimal("0.0000000000016")
    assert annotated["realized_pnl"] == Decimal("-0.000000000001")
    assert annotated["realized_pnl"] == sale.amount_spent - annotated["cost_basis"] * sale.asset_received


def test_realized_sums_match_build_positions(db):
    """Σ realized_pnl por par == realized_pnl de build_positions (no-divergencia).

    Incluye cierre total del pliegue (residuo de redondeo), reapertura de un
    pliegue nuevo y un segundo par aislado.
    """
    seed(db, 0, amount_spent=Decimal("100"), asset_received=Decimal("0.001"),
         price=Decimal("100000"))
    seed(db, 1, amount_spent=Decimal("160"), asset_received=Decimal("0.002"),
         price=Decimal("80000"))
    seed(db, 2, operation_type="SELL", amount_spent=Decimal("90"),
         asset_received=Decimal("0.001"), price=Decimal("90000"))
    # Cierra el pliegue: vende las 0.002 restantes.
    seed(db, 3, operation_type="SELL", amount_spent=Decimal("170"),
         asset_received=Decimal("0.002"), price=Decimal("85000"))
    # Pliegue nuevo tras el cierre (no arrastra coste).
    seed(db, 4, amount_spent=Decimal("50"), asset_received=Decimal("0.0005"),
         price=Decimal("100000"))
    seed(db, 5, operation_type="SELL", amount_spent=Decimal("55"),
         asset_received=Decimal("0.0005"), price=Decimal("110000"))
    # Otro par: no debe contaminar el de BTC.
    seed(db, 0, asset="ETH", amount_spent=Decimal("60"),
         asset_received=Decimal("0.02"), price=Decimal("3000"))
    seed(db, 1, asset="ETH", operation_type="SELL", amount_spent=Decimal("70"),
         asset_received=Decimal("0.02"), price=Decimal("3500"))
    # Una fila no ejecutada: a cero y fuera de la suma.
    seed(db, 6, amount_spent=Decimal("999"), asset_received=Decimal("9"),
         price=Decimal("111"), status="REJECTED")

    operations = list_paper_operations(db)
    annotated = operation_pnl(operations)

    for pair in (("BTC", "EUR"), ("ETH", "EUR")):
        expected = next(
            position.realized_pnl
            for position in build_positions(operations)
            if (position.asset, position.quote_currency) == pair
        )
        total = sum(
            (annotated[operation.id]["realized_pnl"] for operation in operations
             if (operation.asset, operation.quote_currency) == pair),
            Decimal("0"),
        )
        assert total == expected

    rejected = next(
        operation for operation in operations if operation.status == "REJECTED"
    )
    assert annotated[rejected.id]["realized_pnl"] == Decimal("0")
    assert annotated[rejected.id]["cost_basis"] == Decimal("0")
    assert annotated[rejected.id]["proceeds"] == Decimal("0")


def test_pnl_true_exposes_server_computed_fields(client, create_scenario):
    """?pnl=true añade los 5 campos con números que cuadran a mano.

    Compras sin comisión: 100 @100000 (0.001) + 200 @80000 (0.0025);
    coste medio = 300/0.0035; venta 0.001 @90000 sin comisión:
    realized = 90 − 300/3.5 × ... = 30/7 y pnl_pct = 5 exacto.
    """
    create_scenario()
    buy(client)
    buy(client, order_amount="200", price="80000")
    sell(client)

    items = client.get("/paper/operations?pnl=true").json()
    assert [item["operation_type"] for item in items] == ["BUY", "BUY", "SELL"]
    for item in items:
        for field in ("cost_basis", "proceeds", "fees", "realized_pnl", "pnl_pct"):
            assert field in item
            Decimal(str(item[field]))  # parseable como número

    buys, _, sale = items
    assert Decimal(str(buys["realized_pnl"])) == Decimal("0")
    assert Decimal(str(buys["pnl_pct"])) == Decimal("0")
    assert Decimal(str(sale["proceeds"])) == Decimal("90")
    assert Decimal(str(sale["fees"])) == Decimal("0")

    average = Decimal("300") / Decimal("0.0035")
    assert abs(Decimal(str(sale["cost_basis"])) - average) < Decimal("1e-9")
    expected = Decimal("90") - average * Decimal("0.001")
    assert abs(Decimal(str(sale["realized_pnl"])) - expected) < Decimal("1e-9")
    assert abs(Decimal(str(sale["pnl_pct"])) - Decimal("5")) < Decimal("1e-6")

    portfolio = client.get("/paper/portfolio?quote_currency=EUR").json()
    assert abs(Decimal(str(sale["realized_pnl"]))
               - Decimal(str(portfolio["realized_pnl"]))) < Decimal("1e-9")


def test_without_pnl_response_is_byte_identical(client, create_scenario):
    """Sin ?pnl=true la respuesta es byte-idéntica a la de hoy."""
    create_scenario()
    buy(client)
    sell(client)

    plain = client.get("/paper/operations")
    assert plain.status_code == 200
    assert client.get("/paper/operations").content == plain.content
    assert client.get("/paper/operations?pnl=false").content == plain.content
    assert all(
        "realized_pnl" not in item and "cost_basis" not in item
        for item in plain.json()
    )


def test_limit_and_order_apply_after_pnl_computation(client, create_scenario):
    """limit/order/filtros recortan después: el coste medio no se falsea."""
    create_scenario()
    buy(client)
    buy(client, order_amount="200", price="80000")
    sell(client)

    full = client.get("/paper/operations?pnl=true").json()
    sale_full = next(
        item for item in full if item["operation_type"] == "SELL"
    )

    desc_limited = client.get("/paper/operations?pnl=true&order=desc&limit=1").json()
    assert len(desc_limited) == 1
    assert desc_limited[0]["id"] == sale_full["id"]
    assert desc_limited[0]["realized_pnl"] == sale_full["realized_pnl"]
    assert desc_limited[0]["cost_basis"] == sale_full["cost_basis"]

    sells_only = client.get("/paper/operations?pnl=true&side=SELL").json()
    assert [item["operation_type"] for item in sells_only] == ["SELL"]
    assert sells_only[0]["realized_pnl"] == sale_full["realized_pnl"]

    limited = client.get("/paper/operations?pnl=true&limit=2").json()
    assert [item["id"] for item in limited] == [item["id"] for item in full[:2]]
    assert limited[0]["cost_basis"] == full[0]["cost_basis"]


def test_sell_without_position_keeps_literal_error(client, create_scenario):
    """Venta sin posición: el literal de Guard no cambia con M2."""
    create_scenario()
    result = sell(client, asset="ETH")

    assert result["executed"] is False
    assert result["reason"] == "No open position for asset"
    assert client.get("/paper/operations").json() == []
    assert client.get("/paper/operations?pnl=true").json() == []
