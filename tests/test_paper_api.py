from fastapi.testclient import TestClient
from decimal import Decimal
from uuid import uuid4
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


def create_scenario(
    initial_balance="500",
    max_order_amount="200",
    daily_limit=None,
    monthly_limit=None,
):
    body = {
        "quote_currency": "EUR",
        "initial_balance": initial_balance,
        "max_order_amount": max_order_amount,
        "max_fee_percentage": "2",
    }
    if daily_limit is not None:
        body["daily_limit"] = daily_limit
    if monthly_limit is not None:
        body["monthly_limit"] = monthly_limit

    response = client.post(
        "/paper/scenarios",
        json=body,
    )
    assert response.status_code == 201


def test_paper_buy_api_executes_order():
    clear_database()

    app.dependency_overrides[get_db] = override_get_db
    create_scenario()

    response = client.post(
        "/paper/buy",
        json={
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": 100,
            "price": 100000,
            "estimated_fee": 1,
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
    create_scenario()

    buy_response = client.post(
        "/paper/buy",
        json={
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": 100,
            "price": 100000,
            "estimated_fee": 1,
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
    create_scenario(initial_balance="100", max_order_amount="1000")

    response = client.post(
        "/paper/buy",
        json={
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": 500,
            "price": 100000,
            "estimated_fee": 1,
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
    create_scenario()

    response = client.post(
        "/paper/buy",
        json={
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": -100,
            "price": 100000,
            "estimated_fee": 1,
            "exchange": "MEXC",
        },
    )

    assert response.status_code == 422

    app.dependency_overrides.clear()


def test_paper_api_rejects_client_supplied_strategy_origin():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    create_scenario()

    response = client.post(
        "/paper/buy",
        json={
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": 100,
            "price": 100000,
            "estimated_fee": 1,
            "strategy_id": 42,
        },
    )

    assert response.status_code == 422
    assert client.get("/operations").json() == []
    app.dependency_overrides.clear()


def test_paper_scenario_owns_balance_and_limits():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    try:
        created = client.post(
            "/paper/scenarios",
            json={
                "quote_currency": "EUR",
                "initial_balance": "100",
                "max_order_amount": "70",
                "max_fee_percentage": "2",
                "daily_limit": "100",
                "monthly_limit": "100",
            },
        )
        assert created.status_code == 201
        listed = client.get("/paper/scenarios")
        assert listed.status_code == 200
        assert [item["quote_currency"] for item in listed.json()] == ["EUR"]

        first = client.post(
            "/paper/buy",
            json={
                "request_id": str(uuid4()),
                "asset": "BTC",
                "quote_currency": "EUR",
                "order_amount": "60",
                "price": "100000",
                "estimated_fee": "1",
            },
        )
        assert first.status_code == 200
        assert first.json()["executed"] is True

        scenario = client.get("/paper/scenarios/EUR")
        assert scenario.status_code == 200
        assert Decimal(str(scenario.json()["available_balance"])) == Decimal("40")

        second = client.post(
            "/paper/buy",
            json={
                "request_id": str(uuid4()),
                "asset": "BTC",
                "quote_currency": "EUR",
                "order_amount": "50",
                "price": "100000",
                "estimated_fee": "0",
            },
        )
        assert second.status_code == 200
        assert second.json()["executed"] is False
        assert second.json()["reason"] == "Insufficient budget"
        assert len(client.get("/operations").json()) == 1
    finally:
        app.dependency_overrides.clear()


def test_paper_buy_requires_scenario_and_rejects_client_limits():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    try:
        body = {
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": "50",
            "price": "100000",
        }
        missing = client.post("/paper/buy", json=body)
        assert missing.status_code == 404

        client.post(
            "/paper/scenarios",
            json={
                "quote_currency": "EUR",
                "initial_balance": "100",
                "max_order_amount": "50",
                "max_fee_percentage": "2",
            },
        )
        forged = client.post(
            "/paper/buy",
            json={**body, "available_budget": "9999"},
        )
        assert forged.status_code == 422
        assert client.get("/operations").json() == []
    finally:
        app.dependency_overrides.clear()


def test_paper_scenario_applies_order_and_daily_limits():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    try:
        create_scenario(initial_balance="500", max_order_amount="60", daily_limit="70")
        body = {
            "asset": "BTC",
            "quote_currency": "EUR",
            "price": "100000",
            "estimated_fee": "0",
        }

        too_large = client.post(
            "/paper/buy", json={**body, "request_id": str(uuid4()), "order_amount": "80"}
        )
        first = client.post(
            "/paper/buy", json={**body, "request_id": str(uuid4()), "order_amount": "50"}
        )
        over_day = client.post(
            "/paper/buy", json={**body, "request_id": str(uuid4()), "order_amount": "30"}
        )

        assert too_large.json()["reason"] == "Order exceeds maximum allowed amount"
        assert first.json()["executed"] is True
        assert over_day.json()["reason"] == "Daily spending limit exceeded"
        assert len(client.get("/operations").json()) == 1

        duplicate = client.post(
            "/paper/scenarios",
            json={
                "quote_currency": "EUR",
                "initial_balance": "500",
                "max_order_amount": "60",
                "max_fee_percentage": "2",
            },
        )
        assert duplicate.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_new_paper_scenario_starts_with_its_full_balance():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    try:
        historical = client.post(
            "/operations",
            json={
                "operation_type": "BUY",
                "asset": "BTC",
                "quote_currency": "EUR",
                "amount_spent": "90",
                "asset_received": "0.0009",
                "price": "100000",
                "mode": "PAPER",
            },
        )
        assert historical.status_code == 200

        create_scenario(initial_balance="100", max_order_amount="100", daily_limit="100")
        initial = client.get("/paper/scenarios/EUR")
        assert Decimal(str(initial.json()["available_balance"])) == Decimal("100")

        purchase = client.post(
            "/paper/buy",
            json={
                "request_id": str(uuid4()),
                "asset": "BTC",
                "quote_currency": "EUR",
                "order_amount": "20",
                "price": "100000",
            },
        )
        assert purchase.json()["executed"] is True
        final = client.get("/paper/scenarios/EUR")
        assert Decimal(str(final.json()["available_balance"])) == Decimal("80")
    finally:
        app.dependency_overrides.clear()


def test_paper_buy_retry_reuses_result_without_duplicate_operation():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    try:
        create_scenario()
        body = {
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": "50",
            "price": "100000",
            "estimated_fee": "0.5",
        }

        first = client.post("/paper/buy", json=body)
        repeated = client.post("/paper/buy", json=body)
        changed = client.post("/paper/buy", json={**body, "order_amount": "30"})

        assert first.status_code == 200
        assert repeated.status_code == 200
        assert repeated.json() == first.json()
        assert first.json()["operation_id"] is not None
        assert changed.status_code == 409
        assert len(client.get("/operations").json()) == 1
    finally:
        app.dependency_overrides.clear()


def test_paper_buy_retry_returns_same_rounded_result():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    try:
        create_scenario()
        body = {
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": "100",
            "price": "3",
        }

        first = client.post("/paper/buy", json=body)
        repeated = client.post("/paper/buy", json=body)

        assert first.status_code == 200
        assert repeated.json() == first.json()
        assert len(client.get("/operations").json()) == 1
    finally:
        app.dependency_overrides.clear()


def test_paper_buy_retry_preserves_blocked_decision():
    clear_database()
    app.dependency_overrides[get_db] = override_get_db
    try:
        create_scenario(initial_balance="20")
        body = {
            "request_id": str(uuid4()),
            "asset": "BTC",
            "quote_currency": "EUR",
            "order_amount": "30",
            "price": "100000",
        }

        first = client.post("/paper/buy", json=body)
        repeated = client.post("/paper/buy", json=body)

        assert first.status_code == 200
        assert first.json()["executed"] is False
        assert repeated.json() == first.json()
        assert client.get("/operations").json() == []
    finally:
        app.dependency_overrides.clear()
