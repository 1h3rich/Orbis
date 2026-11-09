from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from decimal import Decimal, localcontext
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.math.calculations import calculate_buy_summaries
from app.api.exact_json import exact_json, exact_jsonable
from app.api.schemas import (
    StrategyCreate,
    OperationCreate,
    PaperBuyRequest,
    PaperMarketBuyRequest,
    PaperMarketSellRequest,
    PaperPriceRequest,
    PaperScenarioCreate,
    PaperSellRequest,
    PaperSpotProvider,
)
from app.core.paper_portfolio import build_paper_portfolio
from app.core.runtime_source import RuntimeSourceMonitor
from app.core.paper_scenarios import (
    close_paper_scenario,
    create_paper_scenario,
    get_paper_scenario,
    get_paper_scenarios,
    paper_scenario_snapshot,
)
from app.core.paper_trading import (
    PaperRequestConflict,
    execute_configured_paper_buy,
    execute_configured_paper_sell,
    get_previous_paper_result,
)
from app.core.paper_market_orders import (
    MarketQuoteUnavailable, evidence_for, legacy_market_request_digest, market_request_digest,
    market_trade_response, quote_from_snapshot,
)
from app.marketdata.service import snapshot as market_snapshot
from app.marketdata.normalize import canonical_base, canonical_quote
from app.ledger.repository import (
    _sum_amounts,
    create_operation,
    list_paper_operations,
)
from app.database import models
from app.database.database import Base, engine, get_db, legacy_paper_access, legacy_paper_runtime, DATABASE_URL
from app.database.legacy_runtime import (LegacyDatabaseError, legacy_database_format,
                                         inspect_legacy_sibling_copies)
from app.database.legacy_backup import LegacyBackupError, create_legacy_encrypted_backup
from sqlalchemy.engine import make_url
from app.database.schema_check import check_schema_or_log
from app.database.repository import (
    StrategyInUseError,
    create_strategy,
    get_strategies,
    get_strategy,
    update_strategy,
    delete_strategy,
)
from app.pricing.providers import list_prices, upsert_price
from app.security.loopback import LocalLoopbackMiddleware


def _initialize_manual_paper_schema(target=engine):
    Base.metadata.create_all(bind=target)
    check_schema_or_log(target)


# A GUI-first start can serve Settings with a missing/locked encrypted PAPER.
# Its model schema is initialized only after creation or verified unlock.
if legacy_paper_runtime is None or legacy_paper_runtime.status()['unlocked']:
    _initialize_manual_paper_schema()

from app.automation.paper_dca import PaperDca, create_paper_dca_router  # noqa: E402
from app.automation.paper_worker import PaperDcaWorker  # noqa: E402
from app.automation.spot_strategy_paper import SpotStrategyPaper, create_spot_strategy_router  # noqa: E402
from app.automation.spot_strategy_worker import SpotStrategyWorker  # noqa: E402
from app.derivatives.signal_monitor import SignalMonitor, create_signal_monitor_router  # noqa: E402
from app.derivatives.signal_worker import SignalWorker  # noqa: E402
from app.derivatives.signal_risk_worker import SignalRiskWorker  # noqa: E402
from app.derivatives.funding_worker import AutoFunding, AutoFundingWorker  # noqa: E402
from app.lab.backup import lab_runtime_lease  # noqa: E402
from app.automation.lease import automation_lease  # noqa: E402
from app.automation.spot_paper_lease import spot_paper_lease  # noqa: E402
from app.derivatives.paper_lease import paper_perp_lease  # noqa: E402

_lab_path = Path(os.environ.get("ORBIS_LAB_PATH", "~/.local/share/orbis/lab.db"))
_automation_path = Path(os.environ.get("ORBIS_AUTOMATION_PATH", "~/.local/share/orbis/automation.db"))
_paper_perp_path = Path(os.environ.get("ORBIS_PAPER_PERP_PATH", "~/.local/share/orbis/paper-perp.db"))
_spot_strategy_path = Path(os.environ.get("ORBIS_SPOT_STRATEGY_PATH", "~/.local/share/orbis/spot-strategy-paper.db"))
_paper_dca = PaperDca(_automation_path, _lab_path)
_paper_dca_worker = PaperDcaWorker(_paper_dca)
_spot_strategy_paper = SpotStrategyPaper(_spot_strategy_path, _lab_path, allow_create=False)
_spot_strategy_worker = SpotStrategyWorker(_spot_strategy_paper)
_signal_monitor = SignalMonitor(_paper_perp_path, _lab_path)
_funding_worker = AutoFundingWorker(AutoFunding(_signal_monitor.paper))
_signal_worker = SignalWorker(_signal_monitor, after_poll=_funding_worker.wake)
_signal_risk_worker = SignalRiskWorker(_signal_monitor,
                                       action_wake=lambda: (_signal_worker.wake(), _funding_worker.wake()))
