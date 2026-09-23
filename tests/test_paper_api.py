from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.main import app


TEST_DATABASE_URL = "sqlite://"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    autocommit=False,
)

Base.metadata.create_all(bind=test_engine)


def override_get_db():
    """
    Sustituye la base de datos real por una base
    de datos temporal utilizada únicamente en tests.
    """

    db = TestingSessionLocal()

    try:
        yield db
    finally:
        db.close()


client = TestClient(app)


def clear_database():
    """
    Limpia las tablas antes de cada prueba para evitar
    que una prueba afecte a las siguientes.
    """

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def test_paper_buy_api_executes_order():
    clear_database()

    app.dependency_overrides[get_db] = override_get_db

    response = client.post(
        "/paper/buy",
        json={
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": 100,
            "price": 100000,
            "available_budget": 500,
            "max_order_amount": 200,
            "estimated_fee": 1,
            "max_fee_percentage": 2,
            "exchange": "MEXC",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["executed"] is True
    assert data["reason"] == "Paper trade executed"
    assert data["asset"] == "BTC"
    assert data["amount_spent"] == 100
    assert data["asset_received"] == 0.00099

    app.dependency_overrides.clear()


def test_paper_buy_api_records_operation():
    clear_database()

    app.dependency_overrides[get_db] = override_get_db

    buy_response = client.post(
        "/paper/buy",
        json={
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": 100,
            "price": 100000,
            "available_budget": 500,
            "max_order_amount": 200,
            "estimated_fee": 1,
            "max_fee_percentage": 2,
            "exchange": "MEXC",
        },
    )

    assert buy_response.status_code == 200
    assert buy_response.json()["executed"] is True

    operations_response = client.get("/operations")

    assert operations_response.status_code == 200

    operations = operations_response.json()

    assert len(operations) == 1

    operation = operations[0]

    # Qué ocurrió
    assert operation["operation_type"] == "BUY"

    # Cómo ocurrió
    assert operation["mode"] == "PAPER"

    # De dónde vino
    assert operation["source"] == "MANUAL"

    # Estado de la operación
    assert operation["status"] == "EXECUTED"

    # Exchange simulado
    assert operation["exchange"] == "MEXC"

    # No fue generada por una estrategia
    assert operation["strategy_id"] is None

    # Datos financieros
    assert operation["asset"] == "BTC"
    assert operation["quote_currency"] == "EUR"
    assert operation["amount_spent"] == 100
    assert operation["asset_received"] == 0.00099

    # Toda operación debe tener fecha
    assert operation["timestamp"] is not None

    app.dependency_overrides.clear()


def test_paper_buy_api_blocked_order_not_recorded():
    clear_database()

    app.dependency_overrides[get_db] = override_get_db

    response = client.post(
        "/paper/buy",
        json={
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": 500,
            "price": 100000,
            "available_budget": 100,
            "max_order_amount": 1000,
            "estimated_fee": 1,
            "max_fee_percentage": 2,
            "exchange": "MEXC",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["executed"] is False
    assert data["reason"] == "Insufficient budget"

    operations_response = client.get("/operations")

    assert operations_response.status_code == 200
    assert operations_response.json() == []

    app.dependency_overrides.clear()


def test_paper_buy_api_rejects_invalid_input():
    clear_database()

    app.dependency_overrides[get_db] = override_get_db

    response = client.post(
        "/paper/buy",
        json={
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": -100,
            "price": 100000,
            "available_budget": 500,
            "max_order_amount": 200,
            "estimated_fee": 1,
            "max_fee_percentage": 2,
            "exchange": "MEXC",
        },
    )

    assert response.status_code == 422

    app.dependency_overrides.clear()