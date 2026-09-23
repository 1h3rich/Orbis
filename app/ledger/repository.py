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
    Inserta y confirma una operación; devuelve el registro con su ID asignado.

    No valida el tipo de operación ni comprueba si ya existe un duplicado.
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
    """Consulta todas las operaciones sin filtros ni orden garantizado."""
    return db.query(Operation).all()