from app.derivatives.perp_observed_archive import ObservedArchive  # noqa: E402
from app.derivatives.perp_observed_service import ObservedPaperService  # noqa: E402
from app.derivatives.perp_observed_api import create_observed_paper_router  # noqa: E402
from app.lab.store import LabStore  # noqa: E402

_observed_paper = ObservedPaperService(LabStore(_lab_path,allow_create=False),
    ObservedArchive(_signal_monitor.paper))


@asynccontextmanager
async def lifespan(_: FastAPI):
    with (lab_runtime_lease(_lab_path), automation_lease(_automation_path),
          paper_perp_lease(_paper_perp_path), spot_paper_lease(_spot_strategy_path)):
        workers = (_spot_strategy_worker, _signal_worker, _signal_risk_worker,
                   _funding_worker, _paper_dca_worker, _observed_paper)
        try:
            _paper_dca_worker.start()
            _spot_strategy_worker.start()
            _funding_worker.start()
            _signal_risk_worker.start()
            _signal_worker.start()
            _observed_paper.start()
            yield
        finally:
            # Primero detener todos: ninguno debe iniciar otro poll mientras
            # esperamos a que los demás terminen. La lease se libera después.
            for worker in workers:
                worker.request_stop()
            for worker in workers:
                worker.stop()


app = FastAPI(title="Orbis API", version="0.1.0", lifespan=lifespan)
app.add_middleware(LocalLoopbackMiddleware)
_runtime_source = RuntimeSourceMonitor(Path(__file__).resolve().parents[1])


@app.get("/system/runtime")
def system_runtime(response: Response) -> dict:
    """Sólo comunica si el código local cambió desde este arranque."""
    response.headers["Cache-Control"] = "no-store"
    return {"restart_required": _runtime_source.restart_required()}


class LegacyPaperUnlockRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    passphrase: SecretStr=Field(min_length=12,max_length=1024)


class LegacyPaperCreateRequest(LegacyPaperUnlockRequest):
    repeated: SecretStr=Field(min_length=12,max_length=1024)
    confirmation: Literal['CREAR PAPER CIFRADO']


class LegacyPaperBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    passphrase: SecretStr = Field(min_length=12, max_length=1024)
    confirmation: Literal["COPIA PAPER"]


@app.get("/paper/legacy/status")
def legacy_paper_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    target = make_url(DATABASE_URL)
    local_file = (target.get_backend_name() == "sqlite" and target.database
                  and target.database != ":memory:" and not target.query.get("uri"))
    if (legacy_paper_runtime is not None and legacy_paper_access is legacy_paper_runtime.access
            and target.database and Path(target.database).expanduser().absolute()==legacy_paper_runtime.path):
        status=legacy_paper_runtime.status()
    elif legacy_paper_access is not None:
        status = legacy_paper_access.status()
    elif local_file:
        status = {"format": legacy_database_format(target.database),
                  "unlocked": True, "remaining_seconds": 0}
    else:
        status = {"format": "memory", "unlocked": True, "remaining_seconds": 0}
    if local_file:
        return status | inspect_legacy_sibling_copies(Path(target.database))
    return status | {"plaintext_sibling_copies": [],
                     "unverified_sibling_copies": [], "sibling_scan_complete": True}


@app.post("/paper/legacy/backup/encrypted")
async def backup_legacy_paper(request: Request, response: Response):
    from app.core.manual_paper_protection_api import secret_body
    payload=await secret_body(request,LegacyPaperBackupRequest)
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_backup_legacy_paper,payload)


def _backup_legacy_paper(payload: LegacyPaperBackupRequest):
    target = make_url(DATABASE_URL)
    if (target.get_backend_name() != "sqlite" or not target.database
            or target.database == ":memory:" or target.query.get("uri")):
        raise HTTPException(status_code=409, detail="PAPER no usa un archivo local compatible")
    source = Path(target.database).expanduser().absolute()
    try:
        source_format = legacy_database_format(source)
        source_key = None
        if source_format == "encrypted_or_invalid":
            if legacy_paper_access is None or legacy_paper_access.path != source:
                raise HTTPException(status_code=409, detail="PAPER cifrado no tiene acceso local compatible")
            source_key = legacy_paper_access.get()
            if source_key is None:
                raise HTTPException(status_code=423, detail="Desbloquea PAPER antes de crear la copia")
        elif source_format != "plain":
            raise HTTPException(status_code=409, detail="PAPER activo no está inicializado")
        result = create_legacy_encrypted_backup(
            source, payload.passphrase.get_secret_value(), source_passphrase=source_key)
    except (LegacyBackupError, LegacyDatabaseError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"encrypted_backup": True, "active_database_unchanged": True,
            "active_database_encrypted": source_format == "encrypted_or_invalid",
            "path": result["path"], "tables": result["tables"],
            "user_version": result["user_version"], "fingerprint": result["fingerprint"]}


