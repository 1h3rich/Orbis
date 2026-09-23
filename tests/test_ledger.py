from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.ledger.models import Operation
from app.ledger.repository import create_operation, get_operations
from app.main import app
from app.math.calculations import calculate_portfolio_summary


def create_test_db():
    """
    Crea una base de datos SQLite temporal
    para una prueba.
    """

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

    return engine, Session


def test_create_operation():
    """
    Comprueba que podemos representar una compra
    utilizando Decimal dentro del dominio de Orbis.
    """

    operation = Operation(
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("100"),
        asset_received=Decimal("0.001"),
        price=Decimal("100000"),
        trading_fee=Decimal("0.10"),
    )

    assert operation.operation_type == "BUY"
    assert operation.asset == "BTC"

    assert operation.amount_spent == Decimal("100")
    assert operation.asset_received == Decimal("0.001")
    assert operation.trading_fee == Decimal("0.10")


def test_save_operation_in_database():
    """
    Comprueba que una operación puede guardarse
    y recuperarse manteniendo Decimal.
    """

    engine, Session = create_test_db()

    db = Session()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("100"),
        asset_received=Decimal("0.001"),
        price=Decimal("100000"),
        trading_fee=Decimal("0.10"),
    )

    operations = get_operations(db)

    assert len(operations) == 1
    assert operations[0].asset == "BTC"

    assert operations[0].amount_spent == Decimal(
        "100.000000000000"
    )

    assert isinstance(
        operations[0].amount_spent,
        Decimal
    )

    db.close()


def test_operations_api():
    """
    Comprueba que la API permite registrar
    y consultar operaciones del Ledger.
    """

    engine, Session = create_test_db()

    def override_get_db():
        db = Session()

        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    response = client.post(
        "/operations",
        json={
            "operation_type": "BUY",
            "asset": "BTC",
            "quote_currency": "EUR",
            "amount_spent": 100,
            "asset_received": 0.001,
            "price": 100000,
            "trading_fee": 0.10,
            "withdrawal_fee": 0,
            "network_fee": 0,
        }
    )

    assert response.status_code == 200

    response = client.get("/operations")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["asset"] == "BTC"

    app.dependency_overrides.clear()


def test_operation_total_cost():
    """
    Comprueba que Decimal mantiene la precisión
    al calcular comisiones y coste total.
    """

    operation = Operation(
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("100"),
        asset_received=Decimal("0.001"),
        price=Decimal("100000"),
        trading_fee=Decimal("0.50"),
        withdrawal_fee=Decimal("1.00"),
        network_fee=Decimal("0.25"),
    )

    assert operation.total_fees() == Decimal("1.75")
    assert operation.total_cost() == Decimal("101.75")


def test_calculate_portfolio_summary():
    """
    Comprueba que Orbis puede calcular una cartera
    a partir de varias compras almacenadas con Decimal.
    """

    engine, Session = create_test_db()

    db = Session()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("500"),
        asset_received=Decimal("0.005"),
        price=Decimal("100000"),
        trading_fee=Decimal("1"),
    )

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("500"),
        asset_received=Decimal("0.00625"),
        price=Decimal("80000"),
        trading_fee=Decimal("1"),
    )

    operations = get_operations(db)

    summary = calculate_portfolio_summary(operations)

    assert summary["total_invested"] == Decimal(
        "1000.000000000000"
    )

    assert summary["total_asset_received"] == Decimal(
        "0.011250000000"
    )

    assert summary["total_fees"] == Decimal(
        "2.000000000000"
    )

    assert summary["average_buy_price"].quantize(
        Decimal("0.01")
    ) == Decimal("88888.89")

    db.close()


def test_portfolio_summary_api():
    """
    Comprueba que la API puede devolver correctamente
    el resumen de una cartera basada en Decimal.
    """

    engine, Session = create_test_db()

    def override_get_db():
        db = Session()

        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    response = client.post(
        "/operations",
        json={
            "operation_type": "BUY",
            "asset": "BTC",
            "quote_currency": "EUR",
            "amount_spent": 1000,
            "asset_received": 0.01,
            "price": 100000,
            "trading_fee": 2,
            "withdrawal_fee": 1,
            "network_fee": 0,
        }
    )

    assert response.status_code == 200

    response = client.get("/portfolio/summary")

    assert response.status_code == 200

    data = response.json()

    assert Decimal(str(data["total_invested"])) == Decimal("1000")
    assert Decimal(str(data["total_asset_received"])) == Decimal("0.01")
    assert Decimal(str(data["total_fees"])) == Decimal("3")
    assert Decimal(str(data["average_buy_price"])) == Decimal("100000")

    app.dependency_overrides.clear()


def test_ledger_operation_metadata():
    """
    El Ledger debe conservar metadatos suficientes
    para reconstruir el origen de una operación.
    """

    engine, Session = create_test_db()

    db = Session()

    operation = create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("100"),
        asset_received=Decimal("0.001"),
        price=Decimal("100000"),
        trading_fee=Decimal("1"),
        mode="PAPER",
        source="MANUAL",
        status="EXECUTED",
        exchange="MEXC",
    )

    assert operation.id is not None
    assert operation.timestamp is not None

    assert operation.operation_type == "BUY"
    assert operation.mode == "PAPER"
    assert operation.source == "MANUAL"
    assert operation.status == "EXECUTED"
    assert operation.exchange == "MEXC"
    assert operation.strategy_id is None

    db.close()


def test_ledger_default_metadata():
    """
    Comprueba los valores seguros por defecto
    de una operación del Ledger.
    """

    engine, Session = create_test_db()

    db = Session()

    operation = create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("100"),
        asset_received=Decimal("0.001"),
        price=Decimal("100000"),
    )

    assert operation.mode == "PAPER"
    assert operation.source == "MANUAL"
    assert operation.status == "EXECUTED"
    assert operation.exchange is None
    assert operation.strategy_id is None
    assert operation.timestamp is not None

    db.close()


def test_manual_operation_schema_rejects_forged_source():
    from pydantic import ValidationError
    from app.api.schemas import OperationCreate

    with pytest.raises(ValidationError):
        OperationCreate.model_validate({
            "operation_type": "BUY",
            "asset": "BTC",
            "quote_currency": "EUR",
            "amount_spent": "100",
            "asset_received": "0.001",
            "price": "100000",
            "source": "STRATEGY",
        })


def test_manual_operation_rejects_unknown_strategy_reference():
    engine, Session = create_test_db()

    def override_get_db():
        with Session() as db:
            yield db

    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/operations",
                json={
                    "operation_type": "BUY",
                    "asset": "BTC",
                    "quote_currency": "EUR",
                    "amount_spent": "100",
                    "asset_received": "0.001",
                    "price": "100000",
                    "strategy_id": 999,
                },
            )
            operations = client.get("/operations")

        assert response.status_code == 404
        assert operations.json() == []
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override
        engine.dispose()
