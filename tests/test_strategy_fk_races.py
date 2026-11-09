"""Carreras del Ledger heredado contra una SQLite temporal con FK activas."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.database.database import get_db
from app.database.models import Operation, Strategy
from app.database.repository import StrategyInUseError, delete_strategy
from app.ledger.repository import create_operation


def _strategy(db):
    row = Strategy(name="Carrera", strategy_type="DCA", enabled=True, config={})
    db.add(row)
    db.commit()
    return row.id


def _operation(db, strategy_id):
    return create_operation(
        db=db, operation_type="BUY", asset="BTC", quote_currency="EUR",
        amount_spent=Decimal("100"), asset_received=Decimal("0.001"),
        price=Decimal("100000"), strategy_id=strategy_id,
    )


def test_concurrent_operation_prevents_strategy_delete(file_db_engine):
    sessions = sessionmaker(bind=file_db_engine, autoflush=False)
    with sessions() as setup:
        strategy_id = _strategy(setup)

    with sessions() as deleting:
        def insert_while_deleting(_session, _context, _instances):
            with sessions() as writer:
                _operation(writer, strategy_id)

        event.listen(deleting, "before_flush", insert_while_deleting, once=True)
        with pytest.raises(StrategyInUseError, match="desactívala"):
            delete_strategy(deleting, strategy_id)

    with sessions() as verify:
        assert verify.get(Strategy, strategy_id) is not None
        assert verify.query(Operation).filter_by(strategy_id=strategy_id).count() == 1
    with file_db_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_strategy_deleted_between_api_check_and_operation_insert(
    file_db_engine, monkeypatch,
):
    sessions = sessionmaker(bind=file_db_engine, autoflush=False)
    with sessions() as setup:
        strategy_id = _strategy(setup)

    def test_db():
        with sessions() as db:
            yield db

    main_module.app.dependency_overrides[get_db] = test_db
    original_create = main_module.create_operation

    def delete_then_insert(**kwargs):
        with sessions() as writer:
            writer.delete(writer.get(Strategy, strategy_id))
            writer.commit()
        return original_create(**kwargs)

    monkeypatch.setattr(main_module, "create_operation", delete_then_insert)
    with TestClient(main_module.app) as client:
        response = client.post("/operations", json={
            "operation_type": "BUY", "asset": "BTC", "quote_currency": "EUR",
            "amount_spent": "100", "asset_received": "0.001", "price": "100000",
            "strategy_id": strategy_id,
        })
    assert response.status_code == 409
    assert "no se registró nada" in response.json()["detail"]
    with sessions() as verify:
        assert verify.get(Strategy, strategy_id) is None
        assert verify.query(Operation).count() == 0
