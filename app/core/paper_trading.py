from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, localcontext
import hashlib
import json
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.orders import OrderIntent, to_decimal
from app.core.paper_scenarios import paper_scenario_snapshot
from app.database.models import PaperRequest, PaperScenario
from app.database.repository import get_strategy
from app.guard.risk import evaluate_order, evaluate_sell
from app.ledger.repository import (
    create_operation,
    get_daily_spent,
    get_monthly_spent,
    list_paper_operations,
)
from app.math.positions import build_positions, position_by_asset


BUY = "BUY"
SELL = "SELL"

BLOCKED_AMOUNT = Decimal("0")
LEDGER_MIN_UNIT = Decimal("0.000000000001")
MAX_BUY_ROUNDING_FRACTION = Decimal("0.0001")  # 0,01 % del neto.


def _fits_ledger(value: Decimal) -> bool:
    """El libro heredado usa Numeric(30, 12): rechazar pérdidas de escala."""
    if not value.is_finite():
        return False
    with localcontext() as context:
        context.prec = 80
        normalized = value.normalize()
    return normalized.as_tuple().exponent >= -12 and (
        normalized == 0 or normalized.adjusted() <= 17
    )


def _ledger_floor(value: Decimal) -> Decimal:
    """Trunca a la escala del libro: nunca crea activo o caja por redondeo."""
    with localcontext() as context:
        context.prec = 40  # Numeric(30, 12) admite hasta 30 dígitos.
        return value.quantize(LEDGER_MIN_UNIT, rounding=ROUND_DOWN)


@dataclass
class PaperTradeResult:
    executed: bool
    reason: str
    side: str
    asset: str
    amount_spent: Decimal
    asset_received: Decimal
    price: Decimal
    fee: Decimal
    operation_id: int | None = None


class PaperRequestConflict(ValueError):
    """El mismo identificador se ha reutilizado con una propuesta diferente."""


def _result(
    side: str,
    asset: str,
    price,
    executed: bool,
    reason: str,
    amount_spent=BLOCKED_AMOUNT,
    asset_received=BLOCKED_AMOUNT,
    fee=BLOCKED_AMOUNT,
    operation_id: int | None = None,
) -> PaperTradeResult:
    """Construye el resultado de una operación con importes siempre en Decimal."""
    return PaperTradeResult(
        executed=executed,
        reason=reason,
        side=side,
        asset=asset,
        amount_spent=to_decimal(amount_spent),
        asset_received=to_decimal(asset_received),
        price=to_decimal(price),
        fee=to_decimal(fee),
        operation_id=operation_id,
    )


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

    El precio no válido lo rechaza Guard (``check_price``); el núcleo ya no
    valida precio por su cuenta (H6).
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
        price=price,
    )

    if not guard_result.allowed:
        return _result(
            side=BUY,
            asset=asset,
            price=price,
            executed=False,
            reason=guard_result.reason,
        )

    if not all(_fits_ledger(value) for value in (order_amount, price, estimated_fee)):
        return _result(
            side=BUY, asset=asset, price=price,
            executed=False, reason="Order input outside ledger precision",
        )

    with localcontext() as context:
        context.prec = 80
        net_amount = order_amount - estimated_fee
        asset_received = net_amount / price
    if asset_received < LEDGER_MIN_UNIT:
        return _result(
            side=BUY, asset=asset, price=price,
            executed=False, reason="Order quantity below ledger precision",
        )
    if asset_received.adjusted() > 17:
        return _result(
            side=BUY, asset=asset, price=price,
            executed=False, reason="Order quantity outside ledger precision",
        )
    asset_received = _ledger_floor(asset_received)
    with localcontext() as context:
        context.prec = 80
        rounding_loss = net_amount - asset_received * price
        if rounding_loss > net_amount * MAX_BUY_ROUNDING_FRACTION:
            return _result(
                side=BUY, asset=asset, price=price,
                executed=False, reason="Order rounding loss exceeds limit",
            )

    return _result(
        side=BUY,
        asset=asset,
        price=price,
        executed=True,
        reason="Paper trade executed",
        amount_spent=order_amount,
        asset_received=asset_received,
        fee=estimated_fee,
    )


