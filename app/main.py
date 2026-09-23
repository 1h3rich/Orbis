from fastapi import FastAPI, HTTPException, Depends
from app.math.calculations import calculate_portfolio_summary
from app.api.schemas import StrategyCreate, OperationCreate
from app.ledger.repository import create_operation, get_operations
from app.database import models
from app.database.database import Base, engine, get_db
from app.database.repository import (
    create_strategy,
    get_strategies,
    get_strategy,
    update_strategy,
    delete_strategy,
)


Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Orbis API",
    version="0.1.0"
)


# Obtiene el estado root
@app.get("/")
def root():
    return {
        "name": "Orbis",
        "version": "0.1.0",
        "status": "running"
    }


# Lista la estrategia
@app.get("/strategies")
def list_strategies(db = Depends(get_db)):
    return get_strategies(db)

# Añade una estrategia
@app.post("/strategies")
def add_strategy(strategy: StrategyCreate, db = Depends(get_db)):
    return create_strategy(
        db=db,
        name=strategy.name,
        strategy_type=strategy.strategy_type,
        config=strategy.config,
        enabled=strategy.enabled,
    )

# Lee la estrategia
@app.get("/strategies/{strategy_id}")
def read_strategy(strategy_id: int, db = Depends(get_db)):
    strategy = get_strategy(db, strategy_id)

    if strategy is None:
        raise HTTPException(
            status_code=404,
            detail="Strategy not found"
        )

    return strategy


# Edita la estrategia
@app.put("/strategies/{strategy_id}")
def edit_strategy(
    strategy_id: int,
    strategy: StrategyCreate,
    db = Depends(get_db)
):
    updated_strategy = update_strategy(
        db=db,
        strategy_id=strategy_id,
        name=strategy.name,
        strategy_type=strategy.strategy_type,
        config=strategy.config,
        enabled=strategy.enabled,
    )

    if updated_strategy is None:
        raise HTTPException(
            status_code=404,
            detail="Strategy not found"
        )

    return updated_strategy


# Borra la estrategia
@app.delete("/strategies/{strategy_id}")
def remove_strategy(strategy_id: int, db = Depends(get_db)):
    deleted = delete_strategy(db, strategy_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Strategy not found"
        )

    return {
        "message": "Strategy deleted",
        "id": strategy_id
    }

@app.get("/operations")
def list_operations(db = Depends(get_db)):
    """
    Devuelve el historial de operaciones del Ledger.
    """
    return get_operations(db)


@app.post("/operations")
def add_operation(
    operation: OperationCreate,
    db = Depends(get_db)
):
    """
    Registra una nueva operación financiera.
    """
    return create_operation(
        db=db,
        operation_type=operation.operation_type,
        asset=operation.asset,
        quote_currency=operation.quote_currency,
        amount_spent=operation.amount_spent,
        asset_received=operation.asset_received,
        price=operation.price,
        trading_fee=operation.trading_fee,
        withdrawal_fee=operation.withdrawal_fee,
        network_fee=operation.network_fee,
    )

@app.get("/portfolio/summary")
def portfolio_summary(db = Depends(get_db)):
    """
    Calcula el estado de la cartera utilizando
    las operaciones registradas en el Ledger.
    """
    operations = get_operations(db)

    return calculate_portfolio_summary(operations)