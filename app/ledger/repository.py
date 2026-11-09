from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from sqlalchemy.orm import Session

from app.database.models import Operation


def to_decimal(value) -> Decimal:
    """
    Convierte un valor numérico a Decimal evitando
    arrastrar errores binarios propios de float.

    Nunca debe aplicarse a un ``float`` obtenido de una agregación SQL
    (``func.sum``): ver ``_sum_amounts``.
    """
    return Decimal(str(value))


def _sum_amounts(values) -> Decimal:
    """
    Suma importes del Ledger de forma exacta en Python.

    B1: las agregaciones SQL (``func.sum``) pueden devolver ``float`` y
    ``Decimal(str(float))`` arrastra el error binario (p. ej.
    ``0.30000000000000004``). Aquí se suman los ``Decimal`` tal cual salen
    de la columna ``Numeric``.
    """
    with localcontext() as context:
        context.prec = 80
        return sum((to_decimal(value) for value in values), Decimal("0"))


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

    H10: el orden es determinista (``timestamp``, ``id``) para que las
    reconstrucciones de saldos y carteras sean reproducibles.
    """
    return (
        db.query(Operation)
        .order_by(Operation.timestamp.asc(), Operation.id.asc())
        .all()
    )


def list_paper_operations(
    db: Session,
    asset: str | None = None,
    quote_currency: str | None = None,
    side: str | None = None,
    strategy_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Operation]:
    """
    Lista operaciones PAPER con filtros opcionales y orden determinista
    ``timestamp``, ``id`` (ascendente).

    ``side`` acepta ``BUY`` o ``SELL``; ``start`` es inclusivo y ``end`` exclusivo.
    """
    query = db.query(Operation).filter(Operation.mode == "PAPER")

    if asset is not None:
        query = query.filter(Operation.asset == asset)
    if quote_currency is not None:
        query = query.filter(Operation.quote_currency == quote_currency)
    if side is not None:
        query = query.filter(Operation.operation_type == side.upper())
    if strategy_id is not None:
        query = query.filter(Operation.strategy_id == strategy_id)
    if start is not None:
        query = query.filter(Operation.timestamp >= start)
    if end is not None:
        query = query.filter(Operation.timestamp < end)

    return query.order_by(Operation.timestamp.asc(), Operation.id.asc()).all()


def get_net_cash_moved(
    db: Session,
    mode: str,
    quote_currency: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Decimal:
    """
    Devuelve el movimiento neto de caja: ``ΣSELL − ΣBUY`` de las operaciones
    EXECUTED de un modo y moneda. En caja: ``initial_balance + este valor``.

    La suma se hace en Python sobre ``Decimal`` (B1), nunca con ``func.sum``.
    """
    query = db.query(
        Operation.operation_type,
        Operation.amount_spent,
    ).filter(
        Operation.status == "EXECUTED",
        Operation.operation_type.in_(("BUY", "SELL")),
        Operation.mode == mode,
        Operation.quote_currency == quote_currency,
    )
    if since is not None:
        query = query.filter(Operation.timestamp >= since)
    if until is not None:
        query = query.filter(Operation.timestamp < until)

    with localcontext() as context:
        context.prec = 80
        net = Decimal("0")
        for operation_type, amount in query.all():
            if operation_type == "SELL":
                net += to_decimal(amount)
            else:
                net -= to_decimal(amount)
        return net


def get_total_spent(
    db: Session,
    mode: str,
    quote_currency: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Decimal:
    """
    Suma compras ejecutadas de un modo y moneda desde un instante dado.

    Sigue filtrando solo ``BUY`` para no alterar el significado histórico de
    ``total_spent``/``available_balance``; la caja real (compras menos ventas)
    se calcula con ``get_net_cash_moved``.
    """
    query = db.query(Operation.amount_spent).filter(
        Operation.operation_type == "BUY",
        Operation.status == "EXECUTED",
        Operation.mode == mode,
        Operation.quote_currency == quote_currency,
    )
    if since is not None:
        query = query.filter(Operation.timestamp >= since)
    if until is not None:
        query = query.filter(Operation.timestamp < until)
    amounts = query.all()
    return _sum_amounts(amount for (amount,) in amounts)


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

    B1: no se usa ``func.sum``; los importes se leen fila a fila y se suman
    como ``Decimal`` exacto.
    """

    query = db.query(Operation.amount_spent).filter(
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

    return _sum_amounts(amount for (amount,) in query.all())


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