@app.post("/paper/legacy/lock")
def lock_legacy_paper(response: Response):
    response.headers['Cache-Control']='no-store'
    if legacy_paper_runtime is not None and legacy_paper_access is legacy_paper_runtime.access:
        if legacy_paper_runtime.status()['format'] not in ('missing','encrypted_or_invalid'):
            raise HTTPException(409,'PAPER manual no está cifrado')
        return legacy_paper_runtime.lock()
    if legacy_paper_access is None or legacy_database_format(legacy_paper_access.path)!='encrypted_or_invalid':
        raise HTTPException(status_code=409, detail="PAPER manual no está cifrado")
    status = (legacy_paper_runtime.lock() if legacy_paper_runtime is not None and legacy_paper_access is legacy_paper_runtime.access
        else legacy_paper_access.lock())
    engine.dispose()
    return status


@app.post("/paper/legacy/unlock")
async def unlock_legacy_paper(request: Request,response: Response):
    from app.core.manual_paper_protection_api import secret_body
    payload=await secret_body(request,LegacyPaperUnlockRequest)
    response.headers['Cache-Control']='no-store'
    if legacy_paper_access is None or legacy_database_format(legacy_paper_access.path)!='encrypted_or_invalid':
        raise HTTPException(status_code=409, detail="PAPER manual no está cifrado")
    try:
        if legacy_paper_runtime is not None and legacy_paper_access is legacy_paper_runtime.access:
            status=await run_in_threadpool(legacy_paper_runtime.unlock,payload.passphrase.get_secret_value(),
                initialize=_initialize_manual_paper_schema)
        else:status=await run_in_threadpool(legacy_paper_access.unlock,payload.passphrase.get_secret_value())
    except LegacyDatabaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:raise HTTPException(409,'El esquema PAPER necesita revisión; el almacén queda cerrado') from None
    engine.dispose()
    return status


@app.post('/paper/legacy/create')
async def create_legacy_paper(request: Request,response: Response):
    from app.core.manual_paper_protection_api import secret_body
    payload=await secret_body(request,LegacyPaperCreateRequest)
    response.headers['Cache-Control']='no-store'
    if legacy_paper_runtime is None:raise HTTPException(409,'PAPER manual no utiliza un archivo local compatible')
    key=payload.passphrase.get_secret_value()
    if key!=payload.repeated.get_secret_value():raise HTTPException(422,'Las contraseñas PAPER no coinciden')
    try:return await run_in_threadpool(legacy_paper_runtime.create,key,initialize=_initialize_manual_paper_schema)
    except LegacyDatabaseError as exc:raise HTTPException(409,str(exc)) from None
    except Exception:raise HTTPException(409,'No se pudo verificar el nuevo PAPER; consulta su estado antes de reintentar') from None


@app.exception_handler(LegacyDatabaseError)
async def manual_paper_unavailable(request: Request,exc: LegacyDatabaseError):
    return JSONResponse({'detail':'PAPER manual no disponible; revisa Ajustes'},status_code=423,
        headers={'Cache-Control':'no-store'})

from app.portfolio.api import create_real_router  # noqa: E402

_real_vault_path = Path(os.environ.get("ORBIS_REAL_VAULT_PATH", "~/.local/share/orbis/real-vault.db"))
from app.core.storage_protection import StorageProtection, active_plain_sessions
from app.core.storage_protection_api import create_storage_protection_router, StorageProtectionMiddleware

_real_idle_checks = []
_legacy_database_name = make_url(DATABASE_URL).database
_storage_protection = StorageProtection(
    {'LAB':_lab_path,'DCA':_automation_path,'SPOT':_spot_strategy_path,'GRVT':_paper_perp_path},
    forbidden_paths=(_real_vault_path,) + ((Path(_legacy_database_name),)
        if _legacy_database_name and _legacy_database_name != ':memory:' else ()))
app.state.storage_protection = _storage_protection
app.add_middleware(StorageProtectionMiddleware,control=_storage_protection)
app.include_router(create_real_router(_real_vault_path, lab_path=_lab_path,
    paper_perp_path=_paper_perp_path, spot_paper=_spot_strategy_paper,dca_paper=_paper_dca,
    register_storage_idle=_real_idle_checks.append))


