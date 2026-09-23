from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.paper_trading import execute_paper_buy
from app.database.database import Base
from app.ledger.repository import create_operation


def test_decimal_basic_precision():
    """
    Decimal debe representar correctamente operaciones
    que con float producen errores binarios.
    """

    result = Decimal("0.1") + Decimal("0.2")

    assert result == Decimal("0.3")


def test_paper_trading_uses_decimal():
    """
    Paper Trading debe calcular cantidades utilizando
    Decimal y no float.
    """

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

    assert isinstance(
        result.asset_received,
        Decimal
    )

    assert result.asset_received == Decimal("0.00099")


def test_ledger_stores_decimal_values():
    """
    Las cantidades recuperadas del Ledger deben
    seguir siendo Decimal.
    """

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

    db = Session()

    operation = create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("100.10"),
        asset_received=Decimal("0.001001"),
        price=Decimal("100000"),
        trading_fee=Decimal("0.25"),
    )

    assert isinstance(operation.amount_spent, Decimal)
    assert isinstance(operation.asset_received, Decimal)
    assert isinstance(operation.trading_fee, Decimal)

    assert operation.amount_spent == Decimal("100.100000000000")
    assert operation.asset_received == Decimal("0.001001000000")
    assert operation.trading_fee == Decimal("0.250000000000")

    db.close()


def test_decimal_total_cost_precision():
    """
    Comprueba que el coste total no introducezca
    errores de coma flotante.
    """

    from app.ledger.models import Operation

    operation = Operation(
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("0.1"),
        asset_received=Decimal("0.000001"),
        price=Decimal("100000"),
        trading_fee=Decimal("0.2"),
    )

    assert operation.total_cost() == Decimal("0.3")