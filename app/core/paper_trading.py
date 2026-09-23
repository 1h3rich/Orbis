from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.guard.risk import evaluate_order
from app.ledger.repository import (
    create_operation,
    get_daily_spent,
    get_monthly_spent,
)


def to_decimal(value) -> Decimal:
    """
    Convierte números recibidos por Paper Trading
    a Decimal de forma segura.
    """
    return Decimal(str(value))


@dataclass
class PaperTradeResult:
    executed: bool
    reason: str
    asset: str
    amount_spent: Decimal
    asset_received: Decimal
    price: Decimal
    fee: Decimal


def execute_paper_buy(
    asset: str,
    order_amount,
    price,
    available_budget,
    max_order_amount,
    estimated_fee,
    max_fee_percentage,
) -> PaperTradeResult:
    """
    Simula una compra utilizando aritmética Decimal.
    """

    order_amount = to_decimal(order_amount)
    price = to_decimal(price)
    available_budget = to_decimal(available_budget)
    max_order_amount = to_decimal(max_order_amount)
    estimated_fee = to_decimal(estimated_fee)
    max_fee_percentage = to_decimal(max_fee_percentage)

    guard_result = evaluate_order(
        order_amount=order_amount,
        available_budget=available_budget,
        max_order_amount=max_order_amount,
        estimated_fee=estimated_fee,
        max_fee_percentage=max_fee_percentage,
    )

    if not guard_result.allowed:
        return PaperTradeResult(
            executed=False,
            reason=guard_result.reason,
            asset=asset,
            amount_spent=Decimal("0"),
            asset_received=Decimal("0"),
            price=price,
            fee=Decimal("0"),
        )

    if price <= 0:
        return PaperTradeResult(
            executed=False,
            reason="Asset price must be greater than zero",
            asset=asset,
            amount_spent=Decimal("0"),
            asset_received=Decimal("0"),
            price=price,
            fee=Decimal("0"),
        )

    net_amount = order_amount - estimated_fee
    asset_received = net_amount / price

    return PaperTradeResult(
        executed=True,
        reason="Paper trade executed",
        asset=asset,
        amount_spent=order_amount,
        asset_received=asset_received,
        price=price,
        fee=estimated_fee,
    )


def execute_and_record_paper_buy(
    db: Session,
    asset: str,
    quote_currency: str,
    order_amount,
    price,
    available_budget,
    max_order_amount,
    estimated_fee,
    max_fee_percentage,
    exchange: str | None = None,
    strategy_id: int | None = None,
    daily_limit=None,
    monthly_limit=None,
) -> PaperTradeResult:
    """
    Ejecuta una compra simulada teniendo en cuenta
    el historial real de operaciones PAPER del Ledger.
    """

    order_amount = to_decimal(order_amount)

    daily_spent = get_daily_spent(
        db=db,
        mode="PAPER",
    )

    monthly_spent = get_monthly_spent(
        db=db,
        mode="PAPER",
    )

    guard_result = evaluate_order(
        order_amount=order_amount,
        available_budget=available_budget,
        max_order_amount=max_order_amount,
        estimated_fee=estimated_fee,
        max_fee_percentage=max_fee_percentage,
        daily_spent=daily_spent,
        daily_limit=daily_limit,
        monthly_spent=monthly_spent,
        monthly_limit=monthly_limit,
    )

    if not guard_result.allowed:
        return PaperTradeResult(
            executed=False,
            reason=guard_result.reason,
            asset=asset,
            amount_spent=Decimal("0"),
            asset_received=Decimal("0"),
            price=to_decimal(price),
            fee=Decimal("0"),
        )

    result = execute_paper_buy(
        asset=asset,
        order_amount=order_amount,
        price=price,
        available_budget=available_budget,
        max_order_amount=max_order_amount,
        estimated_fee=estimated_fee,
        max_fee_percentage=max_fee_percentage,
    )

    if not result.executed:
        return result

    source = (
        "STRATEGY"
        if strategy_id is not None
        else "MANUAL"
    )

    create_operation(
        db=db,
        operation_type="BUY",
        asset=asset,
        quote_currency=quote_currency,
        amount_spent=result.amount_spent,
        asset_received=result.asset_received,
        price=result.price,
        trading_fee=result.fee,
        mode="PAPER",
        source=source,
        status="EXECUTED",
        exchange=exchange,
        strategy_id=strategy_id,
    )

    return result