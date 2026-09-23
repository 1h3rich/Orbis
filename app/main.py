from fastapi import FastAPI, HTTPException, Depends
from app.math.calculations import calculate_portfolio_summary
from app.api.schemas import (
    StrategyCreate,
    OperationCreate,
    PaperBuyRequest,
)
from app.core.paper_trading import execute_and_record_paper_buy
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


@app.get("/")
def root():
    """Devuelve la identidad y versión declarada de la API para comprobar que responde."""
    return {
        "name": "Orbis",
        "version": "0.1.0",
        "status": "running"
    }


@app.get("/strategies")
def list_strategies(db = Depends(get_db)):
    """Lista todas las estrategias guardadas, con independencia de `enabled`."""
    return get_strategies(db)

@app.post("/strategies")
def add_strategy(strategy: StrategyCreate, db = Depends(get_db)):
    """Guarda la definición de una estrategia; todavía no programa ejecuciones."""
    return create_strategy(
        db=db,
        name=strategy.name,
        strategy_type=strategy.strategy_type,
        config=strategy.config,
        enabled=strategy.enabled,
    )

@app.get("/strategies/{strategy_id}")
def read_strategy(strategy_id: int, db = Depends(get_db)):
    """Busca una estrategia por ID y devuelve HTTP 404 si no existe."""
    strategy = get_strategy(db, strategy_id)

    if strategy is None:
        raise HTTPException(
            status_code=404,
            detail="Strategy not found"
        )

    return strategy


@app.put("/strategies/{strategy_id}")
def edit_strategy(
    strategy_id: int,
    strategy: StrategyCreate,
    db = Depends(get_db)
):
    """Reemplaza los campos configurables de una estrategia existente."""
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


@app.delete("/strategies/{strategy_id}")
def remove_strategy(strategy_id: int, db = Depends(get_db)):
    """Elimina una estrategia por ID o devuelve HTTP 404 si no existe."""
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
    """Devuelve las operaciones registradas, sin filtros ni orden explícito."""
    return get_operations(db)


@app.post("/operations")
def add_operation(
    operation: OperationCreate,
    db = Depends(get_db)
):
    """
    Registra manualmente una operación financiera.
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
        mode=operation.mode,
        source=operation.source,
        status=operation.status,
        exchange=operation.exchange,
        strategy_id=operation.strategy_id,
    )

@app.get("/portfolio/summary")
def portfolio_summary(db = Depends(get_db)):
    """Resume compras y comisiones del Ledger sin consultar precios de mercado."""
    operations = get_operations(db)

    return calculate_portfolio_summary(operations)

@app.post("/paper/buy")
def paper_buy(
    request: PaperBuyRequest,
    db = Depends(get_db)
):
    """
    Ejecuta una compra simulada.

    Guard debe autorizar la operación antes de que
    Paper Trading pueda registrarla en el Ledger.
    """

    result = execute_and_record_paper_buy(
        db=db,
        asset=request.asset,
        quote_currency=request.quote_currency,
        order_amount=request.order_amount,
        price=request.price,
        available_budget=request.available_budget,
        max_order_amount=request.max_order_amount,
        estimated_fee=request.estimated_fee,
        max_fee_percentage=request.max_fee_percentage,
        exchange=request.exchange,
        strategy_id=request.strategy_id,
        daily_limit=request.daily_limit,
        monthly_limit=request.monthly_limit,
    )

    return result
