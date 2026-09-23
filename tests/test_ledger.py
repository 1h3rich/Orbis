from app.ledger.models import Operation
from app.math.calculations import calculate_portfolio_summary
from app.ledger.repository import (
    create_operation,
    get_operations,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base
from app.ledger.repository import create_operation, get_operations

def test_create_operation():
    """
    Comprueba que podemos representar una compra
    dentro del Ledger de Orbis.
    """
    operation = Operation(
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=100,
        asset_received=0.001,
        price=100000,
        trading_fee=0.10,
    )

    assert operation.operation_type == "BUY"
    assert operation.asset == "BTC"
    assert operation.amount_spent == 100
    assert operation.asset_received == 0.001
    assert operation.trading_fee == 0.10

def test_save_operation_in_database():
    """
    Comprueba que una operación puede guardarse
    y recuperarse desde la base de datos.
    """

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

    db = Session()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=100,
        asset_received=0.001,
        price=100000,
        trading_fee=0.10,
    )

    operations = get_operations(db)

    assert len(operations) == 1
    assert operations[0].asset == "BTC"
    assert operations[0].amount_spent == 100

    db.close()

def test_operations_api():
    """
    Comprueba que la API permite registrar
    y consultar operaciones del Ledger.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database.database import get_db

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

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
            "network_fee": 0
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
    Comprueba que el Ledger calcula correctamente
    el coste total de una operación.
    """
    operation = Operation(
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=100,
        asset_received=0.001,
        price=100000,
        trading_fee=0.50,
        withdrawal_fee=1.00,
        network_fee=0.25,
    )

    assert operation.total_fees() == 1.75
    assert operation.total_cost() == 101.75

def test_calculate_portfolio_summary():
    """
    Comprueba que Orbis puede calcular una cartera
    a partir de varias compras.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = Session()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=500,
        asset_received=0.005,
        price=100000,
        trading_fee=1,
    )

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=500,
        asset_received=0.00625,
        price=80000,
        trading_fee=1,
    )

    operations = get_operations(db)

    summary = calculate_portfolio_summary(operations)

    assert summary["total_invested"] == 1000
    assert summary["total_asset_received"] == 0.01125
    assert summary["total_fees"] == 2

    assert round(
        summary["average_buy_price"], 2
    ) == 88888.89

    db.close()

def test_portfolio_summary_api():
    """
    Comprueba que la API puede calcular
    el resumen de la cartera.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database.database import get_db

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = Session()

        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    client.post(
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
            "network_fee": 0
        }
    )

    response = client.get("/portfolio/summary")

    assert response.status_code == 200

    data = response.json()

    assert data["total_invested"] == 1000
    assert data["total_asset_received"] == 0.01
    assert data["total_fees"] == 3
    assert data["average_buy_price"] == 100000

    app.dependency_overrides.clear()