def _storage_protection_readiness() -> str | None:
    if _runtime_source.restart_required():
        return 'Reinicia ORBIS para cargar los cambios antes de proteger datos'
    if not _real_idle_checks or not _real_idle_checks[0]():
        return 'Bloquea Patrimonio REAL y detén su operativa antes de proteger datos'
    if legacy_paper_runtime is not None:
        manual=legacy_paper_runtime.status()
        if manual.get('creating'):return 'Espera a que termine la creación de PAPER manual'
        if manual['format']=='encrypted_or_invalid' and manual['unlocked']:
            return 'Bloquea PAPER manual antes de proteger los datos'
    from app.lab.access import lab_access
    from app.automation.access import automation_access
    from app.automation.spot_paper_access import spot_paper_access
    from app.derivatives.paper_access import paper_perp_access
    accesses={'LAB':lab_access,'DCA':automation_access,'SPOT':spot_paper_access,'GRVT':paper_perp_access}
    if any(accesses[name].get(path) is not None for name,path in _storage_protection.paths.items()):
        return 'Bloquea los almacenes abiertos en Ajustes antes de proteger el legado'
    try:
        if active_plain_sessions(_storage_protection.paths):
            return 'Detén los seguimientos PAPER activos antes de proteger los datos'
    except Exception:
        return 'No se pudo comprobar el seguimiento PAPER; revisa su estado'
    return None


_storage_protection.set_readiness(_storage_protection_readiness)
app.include_router(create_storage_protection_router(_storage_protection))
from app.core.manual_paper_protection import ManualPaperProtection
from app.core.manual_paper_protection_api import create_manual_protection_router
_manual_paper_protection=ManualPaperProtection(legacy_paper_runtime,_storage_protection,
    readiness=_storage_protection_readiness,
    forbidden_paths=(_real_vault_path,)+tuple(_storage_protection.paths.values()))
app.state.manual_paper_protection=_manual_paper_protection
app.include_router(create_manual_protection_router(_manual_paper_protection))

from app.lab.api import create_lab_router  # noqa: E402

app.include_router(create_lab_router(_lab_path))
app.include_router(create_spot_strategy_router(_spot_strategy_path, _lab_path,
                                               paper=_spot_strategy_paper, worker=_spot_strategy_worker,
                                               on_access_change=_spot_strategy_worker.wake))

from app.automation.api import create_automation_router  # noqa: E402

app.include_router(create_automation_router(_automation_path, _lab_path))
app.include_router(create_paper_dca_router(_automation_path, _lab_path,
                                           paper=_paper_dca, worker=_paper_dca_worker))

from app.derivatives.paper_api import create_paper_perp_router  # noqa: E402

app.include_router(create_paper_perp_router(
    _paper_perp_path,
    on_access_change=lambda: (_signal_worker.wake(), _signal_risk_worker.wake(),
                              _funding_worker.wake(), _observed_paper.access_changed()),
))
app.include_router(create_observed_paper_router(_observed_paper))
app.include_router(create_signal_monitor_router(_paper_perp_path, _lab_path,
                                                monitor=_signal_monitor, worker=_signal_worker,
                                                risk_worker=_signal_risk_worker,
                                                funding_worker=_funding_worker))

from app.web.mount import register_ui  # noqa: E402
register_ui(app)


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
    """Sólo elimina definiciones sin operaciones históricas asociadas."""
    try:
        deleted = delete_strategy(db, strategy_id)
    except StrategyInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

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
    """Compatibilidad del Ledger PAPER; nunca publica filas LIVE legadas."""
    return exact_json(list_paper_operations(db))


