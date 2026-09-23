from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.paper_scenarios import paper_scenario_snapshot
from app.database.models import PaperRequest, PaperScenario
from app.database.repository import get_strategy
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
    operation_id: int | None = None


class PaperRequestConflict(ValueError):
    """El mismo identificador se ha reutilizado con una compra diferente."""


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
    since: datetime | None = None,
    commit: bool = True,
) -> PaperTradeResult:
    """
    Ejecuta una compra simulada teniendo en cuenta
    el historial real de operaciones PAPER del Ledger.
    """

    order_amount = to_decimal(order_amount)

    if strategy_id is not None:
        strategy = get_strategy(db, strategy_id)
        if strategy is None or not strategy.enabled:
            return PaperTradeResult(
                executed=False,
                reason="Strategy not found or disabled",
                asset=asset,
                amount_spent=Decimal("0"),
                asset_received=Decimal("0"),
                price=to_decimal(price),
                fee=Decimal("0"),
            )

    daily_spent = get_daily_spent(
        db=db,
        mode="PAPER",
        quote_currency=quote_currency,
        since=since,
    )

    monthly_spent = get_monthly_spent(
        db=db,
        mode="PAPER",
        quote_currency=quote_currency,
        since=since,
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

    operation = create_operation(
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
        commit=commit,
    )

    result.operation_id = operation.id
    return result


def execute_configured_paper_buy(
    db: Session,
    scenario: PaperScenario,
    request_id: str,
    asset: str,
    order_amount,
    price,
    estimated_fee,
    exchange: str | None = None,
) -> PaperTradeResult:
    """Evalúa una compra una sola vez y guarda el resultado junto a la operación."""
    payload = {
        "asset": asset,
        "quote_currency": scenario.quote_currency,
        "order_amount": str(to_decimal(order_amount)),
        "price": str(to_decimal(price)),
        "estimated_fee": str(to_decimal(estimated_fee)),
        "exchange": exchange,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    def previous_result(record: PaperRequest) -> PaperTradeResult:
        if record.payload_digest != digest:
            raise PaperRequestConflict("Request ID already used for another purchase")
        return PaperTradeResult(
            executed=record.executed,
            reason=record.reason,
            asset=record.asset,
            amount_spent=record.amount_spent,
            asset_received=record.asset_received,
            price=record.price,
            fee=record.fee,
            operation_id=record.operation_id,
        )

    existing = db.get(PaperRequest, request_id)
    if existing is not None:
        return previous_result(existing)

    snapshot = paper_scenario_snapshot(db, scenario)
    result = execute_and_record_paper_buy(
        db=db,
        asset=asset,
        quote_currency=scenario.quote_currency,
        order_amount=order_amount,
        price=price,
        available_budget=snapshot["available_balance"],
        max_order_amount=scenario.max_order_amount,
        estimated_fee=estimated_fee,
        max_fee_percentage=scenario.max_fee_percentage,
        exchange=exchange,
        daily_limit=scenario.daily_limit,
        monthly_limit=scenario.monthly_limit,
        since=scenario.created_at,
        commit=False,
    )

    record = PaperRequest(
        request_id=request_id,
        payload_digest=digest,
        executed=result.executed,
        reason=result.reason,
        asset=result.asset,
        amount_spent=result.amount_spent,
        asset_received=result.asset_received,
        price=result.price,
        fee=result.fee,
        operation_id=result.operation_id,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(PaperRequest, request_id)
        if existing is None:
            raise
        return previous_result(existing)

    db.refresh(record)
    return previous_result(record)
