from sqlalchemy.orm import Session

from app.database.models import Operation


def create_operation(
    db: Session,
    operation_type: str,
    asset: str,
    quote_currency: str,
    amount_spent: float,
    asset_received: float,
    price: float,
    trading_fee: float = 0.0,
    withdrawal_fee: float = 0.0,
    network_fee: float = 0.0,
):
    """
    Guarda una nueva operación en el Ledger.
    """

    operation = Operation(
        operation_type=operation_type,
        asset=asset,
        quote_currency=quote_currency,
        amount_spent=amount_spent,
        asset_received=asset_received,
        price=price,
        trading_fee=trading_fee,
        withdrawal_fee=withdrawal_fee,
        network_fee=network_fee,
    )

    db.add(operation)
    db.commit()
    db.refresh(operation)

    return operation


def get_operations(db: Session):
    """
    Devuelve todas las operaciones registradas.
    """
    return db.query(Operation).all()


