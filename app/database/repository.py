from sqlalchemy.orm import Session

from app.database.models import Strategy


def create_strategy(
    db: Session,
    name: str,
    strategy_type: str,
    config: dict,
    enabled: bool = True,
):
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
    return db.query(Strategy).all()


def get_strategy(db: Session, strategy_id: int):
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
    strategy = get_strategy(db, strategy_id)

    if strategy is None:
        return False

    db.delete(strategy)
    db.commit()

    return True