"""Sesiones PAPER auditables y comprobación de esquema (scripts/check_schema.py)."""

import importlib.util
import sqlite3
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.database.database import Base
from app.database.schema_check import schema_report

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Esquema anterior a las sesiones: UNIQUE(quote_currency), sin `generation`,
#: sin `closed_at` y sin la tabla `price_quotes`.
LEGACY_SCHEMA = [
    """
    CREATE TABLE strategies (
        id INTEGER NOT NULL,
        name VARCHAR NOT NULL,
        strategy_type VARCHAR NOT NULL,
        enabled BOOLEAN NOT NULL,
        config JSON NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE operations (
        id INTEGER NOT NULL,
        timestamp DATETIME NOT NULL,
        operation_type VARCHAR NOT NULL,
        mode VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        exchange VARCHAR,
        strategy_id INTEGER,
        asset VARCHAR NOT NULL,
        quote_currency VARCHAR NOT NULL,
        amount_spent NUMERIC(30, 12) NOT NULL,
        asset_received NUMERIC(30, 12) NOT NULL,
        price NUMERIC(30, 12) NOT NULL,
        trading_fee NUMERIC(30, 12) NOT NULL,
        withdrawal_fee NUMERIC(30, 12) NOT NULL,
        network_fee NUMERIC(30, 12) NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(strategy_id) REFERENCES strategies (id)
    )
    """,
    """
    CREATE TABLE paper_scenarios (
        id INTEGER NOT NULL,
        quote_currency VARCHAR(12) NOT NULL,
        initial_balance NUMERIC(30, 12) NOT NULL,
        max_order_amount NUMERIC(30, 12) NOT NULL,
        max_fee_percentage NUMERIC(10, 4) NOT NULL,
        daily_limit NUMERIC(30, 12),
        monthly_limit NUMERIC(30, 12),
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (quote_currency)
    )
    """,
    """
    CREATE TABLE paper_requests (
        request_id VARCHAR(36) NOT NULL,
        payload_digest VARCHAR(64) NOT NULL,
        executed BOOLEAN NOT NULL,
        reason VARCHAR NOT NULL,
        asset VARCHAR NOT NULL,
        amount_spent NUMERIC(30, 12) NOT NULL,
        asset_received NUMERIC(30, 12) NOT NULL,
        price NUMERIC(30, 12) NOT NULL,
        fee NUMERIC(30, 12) NOT NULL,
        operation_id INTEGER,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (request_id),
        FOREIGN KEY(operation_id) REFERENCES operations (id)
    )
    """,
]


