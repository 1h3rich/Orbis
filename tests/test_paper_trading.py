from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.paper_trading import (
    execute_paper_buy,
    execute_and_record_paper_buy,
)
from app.database.database import Base
from app.database.models import Strategy
from app.ledger.repository import get_operations


def create_test_db():
    """
    Crea una base de datos temporal independiente
    para una prueba de Paper Trading.
    """

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

    return Session()


def test_paper_buy_executes_safe_order():
    result = execute_paper_buy(
        asset="BTC",
        order_amount=Decimal("100"),
        price=Decimal("100000"),
        available_budget=Decimal("500"),
        max_order_amount=Decimal("200"),
        estimated_fee=Decimal("1"),
        max_fee_percentage=Decimal("2"),
    )

    assert result.executed is True
    assert result.reason == "Paper trade executed"

    assert result.amount_spent == Decimal("100")
    assert result.asset_received == Decimal("0.00099")
    assert result.fee == Decimal("1")


def test_paper_buy_blocked_by_guard():
    result = execute_paper_buy(
        asset="BTC",
        order_amount=Decimal("200"),
        price=Decimal("100000"),
        available_budget=Decimal("100"),
        max_order_amount=Decimal("500"),
        estimated_fee=Decimal("1"),
        max_fee_percentage=Decimal("2"),
    )

    assert result.executed is False
    assert result.reason == "Insufficient budget"
    assert result.asset_received == Decimal("0")


def test_paper_buy_blocks_invalid_price():
    result = execute_paper_buy(
        asset="BTC",
        order_amount=Decimal("100"),
        price=Decimal("0"),
        available_budget=Decimal("500"),
        max_order_amount=Decimal("200"),
        estimated_fee=Decimal("1"),
        max_fee_percentage=Decimal("2"),
    )

    assert result.executed is False
    assert result.reason == "Asset price must be greater than zero"


def test_paper_buy_is_recorded_in_ledger():
    db = create_test_db()

    result = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("100"),
        price=Decimal("100000"),
        available_budget=Decimal("500"),
        max_order_amount=Decimal("200"),
        estimated_fee=Decimal("1"),
        max_fee_percentage=Decimal("2"),
        exchange="MEXC",
    )

    operations = get_operations(db)

    assert result.executed is True
    assert len(operations) == 1

    operation = operations[0]

    assert operation.operation_type == "BUY"
    assert operation.mode == "PAPER"
    assert operation.source == "MANUAL"
    assert operation.status == "EXECUTED"
    assert operation.exchange == "MEXC"

    assert operation.asset == "BTC"
    assert operation.quote_currency == "EUR"

    assert operation.amount_spent == Decimal(
        "100.000000000000"
    )

    assert operation.asset_received == Decimal(
        "0.000990000000"
    )

    assert operation.timestamp is not None

    db.close()


def test_blocked_paper_buy_is_not_recorded():
    db = create_test_db()

    result = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("500"),
        price=Decimal("100000"),
        available_budget=Decimal("100"),
        max_order_amount=Decimal("1000"),
        estimated_fee=Decimal("1"),
        max_fee_percentage=Decimal("2"),
    )

    operations = get_operations(db)

    assert result.executed is False
    assert len(operations) == 0

    db.close()


def test_strategy_paper_buy_records_strategy_source():
    db = create_test_db()

    strategy = Strategy(
        name="BTC Weekly DCA",
        strategy_type="DCA",
        enabled=True,
        config={
            "amount": 50,
            "frequency": "weekly",
        },
    )

    db.add(strategy)
    db.commit()
    db.refresh(strategy)

    result = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("50"),
        price=Decimal("100000"),
        available_budget=Decimal("500"),
        max_order_amount=Decimal("100"),
        estimated_fee=Decimal("0.50"),
        max_fee_percentage=Decimal("2"),
        exchange="MEXC",
        strategy_id=strategy.id,
    )

    operations = get_operations(db)

    assert result.executed is True
    assert len(operations) == 1

    operation = operations[0]

    assert operation.source == "STRATEGY"
    assert operation.strategy_id == strategy.id
    assert operation.mode == "PAPER"

    db.close()


def test_strategy_paper_buy_rejects_unknown_strategy():
    db = create_test_db()

    result = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("50"),
        price=Decimal("100000"),
        available_budget=Decimal("500"),
        max_order_amount=Decimal("100"),
        estimated_fee=Decimal("0.50"),
        max_fee_percentage=Decimal("2"),
        strategy_id=999,
    )

    assert result.executed is False
    assert result.reason == "Strategy not found or disabled"
    assert get_operations(db) == []
    db.close()
