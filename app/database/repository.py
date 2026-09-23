from sqlalchemy.orm import Session

from app.database.models import Strategy


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
    """Borra la estrategia; devuelve False si el ID no existe."""
    strategy = get_strategy(db, strategy_id)

    if strategy is None:
        return False

    db.delete(strategy)
    db.commit()

    return True