def load_check_schema_module():
    path = REPO_ROOT / "scripts" / "check_schema.py"
    spec = importlib.util.spec_from_file_location("orbis_check_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_legacy_db(tmp_path) -> str:
    path = tmp_path / "orbis-legacy.db"
    connection = sqlite3.connect(path)
    for statement in LEGACY_SCHEMA:
        connection.execute(statement)
    connection.commit()
    connection.close()
    return f"sqlite:///{path}"


def buy(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "50",
        "price": "100000",
        "estimated_fee": "0",
    }
    body.update(overrides)
    response = client.post("/paper/buy", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_new_session_reports_its_generation_and_open_state(client, create_scenario):
    created = create_scenario()

    assert created["generation"] == 1
    assert created["is_open"] is True
    assert created["closed_at"] is None
    assert Decimal(str(created["cash_balance"])) == Decimal("1000")


def test_second_session_is_rejected_while_the_first_is_open(client, create_scenario):
    create_scenario()
    duplicate = client.post(
        "/paper/scenarios",
        json={
            "quote_currency": "EUR",
            "initial_balance": "500",
            "max_order_amount": "100",
            "max_fee_percentage": "2",
        },
    )

    assert duplicate.status_code == 409
    assert client.get("/paper/sessions").json()[0]["generation"] == 1


def test_closing_a_session_keeps_its_history(client, create_scenario):
    create_scenario()
    buy(client)

    closed = client.post("/paper/scenarios/EUR/close")
    assert closed.status_code == 200
    assert closed.json()["is_open"] is False
    assert closed.json()["closed_at"] is not None

    # La sesión abierta desaparece del listado, pero no del histórico
    assert client.get("/paper/scenarios").json() == []
    sessions = client.get("/paper/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["is_open"] is False
    assert Decimal(str(sessions[0]["cash_balance"])) == Decimal("950")

    # Las operaciones siguen visibles y ya no hay sesión que acepte órdenes
    assert len(client.get("/paper/operations").json()) == 1
    blocked = client.post("/paper/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.001", "price": "100000", "fee": "0",
    })
    assert blocked.status_code == 404


def test_new_session_starts_with_full_balance_and_visible_history(client, create_scenario):
    create_scenario()
    buy(client)
    client.post("/paper/scenarios/EUR/close")

    second = create_scenario()

    assert second["generation"] == 2
    assert Decimal(str(second["available_balance"])) == Decimal("1000")
    assert Decimal(str(second["cash_balance"])) == Decimal("1000")

    # El historial de la sesión anterior sigue en el Ledger
    operations = client.get("/paper/operations").json()
    assert len(operations) == 1
    assert Decimal(str(operations[0]["amount_spent"])) == Decimal("50")

    # ...y la sesión antigua conserva sus saldos
    sessions = {item["generation"]: item for item in client.get("/paper/sessions").json()}
    assert Decimal(str(sessions[1]["cash_balance"])) == Decimal("950")
    assert Decimal(str(sessions[2]["cash_balance"])) == Decimal("1000")

    # La nueva sesión no arrastra el gasto de la anterior
    scenario = client.get("/paper/scenarios/EUR").json()
    assert Decimal(str(scenario["total_spent"])) == Decimal("0")


def test_operations_of_the_new_session_do_not_touch_the_old_one(client, create_scenario):
    create_scenario()
    buy(client)
    client.post("/paper/scenarios/EUR/close")
    create_scenario()
    buy(client, order_amount="80")

    sessions = {item["generation"]: item for item in client.get("/paper/sessions").json()}
    assert Decimal(str(sessions[1]["cash_balance"])) == Decimal("950")
    assert Decimal(str(sessions[2]["cash_balance"])) == Decimal("920")
    assert len(client.get("/paper/operations").json()) == 2


def test_closing_twice_or_unknown_session_is_not_found(client, create_scenario):
    create_scenario()
    client.post("/paper/scenarios/EUR/close")

    assert client.post("/paper/scenarios/EUR/close").status_code == 404
    assert client.post("/paper/scenarios/JPY/close").status_code == 404


def test_check_schema_passes_on_a_new_database(tmp_path):
    from sqlalchemy import create_engine

    url = f"sqlite:///{tmp_path}/fresh.db"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    try:
        pending, notes = schema_report(engine)
    finally:
        engine.dispose()

    assert pending == []


def test_check_schema_lists_pending_changes_on_a_legacy_database(tmp_path, capsys):
    module = load_check_schema_module()
    url = create_legacy_db(tmp_path)

    exit_code = module.main(["--url", url])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "falta la columna generation" in output
    assert "falta la columna closed_at" in output
    assert "UNIQUE antigua" in output
    assert "uq_paper_scenarios_open_quote" in output
    assert "falta la tabla price_quotes" in output
    # Documenta el DDL manual que create_all no puede aplicar
    assert "ALTER TABLE paper_scenarios ADD COLUMN generation" in output
    assert "CREATE UNIQUE INDEX" in output


def test_check_schema_reports_success_on_a_fresh_database(tmp_path, capsys):
    from sqlalchemy import create_engine

    module = load_check_schema_module()
    url = f"sqlite:///{tmp_path}/ok.db"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    exit_code = module.main(["--url", url])

    assert exit_code == 0
    assert "Esquema correcto" in capsys.readouterr().out


def test_check_schema_script_is_wired_into_the_suite():
    """La suite falla si el esquema preparado al arrancar no cuadra con los modelos."""
    from app.database.schema_check import collect_schema_diff
    from app.database.database import engine

    assert collect_schema_diff(engine) == []
