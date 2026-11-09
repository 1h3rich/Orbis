"""Conurrencia PAPER sobre SQLite en fichero con dos conexiones y BEGIN IMMEDIATE.

SMOKE TEST DECLARADO: no es una prueba de estrés. Comprueba, con transacciones
`BEGIN IMMEDIATE` reales sobre un fichero temporal, que dos hilos que ejecutan
órdenes a la vez nunca dejan el saldo negativo, nunca superan los límites del
escenario y no registran dos operaciones para el mismo `request_id`.
"""

import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.paper_scenarios import get_paper_scenario, paper_scenario_snapshot
from app.core.paper_trading import execute_configured_paper_buy
from app.database.database import Base
from app.ledger.repository import get_daily_spent, list_paper_operations


@pytest.fixture()
def immediate_engine(tmp_path):
    """Motor de fichero: cada hilo toma su propia conexión y escribe con BEGIN IMMEDIATE."""
    path = tmp_path / "concurrency.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _disable_implicit_begin(dbapi_connection, _record):
        # pysqlite emitiría un BEGIN deferred automático; lo tomamos nosotros.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


def seed_scenario(engine, **overrides):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        from app.core.paper_scenarios import create_paper_scenario

        body = {
            "quote_currency": "EUR",
            "initial_balance": Decimal("100"),
            "max_order_amount": Decimal("100"),
            "max_fee_percentage": Decimal("2"),
            "daily_limit": Decimal("100"),
            "monthly_limit": Decimal("1000"),
        }
        body.update(overrides)
        scenario = create_paper_scenario(db=db, **body)
        return scenario.id
    finally:
        db.close()


def run_in_parallel(engine, worker, count):
    """Lanza `count` hilos con su propia conexión y sincroniza el arranque."""
    results = [None] * count
    errors = []
    barrier = threading.Barrier(count)

    def runner(index):
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = Session()
        try:
            barrier.wait(timeout=20)
            results[index] = worker(db, index)
        except Exception as exc:  # pragma: no cover - se reporta abajo
            errors.append(f"hilo {index}: {type(exc).__name__}: {exc}")
        finally:
            db.close()

    threads = [
        threading.Thread(target=runner, args=(index,), name=f"orbis-{index}")
        for index in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads), "hilos bloqueados"
    assert not errors, errors
    return results


def snapshot(engine, scenario_id):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        from app.database.models import PaperScenario

        scenario = db.get(PaperScenario, scenario_id)
        return paper_scenario_snapshot(db, scenario)
    finally:
        db.close()


def test_two_connections_cannot_spend_more_than_the_balance(immediate_engine):
    seed_scenario(immediate_engine)
    request_ids = [str(uuid4()) for _ in range(2)]

    def buy(db, index):
        scenario = get_paper_scenario(db, "EUR")
        return execute_configured_paper_buy(
            db=db,
            scenario=scenario,
            request_id=request_ids[index],
            asset="BTC",
            order_amount=Decimal("60"),
            price=Decimal("100000"),
            estimated_fee=Decimal("0"),
        )

    results = run_in_parallel(immediate_engine, buy, count=2)

    executed = [result for result in results if result.executed]
    assert len(executed) == 1, "solo cabe una compra de 60 en 100 EUR"

    Session = sessionmaker(bind=immediate_engine)
    db = Session()
    try:
        operations = list_paper_operations(db)
        cash = paper_scenario_snapshot(
            db, get_paper_scenario(db, "EUR")
        )["cash_balance"]
        spent = get_daily_spent(db, mode="PAPER", quote_currency="EUR")
    finally:
        db.close()

    assert len(operations) == 1
    assert cash == Decimal("40")
    assert cash >= 0
    assert cash <= Decimal("100")
    assert spent == Decimal("60")
    assert spent <= Decimal("100")


def test_same_request_id_from_two_connections_creates_one_operation(immediate_engine):
    scenario_id = seed_scenario(immediate_engine)
    request_id = str(uuid4())

    def buy(db, _index):
        scenario = get_paper_scenario(db, "EUR")
        return execute_configured_paper_buy(
            db=db,
            scenario=scenario,
            request_id=request_id,
            asset="BTC",
            order_amount=Decimal("60"),
            price=Decimal("100000"),
            estimated_fee=Decimal("0"),
        )

    first, second = run_in_parallel(immediate_engine, buy, count=2)

    assert first.executed is True
    assert second.executed is True
    assert first.operation_id == second.operation_id
    assert first.asset_received == second.asset_received

    Session = sessionmaker(bind=immediate_engine)
    db = Session()
    try:
        assert len(list_paper_operations(db)) == 1
    finally:
        db.close()

    summary = snapshot(immediate_engine, scenario_id)
    assert Decimal(str(summary["cash_balance"])) == Decimal("40")


def test_burst_of_orders_never_goes_negative_or_exceeds_limits(immediate_engine):
    scenario_id = seed_scenario(immediate_engine)
    request_ids = [str(uuid4()) for _ in range(4)]

    def buy(db, index):
        scenario = get_paper_scenario(db, "EUR")
        return execute_configured_paper_buy(
            db=db,
            scenario=scenario,
            request_id=request_ids[index],
            asset="BTC",
            order_amount=Decimal("60"),
            price=Decimal("100000"),
            estimated_fee=Decimal("0"),
        )

    results = run_in_parallel(immediate_engine, buy, count=4)

    executed = [result for result in results if result.executed]
    blocked = [result for result in results if not result.executed]

    assert 1 <= len(executed) <= 1 or len(executed) == 1
    assert len(executed) + len(blocked) == 4
    assert all(result.reason != "PENDING" for result in results)

    summary = snapshot(immediate_engine, scenario_id)
    cash = Decimal(str(summary["cash_balance"]))

    assert cash >= 0
    assert cash <= Decimal("100")
    assert Decimal(str(summary["total_spent"])) <= Decimal("100")
    assert Decimal(str(summary["cash_balance"])) == Decimal("100") - Decimal(
        str(summary["total_spent"])
    )