def execute_paper_sell(
    asset: str,
    quantity,
    price,
    available_quantity,
    estimated_fee,
    max_fee_percentage,
) -> PaperTradeResult:
    """
    Simula una venta con aritmética Decimal.

    El neto acreditado es ``quantity * price - fee`` y las unidades vendidas
    son ``quantity``.
    """

    quantity = to_decimal(quantity)
    price = to_decimal(price)
    available_quantity = to_decimal(available_quantity)
    estimated_fee = to_decimal(estimated_fee)
    max_fee_percentage = to_decimal(max_fee_percentage)

    guard_result = evaluate_sell(
        quantity=quantity,
        price=price,
        estimated_fee=estimated_fee,
        max_fee_percentage=max_fee_percentage,
        available_quantity=available_quantity,
    )

    if not guard_result.allowed:
        return _result(
            side=SELL,
            asset=asset,
            price=price,
            executed=False,
            reason=guard_result.reason,
        )

    if quantity < LEDGER_MIN_UNIT:
        return _result(
            side=SELL, asset=asset, price=price,
            executed=False, reason="Order quantity below ledger precision",
        )
    if not all(_fits_ledger(value) for value in (quantity, price, estimated_fee)):
        return _result(
            side=SELL, asset=asset, price=price,
            executed=False, reason="Order input outside ledger precision",
        )

    with localcontext() as context:
        context.prec = 80
        net_amount = quantity * price - estimated_fee
    if net_amount < LEDGER_MIN_UNIT:
        return _result(
            side=SELL, asset=asset, price=price,
            executed=False, reason="Order proceeds below ledger precision",
        )
    if net_amount.adjusted() > 17:
        return _result(
            side=SELL, asset=asset, price=price,
            executed=False, reason="Order proceeds outside ledger precision",
        )
    net_amount = _ledger_floor(net_amount)

    return _result(
        side=SELL,
        asset=asset,
        price=price,
        executed=True,
        reason="Paper trade executed",
        amount_spent=net_amount,
        asset_received=quantity,
        fee=estimated_fee,
    )


def session_positions(
    db: Session,
    scenario: PaperScenario,
) -> dict:
    """Posiciones vivas de la sesión PAPER, indexadas por activo."""
    operations = list_paper_operations(
        db,
        quote_currency=scenario.quote_currency,
        start=scenario.created_at,
        end=scenario.closed_at,
    )
    positions = build_positions(operations, mode="PAPER")
    return position_by_asset(positions)


def _strategy_source(db: Session, strategy_id: int | None, source: str) -> tuple[bool, str]:
    """Devuelve (válido, motivo). Una estrategia desconocida o desactivada bloquea."""
    if strategy_id is None:
        return True, "MANUAL"

    strategy = get_strategy(db, strategy_id)
    if strategy is None or not strategy.enabled:
        return False, "Strategy not found or disabled"
    return True, "STRATEGY"


