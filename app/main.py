from fastapi import FastAPI, HTTPException, Depends
from decimal import Decimal
from typing import Literal

from sqlalchemy.exc import IntegrityError

from app.math.calculations import calculate_buy_summaries
from app.api.schemas import (
    StrategyCreate,
    OperationCreate,
    PaperBuyRequest,
    PaperScenarioCreate,
)
from app.core.paper_scenarios import (
    create_paper_scenario,
    get_paper_scenario,
    get_paper_scenarios,
    paper_scenario_snapshot,
)
from app.core.paper_trading import PaperRequestConflict, execute_configured_paper_buy
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

    if (
        operation.strategy_id is not None
        and get_strategy(db, operation.strategy_id) is None
    ):
        raise HTTPException(status_code=404, detail="Strategy not found")

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
        source="MANUAL",
        status=operation.status,
        exchange=operation.exchange,
        strategy_id=operation.strategy_id,
    )

@app.get("/portfolio/summary")
def portfolio_summary(
    mode: Literal["PAPER", "LIVE"] = "PAPER",
    asset: str | None = None,
    quote_currency: str | None = None,
    db = Depends(get_db),
):
    """Resume un único par y modo; exige filtros si hay varios pares."""
    summaries = calculate_buy_summaries(get_operations(db), mode=mode)
    if asset is not None:
        summaries = [item for item in summaries if item["asset"] == asset]
    if quote_currency is not None:
        summaries = [
            item for item in summaries
            if item["quote_currency"] == quote_currency
        ]

    if len(summaries) > 1:
        raise HTTPException(
            status_code=400,
            detail="Specify asset and quote_currency, or use /portfolio/buys",
        )

    if summaries:
        return {"mode": mode, **summaries[0]}

    return {
        "mode": mode,
        "asset": asset,
        "quote_currency": quote_currency,
        "total_invested": Decimal("0"),
        "total_asset_received": Decimal("0"),
        "total_fees": Decimal("0"),
        "average_buy_price": Decimal("0"),
    }


@app.get("/portfolio/buys")
def portfolio_buys(mode: Literal["PAPER", "LIVE"] = "PAPER", db = Depends(get_db)):
    """Lista compras ejecutadas por par en PAPER o LIVE, sin mezclar monedas."""
    return calculate_buy_summaries(get_operations(db), mode=mode)

@app.post("/paper/buy")
def paper_buy(
    request: PaperBuyRequest,
    db = Depends(get_db)
):
    """
    Evalúa una compra simulada con capital y límites guardados en el servidor.

    Guard debe autorizar la operación antes de que
    Paper Trading pueda registrarla en el Ledger.
    """

    scenario = get_paper_scenario(db, request.quote_currency)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Paper scenario not found")

    try:
        result = execute_configured_paper_buy(
            db=db,
            scenario=scenario,
            request_id=str(request.request_id),
            asset=request.asset,
            order_amount=request.order_amount,
            price=request.price,
            estimated_fee=request.estimated_fee,
            exchange=request.exchange,
        )
    except PaperRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return result


@app.post("/paper/scenarios", status_code=201)
def add_paper_scenario(request: PaperScenarioCreate, db = Depends(get_db)):
    """Crea una configuración PAPER inmutable para una moneda cotizada."""
    if get_paper_scenario(db, request.quote_currency) is not None:
        raise HTTPException(status_code=409, detail="Paper scenario already exists")

    try:
        scenario = create_paper_scenario(db=db, **request.model_dump())
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Paper scenario already exists") from exc

    return paper_scenario_snapshot(db, scenario)


@app.get("/paper/scenarios")
def list_paper_scenarios(db = Depends(get_db)):
    """Lista el capital, límites y saldo de cada escenario PAPER."""
    return [paper_scenario_snapshot(db, item) for item in get_paper_scenarios(db)]


@app.get("/paper/scenarios/{quote_currency}")
def read_paper_scenario(quote_currency: str, db = Depends(get_db)):
    """Muestra configuración y saldo restante de un escenario PAPER."""
    scenario = get_paper_scenario(db, quote_currency)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Paper scenario not found")
    return paper_scenario_snapshot(db, scenario)