@app.post("/operations")
def add_operation(
    operation: OperationCreate,
    db = Depends(get_db)
):
    """
    Registra manualmente una operación financiera.

    Excepción documentada de idempotencia: este endpoint manual preexistente
    NO usa `request_id` y cada llamada crea una operación nueva. La
    idempotencia con `request_id` (digest que incluye `side` y
    `quote_currency`, 409 ante reutilización con otra propuesta) existe solo
    en el motor PAPER (`POST /paper/buy`, `POST /paper/sell`). No cambiar
    este comportamiento sin necesidad: está fuera del motor PAPER.
    """

    if operation.mode == "LIVE":
        raise HTTPException(
            status_code=409,
            detail="LIVE records are not accepted by the legacy unencrypted ledger; use /real after unlocking the vault",
        )

    if (
        operation.strategy_id is not None
        and get_strategy(db, operation.strategy_id) is None
    ):
        raise HTTPException(status_code=404, detail="Strategy not found")

    try:
        saved = create_operation(
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
        return exact_json(saved)
    except IntegrityError as exc:
        db.rollback()
        if operation.strategy_id is not None and get_strategy(db, operation.strategy_id) is None:
            raise HTTPException(
                status_code=409,
                detail="La estrategia desapareció antes de guardar la operación; no se registró nada",
            ) from exc
        raise

@app.get("/portfolio/summary")
def portfolio_summary(
    mode: Literal["PAPER", "LIVE"] = "PAPER",
    asset: str | None = None,
    quote_currency: str | None = None,
    db = Depends(get_db),
):
    """Resume PAPER; las filas LIVE legadas no salen de la API sin cifrar."""
    if mode == "LIVE":
        raise HTTPException(status_code=410, detail="Legacy LIVE records are not exposed by the unencrypted API")
    summaries = calculate_buy_summaries(list_paper_operations(db), mode="PAPER")
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
        return exact_json({"mode": mode, **summaries[0]})

    return exact_json({
        "mode": mode,
        "asset": asset,
        "quote_currency": quote_currency,
        "total_invested": Decimal("0"),
        "total_asset_received": Decimal("0"),
        "total_fees": Decimal("0"),
        "average_buy_price": Decimal("0"),
    })


@app.get("/portfolio/buys")
def portfolio_buys(mode: Literal["PAPER", "LIVE"] = "PAPER", db = Depends(get_db)):
    """Lista compras PAPER; nunca consulta filas LIVE legadas."""
    if mode == "LIVE":
        raise HTTPException(status_code=410, detail="Legacy LIVE records are not exposed by the unencrypted API")
    return exact_json(calculate_buy_summaries(list_paper_operations(db), mode="PAPER"))

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

    if (request.exchange or "").strip().lower() == "grvt" or ":PERP" in request.quote_currency.upper():
        raise HTTPException(status_code=422, detail="PAPER GRVT PERP requiere motor de derivados; no se registra como spot")
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

    return exact_json(result)


def _paper_public_market_order(request, side: Literal["BUY", "SELL"], db):
    """Una solicitud, una cotización pública y un asiento PAPER atómico."""
    asset = canonical_base(request.asset)
    quote_currency = canonical_quote(request.quote_currency)
    request_id = str(request.request_id)
    size = request.order_amount if side == "BUY" else request.quantity
    fee = request.estimated_fee if side == "BUY" else request.fee
    digest = market_request_digest(
        side=side, asset=asset, quote_currency=quote_currency,
        size=size, fee=fee, provider=request.provider,
    )
    legacy_digest = legacy_market_request_digest(
        side=side, asset=asset, quote_currency=quote_currency,
        size=size, fee=fee, provider=request.provider,
    )

    def confirmed_result():
        try:
            previous = get_previous_paper_result(db, request_id, digest, side)
        except PaperRequestConflict:
            if legacy_digest is None or legacy_digest == digest:
                raise
            previous = get_previous_paper_result(db, request_id, legacy_digest, side)
        if previous is None:
            return None
        evidence = db.get(models.PaperMarketEvidence, request_id)
        if evidence is None:
            raise HTTPException(status_code=409, detail="Market quote evidence is missing")
        return market_trade_response(previous, evidence)

    def unavailable_or_confirmed(detail: str):
        # La consulta pública ocurre sin bloquear escrituras SQLite. Otro
        # intento con el mismo ID puede confirmarse mientras éste falla.
        # Terminar la lectura anterior permite observar ese commit reciente.
        db.rollback()
        try:
            confirmed = confirmed_result()
        except PaperRequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if confirmed is not None:
            return confirmed
        return JSONResponse(status_code=503, content={
            "code": "market_quote_unavailable", "detail": detail,
        })

    try:
        confirmed = confirmed_result()
    except PaperRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if confirmed is not None:
        return confirmed

    scenario = get_paper_scenario(db, quote_currency)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Paper scenario not found")
    try:
        public = market_snapshot(f"{asset}/{quote_currency}", provider=request.provider)
        quote = quote_from_snapshot(
            public, asset=asset, quote_currency=quote_currency,
            side=side, requested_provider=request.provider,
        )
    except MarketQuoteUnavailable as exc:
        return unavailable_or_confirmed(str(exc))
    except Exception:
        return unavailable_or_confirmed("No se pudo consultar la cotización pública")
    def on_claim(session):
        # db.flush() puede esperar a otro escritor. Revalidar después de
        # reclamar el ID evita ejecutar con una cotización vencida en cola.
        quote_from_snapshot(
            public, asset=asset, quote_currency=quote_currency,
            side=side, requested_provider=request.provider,
        )
        session.add(evidence_for(request_id, quote))

    try:
        if side == "BUY":
            result = execute_configured_paper_buy(
                db=db, scenario=scenario, request_id=request_id, asset=asset,
                order_amount=size, price=quote.price, estimated_fee=fee,
                exchange=quote.provider, idempotency_digest=digest, on_claim=on_claim,
            )
        else:
            result = execute_configured_paper_sell(
                db=db, scenario=scenario, request_id=request_id, asset=asset,
                quantity=size, price=quote.price, fee=fee,
                exchange=quote.provider, idempotency_digest=digest, on_claim=on_claim,
            )
    except PaperRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MarketQuoteUnavailable as exc:
        return unavailable_or_confirmed(str(exc))
    evidence = db.get(models.PaperMarketEvidence, request_id)
    if evidence is None:
        raise HTTPException(status_code=500, detail="Market quote evidence was not saved")
    return market_trade_response(result, evidence)


@app.get("/paper/market/preview")
def paper_market_preview(
    asset: str = Query(default="BTC", pattern=r"^[A-Za-z0-9]{1,12}$"),
    quote_currency: str = Query(default="EUR", pattern=r"^[A-Za-z0-9]{2,12}$"),
    provider: PaperSpotProvider = "kraken",
):
    """Referencia de ambas direcciones con el validador de ejecución PAPER.

    Sólo consulta datos públicos. No reserva un precio ni garantiza que la
    cotización siga vigente al enviar una orden: ésta obtiene una nueva.
    """
    base = canonical_base(asset)
    quote = canonical_quote(quote_currency)
    symbol = f"{base}/{quote}"
    try:
        public = market_snapshot(symbol, provider=provider)
    except Exception:
        return {
            "available": False, "symbol": symbol, "provider": provider,
            "stale": True, "price": None, "buy_price": None,
            "sell_price": None, "reason": "No se pudo consultar la cotización pública",
        }
    sides = {}
    reasons = {}
    for side in ("BUY", "SELL"):
        try:
            sides[side] = quote_from_snapshot(
                public, asset=base, quote_currency=quote,
                side=side, requested_provider=provider,
            )
        except MarketQuoteUnavailable as exc:
            reasons[side] = str(exc)
    buy = sides.get("BUY")
    sell = sides.get("SELL")
    reference = buy or sell
    if reference is None:
        return {
            "available": False, "symbol": symbol, "provider": provider,
            "stale": True, "price": None, "buy_price": None,
            "sell_price": None, "reason": reasons.get("BUY") or reasons.get("SELL"),
        }
    received = datetime.fromisoformat(reference.received_at.replace("Z", "+00:00"))
    age = max(0, int((datetime.now(timezone.utc) - received).total_seconds()))
    return {
        "available": True, "symbol": symbol, "provider": reference.provider,
        "source": reference.source, "received_at": reference.received_at,
        "market_time_at": reference.market_time_at,
        "market_time_status": reference.market_time_status,
        "age_seconds": age, "stale": False,
        "price": format(reference.price, "f"),
        "buy_price": format(buy.price, "f") if buy else None,
        "sell_price": format(sell.price, "f") if sell else None,
        "buy_price_basis": buy.price_basis if buy else None,
        "sell_price_basis": sell.price_basis if sell else None,
        "buy_approximate": buy.approximate if buy else None,
        "sell_approximate": sell.approximate if sell else None,
        "buy_reason": reasons.get("BUY"),
        "sell_reason": reasons.get("SELL"),
    }


@app.post("/paper/market/buy")
def paper_market_buy(request: PaperMarketBuyRequest, db = Depends(get_db)):
    """Compra ficticia con precio ask (o último aproximado) obtenido en servidor."""
    return _paper_public_market_order(request, "BUY", db)


@app.post("/paper/market/sell")
def paper_market_sell(request: PaperMarketSellRequest, db = Depends(get_db)):
    """Venta ficticia con precio bid (o último aproximado) obtenido en servidor."""
    return _paper_public_market_order(request, "SELL", db)


@app.post("/paper/scenarios", status_code=201)
def add_paper_scenario(request: PaperScenarioCreate, db = Depends(get_db)):
    """
    Abre una sesión PAPER nueva para una moneda cotizada.

    Solo puede haber una sesión abierta por moneda: si ya existe se devuelve
    `409`. Las sesiones cerradas se conservan (generación, fechas y saldos) y
    sus operaciones siguen visibles en `/paper/operations`.
    """
    if get_paper_scenario(db, request.quote_currency) is not None:
        raise HTTPException(status_code=409, detail="Paper scenario already exists")

    try:
        scenario = create_paper_scenario(db=db, **request.model_dump())
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Paper scenario already exists") from exc

    return exact_json(paper_scenario_snapshot(db, scenario), status_code=201)


@app.post("/paper/scenarios/{quote_currency}/close")
def close_scenario(quote_currency: str, db = Depends(get_db)):
    """Cierra la sesión abierta de la moneda; el historial permanece intacto."""
    scenario = get_paper_scenario(db, quote_currency)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Paper scenario not found")
    return exact_json(paper_scenario_snapshot(db, close_paper_scenario(db, scenario)))


@app.get("/paper/scenarios")
def list_paper_scenarios(db = Depends(get_db)):
    """Lista las sesiones PAPER abiertas: capital, límites y saldo."""
    return exact_json([paper_scenario_snapshot(db, item) for item in get_paper_scenarios(db)])


@app.get("/paper/sessions")
def list_paper_sessions(db = Depends(get_db)):
    """Histórico de sesiones PAPER (abiertas y cerradas) con sus saldos."""
    sessions = get_paper_scenarios(db, open_only=False)
    return exact_json([paper_scenario_snapshot(db, item) for item in sessions])


@app.get("/paper/scenarios/{quote_currency}")
def read_paper_scenario(quote_currency: str, db = Depends(get_db)):
    """Muestra configuración y saldo restante de la sesión PAPER abierta."""
    scenario = get_paper_scenario(db, quote_currency)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Paper scenario not found")
    return exact_json(paper_scenario_snapshot(db, scenario))


def _resolve_open_scenario(db, quote_currency: str | None):
    """Elige la sesión abierta; exige la moneda si hay varias."""
    if quote_currency is not None:
        scenario = get_paper_scenario(db, quote_currency)
        if scenario is None:
            raise HTTPException(status_code=404, detail="Paper scenario not found")
        return scenario

    open_scenarios = get_paper_scenarios(db, open_only=True)
    if not open_scenarios:
        raise HTTPException(status_code=404, detail="Paper scenario not found")
    if len(open_scenarios) > 1:
        raise HTTPException(
            status_code=400,
            detail="Specify quote_currency",
        )
    return open_scenarios[0]


@app.post("/paper/sell")
def paper_sell(
    request: PaperSellRequest,
    db = Depends(get_db)
):
    """
    Ejecuta una venta simulada sobre el inventario de la sesión abierta.

    Guard valida precio, existencias y comisión antes de que Paper Trading
    registre la operación en el Ledger con el neto acreditado.
    """

    if (request.exchange or "").strip().lower() == "grvt" or ":PERP" in request.quote_currency.upper():
        raise HTTPException(status_code=422, detail="PAPER GRVT PERP requiere motor de derivados; no se registra como spot")
    scenario = get_paper_scenario(db, request.quote_currency)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Paper scenario not found")

    try:
        result = execute_configured_paper_sell(
            db=db,
            scenario=scenario,
            request_id=str(request.request_id),
            asset=request.asset,
            quantity=request.quantity,
            price=request.price,
            fee=request.fee,
            exchange=request.exchange,
        )
    except PaperRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return exact_json(result)


@app.get("/paper/portfolio")
def paper_portfolio(
    quote_currency: str | None = None,
    db = Depends(get_db)
):
    """
    Saldo, posiciones y P/L de una sesión PAPER en `Decimal`.

    El proveedor configurado en `OrbisConfig.price_provider` aporta el precio
    de referencia y sus metadatos cuando están disponibles. Sin cotización se
    usa el último precio negociado o el coste medio, marcando el resultado
    como estimado en vez de atribuirle vigencia de mercado.
    """
    scenario = _resolve_open_scenario(db, quote_currency)
    return exact_json(build_paper_portfolio(db, scenario))


@app.get("/paper/operations")
def paper_operations(
    asset: str | None = None,
    quote: str | None = None,
    quote_currency: str | None = None,
    side: Literal["BUY", "SELL"] | None = None,
    date: str | None = None,
    start_at: datetime | None = None,
    end_before: datetime | None = None,
    strategy_id: int | None = None,
    limit: int | None = Query(default=None, gt=0, le=1000),
    order: Literal["asc", "desc"] = Query(default="asc"),
    pnl: bool = Query(default=False),
    db = Depends(get_db)
):
    """
    Operaciones PAPER del Ledger ordenadas por `(timestamp, id)`.

    Filtros opcionales: `asset`, `quote` (alias `quote_currency`), `side`
    (`BUY`/`SELL`), `date` (`YYYY-MM-DD`, día UTC completo), `start_at`
    inclusivo y `end_before` exclusivo (ISO 8601 con zona), y `strategy_id`.
    Incluye las sesiones cerradas: es el historial de la instancia.
    `limit` recorta al final (aditivo, por defecto sin cambios) y `order`
    (`asc` por defecto, `desc` invierte) solo reordena lo filtrado.
    Con `pnl=true` cada fila añade `cost_basis`, `proceeds`, `fees`,
    `realized_pnl` y `pnl_pct` (Decimal del servidor, sin recalcular en el
    cliente); el PnL se calcula sobre el historial PAPER completo en orden
    `(timestamp, id)` y los filtros/`limit`/`order` se aplican después para
    no falsear el coste medio. Sin `pnl` la respuesta es la de siempre.
    Con `pnl=true` los campos `cost_basis`/`proceeds`/`fees`/`realized_pnl`/
    `pnl_pct` se calculan sobre TODO el historial del Ledger (visión global
    de la instancia) y pueden diferir del coste medio de la sesión activa
    que muestra `/paper/portfolio` (visión por sesión) cuando hay sesiones
    cerradas con el mismo par.
    """
    start = None
    end = None
    if date is not None:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid date; expected YYYY-MM-DD",
            ) from exc
        start = day.replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    if start_at is not None and start_at.tzinfo is None:
        raise HTTPException(status_code=400, detail="start_at requires a timezone")
    if end_before is not None and end_before.tzinfo is None:
        raise HTTPException(status_code=400, detail="end_before requires a timezone")
    if start_at is not None:
        boundary = start_at.astimezone(timezone.utc)
        start = max(start, boundary) if start is not None else boundary
    if end_before is not None:
        boundary = end_before.astimezone(timezone.utc)
        end = min(end, boundary) if end is not None else boundary
    if start_at is not None and end_before is not None and start_at >= end_before:
        raise HTTPException(status_code=400, detail="start_at must precede end_before")

    items = list_paper_operations(
        db,
        asset=asset,
        quote_currency=quote_currency if quote_currency is not None else quote,
        side=side,
        strategy_id=strategy_id,
        start=start,
        end=end,
    )
    if pnl:
        # M2: el PnL sale del historial PAPER completo en (timestamp, id)
        # con el mismo pliegue de coste medio que build_positions; lo que
        # pide el cliente (filtros/limit/order) solo recorta después.
        from app.math.positions import operation_pnl

        annotated = operation_pnl(list_paper_operations(db))
        items = [
            {
                **exact_jsonable(operation),
                "cost_basis": details["cost_basis"],
                "proceeds": details["proceeds"],
                "fees": details["fees"],
                "realized_pnl": details["realized_pnl"],
                "pnl_pct": details["pnl_pct"],
            }
            for operation in items
            for details in (annotated[operation.id],)
        ]
    if order == "desc":
        items = list(reversed(items))
    if limit is not None:
        items = items[:limit]
    return exact_json(items)