def _evaluate_and_record(
    db: Session,
    scenario: PaperScenario,
    intent: OrderIntent,
) -> PaperTradeResult:
    """
    Relee saldo e inventario **dentro de la transacción abierta**, aplica Guard
    y, si todo pasa, inserta la operación en el Ledger sin confirmar.
    """

    valid, reason = _strategy_source(db, intent.strategy_id, intent.source)
    if not valid:
        return _result(
            side=intent.side,
            asset=intent.asset,
            price=intent.price,
            executed=False,
            reason=reason,
        )

    source = "STRATEGY" if intent.strategy_id is not None else "MANUAL"

    if intent.side == SELL:
        positions = session_positions(db, scenario)
        current = positions.get(intent.asset)
        available_quantity = current.quantity if current is not None else Decimal("0")

        result = execute_paper_sell(
            asset=intent.asset,
            quantity=intent.quantity,
            price=intent.price,
            available_quantity=available_quantity,
            estimated_fee=intent.fee,
            max_fee_percentage=scenario.max_fee_percentage,
        )
        operation_type = SELL
    else:
        snapshot = paper_scenario_snapshot(db, scenario)

        result = execute_and_record_paper_buy(
            db=db,
            asset=intent.asset,
            quote_currency=intent.quote_currency,
            order_amount=intent.order_amount,
            price=intent.price,
            available_budget=snapshot["cash_balance"],
            max_order_amount=scenario.max_order_amount,
            estimated_fee=intent.fee,
            max_fee_percentage=scenario.max_fee_percentage,
            exchange=intent.exchange,
            strategy_id=intent.strategy_id,
            daily_limit=scenario.daily_limit,
            monthly_limit=scenario.monthly_limit,
            since=scenario.created_at,
            commit=False,
            side=intent.side,
            halted=False,
        )
        return result

    if not result.executed:
        return result

    operation = create_operation(
        db=db,
        operation_type=operation_type,
        asset=intent.asset,
        quote_currency=intent.quote_currency,
        amount_spent=result.amount_spent,
        asset_received=result.asset_received,
        price=result.price,
        trading_fee=result.fee,
        mode="PAPER",
        source=source,
        status="EXECUTED",
        exchange=intent.exchange,
        strategy_id=intent.strategy_id,
        commit=False,
    )
    result.operation_id = operation.id
    return result


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
    side: str = BUY,
    halted: bool = False,
) -> PaperTradeResult:
    """
    Ejecuta una compra simulada teniendo en cuenta
    el historial real de operaciones PAPER del Ledger.

    Se conserva la firma histórica (todos los parámetros originales siguen
    siendo posicionales y con los mismos valores por defecto); ``side`` y
    ``halted`` son opcionales.
    """

    order_amount = to_decimal(order_amount)

    if strategy_id is not None:
        strategy = get_strategy(db, strategy_id)
        if strategy is None or not strategy.enabled:
            return _result(
                side=side,
                asset=asset,
                price=price,
                executed=False,
                reason="Strategy not found or disabled",
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
        price=price,
        halted=halted,
    )

    if not guard_result.allowed:
        return _result(
            side=side,
            asset=asset,
            price=price,
            executed=False,
            reason=guard_result.reason,
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

    if strategy_id is None:
        source = "MANUAL"
    else:
        source = "STRATEGY"

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


def _request_payload(intent: OrderIntent) -> dict:
    """Payload canónico de la propuesta, sin ``side`` (para el digesto legado)."""
    if intent.side == SELL:
        return {
            "asset": intent.asset,
            "quote_currency": intent.quote_currency,
            "quantity": str(to_decimal(intent.quantity)),
            "price": str(to_decimal(intent.price)),
            "estimated_fee": str(to_decimal(intent.fee)),
            "exchange": intent.exchange,
        }

    return {
        "asset": intent.asset,
        "quote_currency": intent.quote_currency,
        "order_amount": str(to_decimal(intent.order_amount)),
        "price": str(to_decimal(intent.price)),
        "estimated_fee": str(to_decimal(intent.fee)),
        "exchange": intent.exchange,
    }


def _digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _previous_paper_result(record: PaperRequest, accepted: set[str], side: str) -> PaperTradeResult:
    if record.payload_digest not in accepted:
        message = (
            "Request ID already used for another purchase"
            if side == BUY else "Request ID already used for another sale"
        )
        raise PaperRequestConflict(message)
    return _result(
        side=side,
        asset=record.asset,
        price=record.price,
        executed=record.executed,
        reason=record.reason,
        amount_spent=record.amount_spent,
        asset_received=record.asset_received,
        fee=record.fee,
        operation_id=record.operation_id,
    )


def get_previous_paper_result(
    db: Session, request_id: str, digest: str, side: str,
) -> PaperTradeResult | None:
    """Reproduce una solicitud de mercado ya confirmada sin consultar de nuevo el precio."""
    record = db.get(PaperRequest, request_id)
    return _previous_paper_result(record, {digest}, side) if record is not None else None


def execute_configured_paper_trade(
    db: Session,
    scenario: PaperScenario,
    request_id: str,
    side: str,
    intent: OrderIntent,
    idempotency_digest: str | None = None,
    on_claim: Callable[[Session], None] | None = None,
) -> PaperTradeResult:
    """
    Ejecuta una orden PAPER una sola vez por ``request_id`` (claim-then-execute).

    Orden de la transacción:

    1. ``INSERT`` de ``paper_requests`` (reclamación del identificador).
    2. Relectura de saldo e inventario dentro de la misma transacción.
    3. Guard.
    4. ``INSERT`` de la operación en el Ledger.
    5. ``UPDATE`` del resultado en la reclamación y commit.

    Si otro proceso ganó la reclamación (``IntegrityError``) se hace rollback y
    se devuelve el resultado ya guardado. El digesto incluye ``side``; al leer
    se acepta el digesto nuevo o el legado (solo compras antiguas).
    """

    if side != intent.side:
        raise ValueError("Side mismatch between request and intent")

    payload = _request_payload(intent)
    digest = idempotency_digest or _digest({**payload, "side": side})
    legacy_digest = _digest(payload) if idempotency_digest is None and side == BUY else None
    accepted = {digest} | ({legacy_digest} if legacy_digest else set())

    existing = db.get(PaperRequest, request_id)
    if existing is not None:
        return _previous_paper_result(existing, accepted, side)

    claim = PaperRequest(
        request_id=request_id,
        payload_digest=digest,
        executed=False,
        reason="PENDING",
        asset=intent.asset,
        amount_spent=BLOCKED_AMOUNT,
        asset_received=BLOCKED_AMOUNT,
        price=to_decimal(intent.price),
        fee=BLOCKED_AMOUNT,
        operation_id=None,
    )
    db.add(claim)

    try:
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            winner = db.get(PaperRequest, request_id)
            if winner is None:
                raise
            return _previous_paper_result(winner, accepted, side)

        if on_claim is not None:
            on_claim(db)
        # La cotización puede tardar y otro escritor puede cerrar o modificar
        # el escenario antes de este claim. Con el bloqueo de escritura ya
        # adquirido, recargar el escenario vuelve atómica la decisión.
        db.refresh(scenario)
        if scenario.closed_at is not None:
            result = _result(
                side=side, asset=intent.asset, price=intent.price,
                executed=False, reason="Paper scenario closed",
            )
        else:
            result = _evaluate_and_record(db, scenario, intent)

        claim.executed = result.executed
        claim.reason = result.reason
        claim.asset = result.asset
        claim.amount_spent = result.amount_spent
        claim.asset_received = result.asset_received
        claim.price = result.price
        claim.fee = result.fee
        claim.operation_id = result.operation_id
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(claim)
    return _previous_paper_result(claim, accepted, side)


def execute_configured_paper_buy(
    db: Session,
    scenario: PaperScenario,
    request_id: str,
    asset: str,
    order_amount,
    price,
    estimated_fee,
    exchange: str | None = None,
    idempotency_digest: str | None = None,
    on_claim: Callable[[Session], None] | None = None,
) -> PaperTradeResult:
    """Evalúa una compra una sola vez y guarda el resultado junto a la operación."""
    intent = OrderIntent(
        side=BUY,
        asset=asset,
        quote_currency=scenario.quote_currency,
        price=to_decimal(price),
        fee=to_decimal(estimated_fee),
        order_amount=to_decimal(order_amount),
        exchange=exchange,
    )
    return execute_configured_paper_trade(
        db=db,
        scenario=scenario,
        request_id=request_id,
        side=BUY,
        intent=intent,
        idempotency_digest=idempotency_digest,
        on_claim=on_claim,
    )


def execute_configured_paper_sell(
    db: Session,
    scenario: PaperScenario,
    request_id: str,
    asset: str,
    quantity,
    price,
    fee,
    exchange: str | None = None,
    strategy_id: int | None = None,
    idempotency_digest: str | None = None,
    on_claim: Callable[[Session], None] | None = None,
) -> PaperTradeResult:
    """Evalúa una venta una sola vez y guarda el resultado junto a la operación."""
    intent = OrderIntent(
        side=SELL,
        asset=asset,
        quote_currency=scenario.quote_currency,
        price=to_decimal(price),
        fee=to_decimal(fee),
        quantity=to_decimal(quantity),
        exchange=exchange,
        strategy_id=strategy_id,
    )
    return execute_configured_paper_trade(
        db=db,
        scenario=scenario,
        request_id=request_id,
        side=SELL,
        intent=intent,
        idempotency_digest=idempotency_digest,
        on_claim=on_claim,
    )
