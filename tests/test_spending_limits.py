from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.paper_trading import execute_and_record_paper_buy
from app.database.database import Base
from app.guard.risk import (
    check_daily_limit,
    check_monthly_limit,
)
from app.ledger.repository import (
    create_operation,
    get_daily_spent,
    get_monthly_spent,
)


def create_test_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Session = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

    return Session()


def test_daily_limit_allows_order():
    result = check_daily_limit(
        order_amount=Decimal("20"),
        daily_spent=Decimal("50"),
        daily_limit=Decimal("100"),
    )

    assert result.allowed is True


def test_daily_limit_blocks_order():
    result = check_daily_limit(
        order_amount=Decimal("60"),
        daily_spent=Decimal("50"),
        daily_limit=Decimal("100"),
    )

    assert result.allowed is False
    assert result.reason == "Daily spending limit exceeded"


def test_monthly_limit_blocks_order():
    result = check_monthly_limit(
        order_amount=Decimal("300"),
        monthly_spent=Decimal("800"),
        monthly_limit=Decimal("1000"),
    )

    assert result.allowed is False
    assert result.reason == "Monthly spending limit exceeded"


def test_daily_spent_reads_ledger():
    db = create_test_db()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("70"),
        asset_received=Decimal("0.0007"),
        price=Decimal("100000"),
        mode="PAPER",
    )

    spent = get_daily_spent(
        db=db,
        mode="PAPER",
    )

    assert spent == Decimal("70")

    db.close()


def test_monthly_spent_reads_ledger():
    db = create_test_db()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("250"),
        asset_received=Decimal("0.0025"),
        price=Decimal("100000"),
        mode="PAPER",
    )

    spent = get_monthly_spent(
        db=db,
        mode="PAPER",
    )

    assert spent == Decimal("250")

    db.close()


def test_paper_and_live_spending_are_separate():
    db = create_test_db()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("50"),
        asset_received=Decimal("0.0005"),
        price=Decimal("100000"),
        mode="PAPER",
    )

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("200"),
        asset_received=Decimal("0.002"),
        price=Decimal("100000"),
        mode="LIVE",
    )

    paper_spent = get_daily_spent(
        db=db,
        mode="PAPER",
    )

    live_spent = get_daily_spent(
        db=db,
        mode="LIVE",
    )

    assert paper_spent == Decimal("50")
    assert live_spent == Decimal("200")

    db.close()


def test_spending_limit_uses_only_the_requested_quote_currency():
    db = create_test_db()

    for currency, amount in (("EUR", "40"), ("USD", "90")):
        create_operation(
            db=db,
            operation_type="BUY",
            asset="BTC",
            quote_currency=currency,
            amount_spent=Decimal(amount),
            asset_received=Decimal("0.001"),
            price=Decimal("100000"),
            mode="PAPER",
        )

    assert get_daily_spent(db, mode="PAPER", quote_currency="EUR") == Decimal("40")
    assert get_monthly_spent(db, mode="PAPER", quote_currency="USD") == Decimal("90")

    db.close()


def test_paper_buy_does_not_mix_eur_and_usd_daily_limits():
    db = create_test_db()
    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="USD",
        amount_spent=Decimal("90"),
        asset_received=Decimal("0.001"),
        price=Decimal("90000"),
        mode="PAPER",
    )

    result = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("20"),
        price=Decimal("100000"),
        available_budget=Decimal("100"),
        max_order_amount=Decimal("100"),
        estimated_fee=Decimal("0"),
        max_fee_percentage=Decimal("2"),
        daily_limit=Decimal("50"),
    )

    assert result.executed is True
    assert get_daily_spent(db, mode="PAPER", quote_currency="EUR") == Decimal("20")
    db.close()


def test_previous_day_does_not_count_towards_today():
    db = create_test_db()

    operation = create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("80"),
        asset_received=Decimal("0.0008"),
        price=Decimal("100000"),
        mode="PAPER",
    )

    now = datetime.now(timezone.utc)

    operation.timestamp = now - timedelta(days=1)

    db.commit()

    spent = get_daily_spent(
        db=db,
        mode="PAPER",
        now=now,
    )

    assert spent == Decimal("0")

    db.close()


def test_paper_buy_blocked_by_accumulated_daily_limit():
    db = create_test_db()

    first = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("70"),
        price=Decimal("100000"),
        available_budget=Decimal("500"),
        max_order_amount=Decimal("200"),
        estimated_fee=Decimal("0"),
        max_fee_percentage=Decimal("2"),
        daily_limit=Decimal("100"),
        monthly_limit=Decimal("1000"),
    )

    assert first.executed is True

    second = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("50"),
        price=Decimal("100000"),
        available_budget=Decimal("500"),
        max_order_amount=Decimal("200"),
        estimated_fee=Decimal("0"),
        max_fee_percentage=Decimal("2"),
        daily_limit=Decimal("100"),
        monthly_limit=Decimal("1000"),
    )

    assert second.executed is False
    assert second.reason == "Daily spending limit exceeded"

    assert get_daily_spent(
        db=db,
        mode="PAPER"
    ) == Decimal("70")

    db.close()


def test_paper_buy_blocked_by_accumulated_monthly_limit():
    db = create_test_db()

    create_operation(
        db=db,
        operation_type="BUY",
        asset="BTC",
        quote_currency="EUR",
        amount_spent=Decimal("480"),
        asset_received=Decimal("0.0048"),
        price=Decimal("100000"),
        mode="PAPER",
    )

    result = execute_and_record_paper_buy(
        db=db,
        asset="BTC",
        quote_currency="EUR",
        order_amount=Decimal("50"),
        price=Decimal("100000"),
        available_budget=Decimal("1000"),
        max_order_amount=Decimal("200"),
        estimated_fee=Decimal("0"),
        max_fee_percentage=Decimal("2"),
        daily_limit=Decimal("1000"),
        monthly_limit=Decimal("500"),
    )

    assert result.executed is False
    assert result.reason == "Monthly spending limit exceeded"

    db.close()