@app.get("/paper/operations/summary")
def paper_operations_summary(
    asset: str | None = None,
    quote: str | None = None,
    quote_currency: str | None = None,
    side: Literal["BUY", "SELL"] | None = None,
    date: str | None = None,
    strategy_id: int | None = None,
    db = Depends(get_db)
):
    """
    Resumen del Ledger PAPER con los mismos filtros que `/paper/operations`.

    Reutiliza `list_paper_operations` + `_sum_amounts` + `Operation.total_fees`
    (sin duplicar lógica): conteos, importes comprados/vendidos, volumen y
    comisiones totales, todo en `Decimal`.
    """
    start = None
    end = None
    if date is not None:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid date; expected YYYY-MM-DD",
            ) from exc
        start = day.replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)

    items = list_paper_operations(
        db,
        asset=asset,
        quote_currency=quote_currency if quote_currency is not None else quote,
        side=side,
        strategy_id=strategy_id,
        start=start,
        end=end,
    )
    buys = [op for op in items if op.operation_type == "BUY"]
    sells = [op for op in items if op.operation_type == "SELL"]
    bought_amount = _sum_amounts(op.amount_spent for op in buys)
    sold_amount = _sum_amounts(op.amount_spent for op in sells)
    with localcontext() as context:
        context.prec = 80
        volume = bought_amount + sold_amount
    return exact_json({
        "operations_count": len(items),
        "buys_count": len(buys),
        "sells_count": len(sells),
        "bought_amount": bought_amount,
        "sold_amount": sold_amount,
        "volume": volume,
        "total_fees": _sum_amounts(op.total_fees for op in items),
    })


@app.get("/paper/prices")
def list_session_prices(db = Depends(get_db)):
    """Cotizaciones manuales publicadas (solo valoración, no ejecución)."""
    return exact_json([
        {
            "asset": row.asset,
            "quote_currency": row.quote_currency,
            "price": row.price,
            "source": row.source,
            "updated_at": row.updated_at,
        }
        for row in list_prices(db)
    ])


@app.post("/paper/prices", status_code=201)
def publish_price(request: PaperPriceRequest, db = Depends(get_db)):
    """Publica o actualiza la cotización manual de un par (valoración, no ejecución)."""
    row = upsert_price(
        db,
        asset=request.asset,
        quote_currency=request.quote_currency,
        price=request.price,
    )
    return exact_json({
        "asset": row.asset,
        "quote_currency": row.quote_currency,
        "price": row.price,
        "source": row.source,
        "updated_at": row.updated_at,
    }, status_code=201)
