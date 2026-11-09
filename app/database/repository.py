from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import Operation, Strategy


class StrategyInUseError(ValueError):
    """La definición debe conservarse para explicar operaciones ya registradas."""


STRATEGY_IN_USE_DETAIL = (
    "La estrategia tiene operaciones registradas; desactívala para conservar el historial"
)


def create_strategy(
    db: Session,
    name: str,
    strategy_type: str,
    config: dict,
    enabled: bool = True,
):
    """Inserta una estrategia y devuelve el registro tras confirmar la transacción."""
    strategy = Strategy(
        name=name,
        strategy_type=strategy_type,
        config=config,
        enabled=enabled,
    )

    db.add(strategy)
    db.commit()
    db.refresh(strategy)

    return strategy


def get_strategies(db: Session):
    """Devuelve todas las estrategias, sin filtrar por su estado `enabled`."""
    return db.query(Strategy).all()


def get_strategy(db: Session, strategy_id: int):
    """Busca una estrategia por ID; devuelve None si no existe."""
    return db.query(Strategy).filter(
        Strategy.id == strategy_id
    ).first()

def update_strategy(
    db: Session,
    strategy_id: int,
    name: str,
    strategy_type: str,
    config: dict,
    enabled: bool,
):
    """Sustituye los campos de la estrategia; devuelve None si no existe."""
    strategy = get_strategy(db, strategy_id)

    if strategy is None:
        return None

    strategy.name = name
    strategy.strategy_type = strategy_type
    strategy.config = config
    strategy.enabled = enabled

    db.commit()
    db.refresh(strategy)

    return strategy

def delete_strategy(db: Session, strategy_id: int):
    """Borra sólo estrategias sin operaciones; conserva la procedencia PAPER."""
    strategy = get_strategy(db, strategy_id)

    if strategy is None:
        return False

    if db.query(Operation.id).filter(Operation.strategy_id == strategy_id).first():
        raise StrategyInUseError(STRATEGY_IN_USE_DETAIL)

    db.delete(strategy)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Otro escritor pudo registrar una operación entre la consulta y el
        # DELETE. La FK protege el dato; la API debe explicar el conflicto.
        if db.query(Operation.id).filter(Operation.strategy_id == strategy_id).first():
            raise StrategyInUseError(STRATEGY_IN_USE_DETAIL) from exc
        raise

    return True
