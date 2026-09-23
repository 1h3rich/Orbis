from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models import Operation


def to_decimal(value) -> Decimal:
    """
    Convierte un valor numérico a Decimal evitando
    arrastrar errores binarios propios de float.
    """
    return Decimal(str(value))


def create_operation(
    db: Session,
    operation_type: str,
    asset: str,
    quote_currency: str,
    amount_spent,
    asset_received,
    price,
    trading_fee=0,
    withdrawal_fee=0,
    network_fee=0,
    mode: str = "PAPER",
    source: str = "MANUAL",
    status: str = "EXECUTED",
    exchange: str | None = None,
    strategy_id: int | None = None,
    commit: bool = True,
):
    """
    Guarda una operación financiera en el Ledger
    utilizando Decimal para sus cantidades.
    """

    operation = Operation(
        operation_type=operation_type,
        asset=asset,
        quote_currency=quote_currency,
        amount_spent=to_decimal(amount_spent),
        asset_received=to_decimal(asset_received),
        price=to_decimal(price),
        trading_fee=to_decimal(trading_fee),
        withdrawal_fee=to_decimal(withdrawal_fee),
        network_fee=to_decimal(network_fee),
        mode=mode,
        source=source,
        status=status,
        exchange=exchange,
        strategy_id=strategy_id,
    )

    db.add(operation)
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(operation)

    return operation


def get_operations(db: Session):
    """
    Devuelve todas las operaciones registradas
    en el Ledger.
    """
    return db.query(Operation).all()


def get_total_spent(
    db: Session,
    mode: str,
    quote_currency: str,
    since: datetime | None = None,
) -> Decimal:
    """Suma compras ejecutadas de un modo y moneda desde un instante dado."""
    query = db.query(Operation.amount_spent).filter(
        Operation.operation_type == "BUY",
        Operation.status == "EXECUTED",
        Operation.mode == mode,
        Operation.quote_currency == quote_currency,
    )
    if since is not None:
        query = query.filter(Operation.timestamp >= since)
    amounts = query.all()
    return sum((to_decimal(amount) for (amount,) in amounts), Decimal("0"))


def get_spent_between(
    db: Session,
    start: datetime,
    end: datetime,
    mode: str,
    quote_currency: str | None = None,
    since: datetime | None = None,
) -> Decimal:
    """
    Calcula cuánto capital se ha gastado en compras
    ejecutadas dentro de un periodo concreto.

    Separa PAPER y LIVE; opcionalmente limita el total a una moneda cotizada.
    """

    query = db.query(
        func.sum(Operation.amount_spent)
    ).filter(
        Operation.operation_type == "BUY",
        Operation.status == "EXECUTED",
        Operation.mode == mode,
        Operation.timestamp >= start,
        Operation.timestamp < end,
    )

    if quote_currency is not None:
        query = query.filter(Operation.quote_currency == quote_currency)
    if since is not None:
        query = query.filter(Operation.timestamp >= since)

    total = query.scalar()

    if total is None:
        return Decimal("0")

    return Decimal(str(total))


def get_daily_spent(
    db: Session,
    mode: str = "PAPER",
    now: datetime | None = None,
    quote_currency: str | None = None,
    since: datetime | None = None,
) -> Decimal:
    """
    Calcula cuánto capital se ha gastado
    durante un día concreto.

    Si no se proporciona fecha, utiliza el día actual.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    start = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    end = start + timedelta(days=1)

    return get_spent_between(
        db=db,
        start=start,
        end=end,
        mode=mode,
        quote_currency=quote_currency,
        since=since,
    )


def get_monthly_spent(
    db: Session,
    mode: str = "PAPER",
    now: datetime | None = None,
    quote_currency: str | None = None,
    since: datetime | None = None,
) -> Decimal:
    """
    Calcula cuánto capital se ha gastado
    durante un mes concreto.

    Si no se proporciona fecha, utiliza el mes actual.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    start = now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    if start.month == 12:
        end = start.replace(
            year=start.year + 1,
            month=1,
        )
    else:
        end = start.replace(
            month=start.month + 1,
        )

    return get_spent_between(
        db=db,
        start=start,
        end=end,
        mode=mode,
        quote_currency=quote_currency,
        since=since,
    )
