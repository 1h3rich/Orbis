"""API local del Laboratorio; nunca crea órdenes reales ni PAPER."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from app.security.local_session import LocalStoreUnlock
from typing import Annotated, Any, Callable, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.lab.analytics import equity_risk
from app.lab.ai_corpus import MAX_RECORDS as MAX_AI_CORPUS_RECORDS, build_ai_corpus
from app.lab.ai_research import (MODEL as AI_MODEL, analyze as analyze_research,
                                 backtest_evidence, comparison_evidence, evidence_digest, research_evidence)
from app.lab.access import lab_access, lab_format
from app.lab.backup import list_lab_backups
from app.lab.coverage import higher_timeframe_coverage
from app.lab.export_split import partition_evidence
from app.lab.secure_backup import create_secure_lab_backup
from app.lab.sequence_risk import sequence_risk
from app.lab.engine import CURRENT_ENGINE_VERSION, INTERVALS, MAX_DATASET_CANDLES, LabError, run_backtest, supports_engine_version, validate_candles, validate_strategy
from app.lab.perp_rules import RuleObservationStore, Symbol as PerpSymbol, capture_rule_observation
from app.lab.optimization import (OPTIMIZER_VERSION, WALK_FORWARD_OPTIMIZER_VERSION, optimize_stops,
                                  walk_forward_stops)
from app.lab.research import (ENGINE_VERSION as RESEARCH_ENGINE_VERSION,
                              LEGACY_ENGINE_VERSION as LEGACY_RESEARCH_ENGINE_VERSION,
                              SEGMENTED_ENGINE_VERSION as SEGMENTED_RESEARCH_ENGINE_VERSION,
                              run_research, validate_research_rule)
from app.lab.research_cohort import MAX_COHORT_RUNS, build_research_cohort
from app.lab.store import LabStore, content_hash, encoded
from app.lab.validation import (ROLLING_ENGINE_VERSION, VALIDATION_ENGINE_VERSION,
                                chronological_holdout, rolling_fixed_validation)
from app.lab.verification import verify_backtest_run
from app.lab.experiment_verification import verify_lab_experiment
from app.research.references import ResearchReferenceError, strategy_reference
from app.lab.volatility_regimes import analyze_volatility_regimes
from app.marketdata.adapters.binance import BinanceAdapter
from app.marketdata.adapters.grvt import GrvtAdapter, GrvtHistoryError
from app.marketdata.adapters.mexc import MEXC_HISTORICAL_PAGE_SIZE, MexcAdapter
from app.marketdata.normalize import parse_symbol
from app.security.sensitive_text import contains_sensitive_ai_data, contains_sensitive_ai_text


ENGINE_VERSION = CURRENT_ENGINE_VERSION
STRATEGY_BUNDLE_SCHEMA = "orbis.lab.strategy.v1"
LOCAL_HOSTS = frozenset(("127.0.0.1", "::1", "localhost", "testserver", "testclient"))
PerpCapture = TypeVar("PerpCapture")


def chart_series_precision(saved: dict) -> dict:
    """La gráfica ya usa Number; nunca redondear el run, riesgo o exportación exactos."""
    def approximate(value: object) -> str:
        try:
            number = Decimal(str(value))
            return format(number, ".12g") if number.is_finite() else str(value)
        except (InvalidOperation, ValueError):
            return str(value)

    return {**saved,
            "equity": [{"t": row["t"], "equity": approximate(row["equity"])} for row in saved["equity"]],
            "drawdown": [{"t": row["t"], "drawdown_pct": approximate(row["drawdown_pct"])}
                         for row in saved["drawdown"]]}


def chart_response(saved: dict, risk: dict, *, curves_windowed: bool) -> dict:
    """Respuesta visual; los puntos exactos siguen en el run y su endpoint."""
    if curves_windowed:
        return {**saved, "equity": [], "drawdown": [], "curve_point_count": len(saved["equity"]),
                "curves_windowed": True, "risk": risk}
    return {**chart_series_precision(saved), "risk": risk}


def _plot_points(rows: list[dict], *fields: str) -> list[dict]:
    """Serializa sólo los números que el gráfico consume como IEEE 754."""
    try:
        points = [{"t": row["t"], **{field: float(row[field]) for field in fields}} for row in rows]
        if any(not isfinite(point[field]) for point in points for field in fields):
            raise ValueError("non-finite plot point")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise LabError("El tramo contiene valores que el gráfico no puede representar") from None
    return points


def _validate_chart_window(from_t: int, to_t: int, limit: int, view: Literal["exact", "plot"]) -> None:
    if from_t > to_t:
        raise HTTPException(status_code=422, detail="El inicio debe preceder al final")
    if view == "exact" and limit > 2000:
        raise HTTPException(status_code=422, detail="La consulta exacta admite hasta 2.000 puntos por página")


def _time_to_extreme(event: dict, field: str, full_week: bool) -> int | None:
    """Etiqueta posterior derivada; nunca se usa al evaluar la señal."""
    stamp = event[field] if full_week else None
    return stamp - event["signal_time"] if stamp is not None else None


class DatasetImport(BaseModel):
    symbol: str = Field(max_length=25)
    timeframe: str
    source: str = Field(min_length=1, max_length=100)
    candles: list[dict[str, Any]] = Field(min_length=3, max_length=MAX_DATASET_CANDLES)


class LabBackupPassphrase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: SecretStr = Field(min_length=12, max_length=1024)


class LabUnlockRequest(LocalStoreUnlock):
    pass


class DatasetDownload(BaseModel):
    symbol: str = Field(max_length=25)
    timeframe: str
    provider: Literal["binance", "mexc", "grvt"] = "binance"
    start: int
    end: int


class PerpDatasetDownload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: PerpSymbol
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"]
    start: int = Field(gt=0)
    end: int = Field(gt=0)


class PerpRuleCapture(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    symbol: PerpSymbol


class PaperSeedRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_dataset_id: str = Field(min_length=1, max_length=100)


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    definition: dict[str, Any]


class StrategyVersionCreate(BaseModel):
    definition: dict[str, Any]


class StrategyBundleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    version_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1, strict=True)
    created_at: str = Field(min_length=1, max_length=40)
    definition: dict[str, Any]


class StrategyBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_id: Literal["orbis.lab.strategy.v1"] = Field(alias="schema")
    environment: Literal["LAB"]
    theoretical_only: Literal[True]
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy: StrategyBundleDefinition


class RunCreate(BaseModel):
    dataset_id: str
    version_id: str


class RunComparisonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left_id: str = Field(min_length=1, max_length=100)
    right_id: str = Field(min_length=1, max_length=100)


class ValidationCreate(RunCreate):
    train_fraction: str = "0.7"


class RollingCreate(RunCreate):
    folds: int = Field(default=3, ge=2, le=5, strict=True)


class OptimizationCreate(ValidationCreate):
    stop_losses: list[str] = Field(min_length=1, max_length=3)
    take_profits: list[str] = Field(min_length=1, max_length=3)


class WalkForwardOptimizationCreate(RunCreate):
    model_config = ConfigDict(extra="forbid")
    stop_losses: list[str] = Field(min_length=1, max_length=3)
    take_profits: list[str] = Field(min_length=1, max_length=3)
    folds: int = Field(default=3, ge=2, le=5, strict=True)
    final_holdout_fraction: str = "0.2"


class ResearchRunCreate(BaseModel):
    dataset_id: str
    rule: dict[str, Any]


class ResearchCohortExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=2, max_length=MAX_COHORT_RUNS)
    boundary_open_t: int = Field(strict=True, gt=0, lt=2**53)


class ResearchAiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    question: str = Field(min_length=3, max_length=1000)
    api_key: SecretStr = Field(min_length=10, max_length=1024)
    save_locally: bool = False


class ComparisonAiRequest(RunComparisonCreate, ResearchAiRequest):
    confirmation: Literal["ENVIAR COMPARACIÓN A OPENAI"]


class AiCorpusRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AiCorpusExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[AiCorpusRecord] = Field(min_length=1, max_length=MAX_AI_CORPUS_RECORDS)
    confirmation: Literal["EXPORTAR JSONL EN CLARO"]


class DeleteAiAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["BORRAR ANÁLISIS"]


def download_binance(symbol: str, timeframe: str, start: int, end: int, *,
                     adapter: BinanceAdapter | MexcAdapter | None = None,
                     now: int | None = None) -> list[dict]:
    """Paginación hacia adelante por startTime explícito; sin velas abiertas."""
    if timeframe not in INTERVALS:
        raise LabError("Temporalidad no admitida")
    step = INTERVALS[timeframe]
    current = now or int(datetime.now(timezone.utc).timestamp())
    if start <= 0 or end <= start or end > current or start % step or end % step or (end - start) // step > MAX_DATASET_CANDLES:
        raise LabError(f"Rango temporal inválido o superior a {MAX_DATASET_CANDLES} velas")
    pair = "/".join(parse_symbol(symbol))
    source = adapter or BinanceAdapter()
    if pair not in source.known_pairs:
        raise LabError("Par no soportado por la descarga Spot actual")
    rows: list[dict] = []
    cursor = start
    mexc = isinstance(source, MexcAdapter)
    while cursor < end:
        # MEXC observado: 500 velas por respuesta; startTime sin endTime puede
        # devolver la ventana reciente en vez del inicio solicitado.
        page_limit = min(MEXC_HISTORICAL_PAGE_SIZE if mexc else 1000, (end - cursor) // step)
        page = source.get_candles(pair, timeframe=timeframe, limit=page_limit,
                                  since=cursor, **({"before": end} if mexc else {}))
        if not page:
            raise LabError("Proveedor sin respuesta; no se guarda un histórico parcial")
        selected = [row for row in page if cursor <= int(row["t"]) < end and int(row["t"]) + step <= current]
        if not selected:
            break
        rows.extend(selected)
        next_cursor = max(int(row["t"]) for row in selected) + step
        if next_cursor <= cursor:
            raise LabError("El proveedor no avanzó en la paginación")
        cursor = next_cursor
    if not rows or int(rows[0]["t"]) != start or int(rows[-1]["t"]) != end - step:
        raise LabError("Proveedor devolvió cobertura incompleta del rango solicitado")
    return rows


def download_grvt(symbol: str, timeframe: str, start: int, end: int, *,
                  adapter: GrvtAdapter | None = None, now: int | None = None) -> list[dict]:
    """Perpetuo TRADE cerrado; el adaptador pagina hacia atrás y exige cobertura."""
    source = adapter or GrvtAdapter()
    if timeframe not in INTERVALS:
        raise LabError("Temporalidad no admitida en Laboratorio")
    if symbol not in source.known_pairs:
        raise LabError("GRVT requiere un perpetuo explícito BASE/USDT:PERP admitido")
    try:
        return source.get_historical_candles(symbol, timeframe, start, end, now=now)
    except GrvtHistoryError as exc:
        raise LabError(str(exc)) from exc


def create_lab_router(path: Path) -> APIRouter:
    store = LabStore(path, allow_create=False)
    router = APIRouter(prefix="/lab", tags=["laboratory"])

    def paper_target(saved: dict, dataset: dict, version: dict) -> dict | None:
        """A navigation target for the exact verified inputs; never activation."""
        if (dataset["id"] != saved["dataset_id"] or version["id"] != saved["version_id"]
                or dataset["hash"] != saved["dataset_hash"]
                or dataset["source"] != saved["dataset_source"]
                or content_hash(version["definition"]) != saved["strategy_hash"]):
            raise LabError("Las entradas cambiaron durante el diagnóstico de cobertura")
        venue = None
        if dataset["verified_public"]:
            if dataset["source"] == "mexc-spot-public" and version["definition"]["direction"] == "LONG":
                venue = "SPOT"
            elif dataset["source"] == "grvt-perp-trade-public" and dataset["symbol"].endswith(":PERP"):
                venue = "GRVT"
        return ({"venue": venue, "run_id": saved["id"], "dataset_id": saved["dataset_id"],
                 "strategy_id": version["strategy_id"], "version_id": saved["version_id"]}
                if venue else None)

    def local_only(request: Request) -> None:
        client_host = request.client.host if request.client else None
        if client_host not in LOCAL_HOSTS or request.url.hostname not in LOCAL_HOSTS:
            raise HTTPException(status_code=403, detail="Laboratorio sólo local")
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Origen externo rechazado")
        route = request.url.path
        if route in ("/lab/status", "/lab/unlock", "/lab/create", "/lab/lock",
                     "/lab/backups", "/lab/backup"):
            return
        try:
            kind = lab_format(store.path)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if kind == "encrypted_or_invalid" and lab_access.get(store.path) is None:
            raise HTTPException(status_code=423, detail="LAB cifrado bloqueado; desbloquéalo en Ajustes")
        if kind in ("missing", "empty"):
            empty_lists = {"/lab/datasets", "/lab/strategies", "/lab/strategy-versions",
                           "/lab/runs", "/lab/validations", "/lab/optimizations",
                           "/lab/research/runs", "/lab/perp/datasets",
                           "/lab/perp/profiles", "/lab/perp/runs"}
            if request.method != "GET" or route not in empty_lists:
                raise HTTPException(status_code=423, detail="Crea primero la base LAB cifrada en Ajustes")

    def fail(exc: LabError) -> HTTPException:
        return HTTPException(status_code=422, detail=str(exc))

    from app.lab.perp_api import create_perp_experiment_router
    router.include_router(create_perp_experiment_router(store),dependencies=[Depends(local_only)])

    def with_reserve_reuse(kind: Literal["validation", "optimization"],
                           saved: dict) -> dict:
        return {**saved, "reserve_reuse": store.reserve_reuse(kind, saved)}

    @router.get("/status", dependencies=[Depends(local_only)])
    def lab_status(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return lab_access.status(store.path)
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/unlock", dependencies=[Depends(local_only)])
    def unlock_lab(body: LabUnlockRequest, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return lab_access.unlock(store.path, body.passphrase.get_secret_value(),
                                          session_seconds=body.session_seconds)
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/create", dependencies=[Depends(local_only)])
    def create_encrypted_lab(body: LabUnlockRequest, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return lab_access.create(store.path, body.passphrase.get_secret_value(),
                                          session_seconds=body.session_seconds)
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/lock", dependencies=[Depends(local_only)])
    def lock_lab(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return lab_access.lock(store.path)
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/backup", dependencies=[Depends(local_only)])
    def backup_lab() -> dict:
        raise HTTPException(
            status_code=410,
            detail="La creación de copias LAB sin cifrar se retiró; usa /lab/backup/encrypted",
        )

    @router.post("/backup/encrypted", dependencies=[Depends(local_only)])
    def backup_lab_encrypted(body: LabBackupPassphrase, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return create_secure_lab_backup(store, body.passphrase.get_secret_value())
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/backups", dependencies=[Depends(local_only)])
    def backups_lab() -> list[dict]:
        return list_lab_backups(store)

    @router.get("/perp/datasets", dependencies=[Depends(local_only)])
    def perp_datasets(response: Response) -> list[dict]:
        from app.lab.perp_store import PerpDatasetStore
        response.headers["Cache-Control"] = "no-store"
        try:
            return PerpDatasetStore(store).list()
        except LabError as exc:
            raise fail(exc) from exc

    def save_perp_capture(load: Callable[[],PerpCapture], save: Callable[[PerpCapture],dict]) -> dict:
        epoch = lab_access.epoch(store.path)
        if epoch is None or lab_format(store.path) != "encrypted_or_invalid":
            raise LabError("Los datos perpetuos requieren LAB cifrado y desbloqueado")
        before = store.path.stat()
        captured = load()
        with lab_access.guard(store.path,epoch):
            after = store.path.stat()
            if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):
                raise LabError("LAB cambió durante la descarga; no se guardó")
            return save(captured)

    @router.post("/perp/datasets/download", dependencies=[Depends(local_only)])
    def download_perp_dataset(body: PerpDatasetDownload, response: Response) -> dict:
        from app.lab.perp_dataset import prepare_perp_dataset
        from app.lab.perp_store import PerpDatasetStore
        response.headers["Cache-Control"] = "no-store"
        try:
            return save_perp_capture(lambda: prepare_perp_dataset(body.symbol,body.timeframe,body.start,body.end),
                                     PerpDatasetStore(store).save)
        except (ValueError, OSError) as exc:
            raise fail(LabError(str(exc))) from exc

    @router.get("/perp/rules",dependencies=[Depends(local_only)])
    def perp_rule_observations(symbol: PerpSymbol,response: Response,
                               before_ns: int | None = Query(default=None,ge=1,lt=2**63),
                               before_hash: str | None = Query(default=None,pattern=r'^[0-9a-f]{64}$')) -> dict:
        response.headers["Cache-Control"]="no-store"
        if (before_ns is None)!=(before_hash is None):
            raise fail(LabError("Cursor de observaciones incompleto"))
        try:
            cursor = (before_ns,before_hash) if before_ns is not None and before_hash is not None else None
            return RuleObservationStore(store).page(symbol,before=cursor)
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/perp/rules/capture",dependencies=[Depends(local_only)])
    def capture_perp_rules(body: PerpRuleCapture,response: Response) -> dict:
        response.headers["Cache-Control"]="no-store"
        try:
            return save_perp_capture(lambda: capture_rule_observation(body.symbol),RuleObservationStore(store).save)
        except (ValueError,OSError) as exc:
            raise fail(LabError(str(exc))) from exc

    @router.get("/perp/datasets/{dataset_id}", dependencies=[Depends(local_only)])
    def perp_dataset(dataset_id: str, response: Response) -> dict:
        from app.lab.perp_store import PerpDatasetStore, summary
        response.headers["Cache-Control"] = "no-store"
        try:
            return summary(*PerpDatasetStore(store).read(dataset_id))
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/perp/datasets/{dataset_id}/export", dependencies=[Depends(local_only)])
    def export_perp_dataset(dataset_id: str, response: Response) -> dict:
        from app.lab.perp_store import PerpDatasetStore
        response.headers["Cache-Control"] = "no-store"
        try:
            header, dataset = PerpDatasetStore(store).read(dataset_id)
            response.headers["Content-Disposition"] = 'attachment; filename="orbis-perp-'+header['id']+'.json"'
            return {**header, "dataset":dataset.payload()}
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/datasets", dependencies=[Depends(local_only)])
    def import_dataset(body: DatasetImport) -> dict:
        try:
            pair = "/".join(parse_symbol(body.symbol))
            bars, gaps = validate_candles(body.candles, body.timeframe, now=int(datetime.now(timezone.utc).timestamp()))
            return store.save_dataset(pair, body.timeframe, body.source.strip(), bars, gaps)
        except (LabError, ValueError) as exc:
            raise fail(LabError(str(exc))) from exc

    @router.post("/datasets/download", dependencies=[Depends(local_only)])
    def download_dataset(body: DatasetDownload) -> dict:
        try:
            pair = "/".join(parse_symbol(body.symbol))
            now = int(datetime.now(timezone.utc).timestamp())
            if body.provider == "grvt":
                rows = download_grvt(pair, body.timeframe, body.start, body.end, now=now)
                source = "grvt-perp-trade-public"
            elif body.provider == "mexc":
                rows = download_binance(pair, body.timeframe, body.start, body.end,
                                        adapter=MexcAdapter(), now=now)
                source = "mexc-spot-public"
            else:
                rows = download_binance(pair, body.timeframe, body.start, body.end, now=now)
                source = "binance-spot-public"
            bars, gaps = validate_candles(rows, body.timeframe, now=now)
            if gaps:
                raise LabError("El proveedor devolvió un histórico con huecos")
            return store.save_dataset(pair, body.timeframe, source, bars, gaps, verified_public=True)
        except (LabError, ValueError) as exc:
            raise fail(LabError(str(exc))) from exc

    @router.post("/datasets/refresh-for-paper", dependencies=[Depends(local_only)])
    def refresh_paper_seed(body: PaperSeedRefresh) -> dict:
        """Nuevo dataset público reciente; conserva intacto el backtest antiguo."""
        try:
            original, _ = store.dataset(body.source_dataset_id)
            source = original["source"]
            if not original["verified_public"] or source not in (
                    "mexc-spot-public", "grvt-perp-trade-public"):
                raise LabError("Sólo se puede actualizar un histórico público MEXC Spot o GRVT PERP")
            timeframe = original["timeframe"]
            if timeframe not in INTERVALS:
                raise LabError("Temporalidad no admitida para PAPER")
            now = int(datetime.now(timezone.utc).timestamp())
            step = INTERVALS[timeframe]
            end = now // step * step
            start = end - 1000 * step
            symbol = original["symbol"]
            if source == "mexc-spot-public":
                rows = download_binance(symbol, timeframe, start, end,
                                        adapter=MexcAdapter(), now=now)
            else:
                rows = download_grvt(symbol, timeframe, start, end,
                                     adapter=GrvtAdapter(), now=now)
            bars, gaps = validate_candles(rows, timeframe, now=now)
            if gaps or len(bars) != 1000 or bars[-1].t != end - step:
                raise LabError("El proveedor no cubrió las 1000 velas cerradas; no se guardó un histórico parcial")
            return store.save_refreshed_dataset(original["id"], original["hash"],
                                                symbol, timeframe, source, bars)
        except (LabError, ValueError) as exc:
            raise fail(LabError(str(exc))) from exc

    @router.post("/datasets/btc-archive", dependencies=[Depends(local_only)])
    def import_btc_archive() -> dict:
        """Conserva el archivo BTC/USD diario verificable como dataset LAB."""
        from app.marketdata.archive import btc_daily_archive

        archive = btc_daily_archive()
        if not archive.get("available"):
            raise fail(LabError(str(archive.get("reason") or "Archivo BTC no disponible")))
        if archive.get("stale") or archive.get("cache_status") == "local-stale":
            raise fail(LabError("El archivo BTC local está desactualizado; actualiza Bitstamp antes de importarlo en LAB"))
        try:
            now = int(datetime.now(timezone.utc).timestamp())
            bars, gaps = validate_candles(archive["candles"], "1d", now=now)
            if gaps:
                raise LabError("Bitstamp devolvió días sin vela; no se guarda un histórico parcial para backtesting")
            if bars[0].t != int(archive["first_candle_at"]) or bars[-1].t != int(archive["last_candle_at"]):
                raise LabError("La cobertura del archivo BTC no coincide con las velas descargadas")
            return store.save_dataset("BTC/USD", "1d", "bitstamp-btcusd-daily-public", bars, gaps,
                                      verified_public=True)
        except (KeyError, LabError, TypeError, ValueError) as exc:
            raise fail(LabError(str(exc))) from exc

    @router.get("/datasets", dependencies=[Depends(local_only)])
    def list_datasets() -> list[dict]:
        return store.datasets()

    @router.get("/datasets/{dataset_id}/lineage", dependencies=[Depends(local_only)])
    def dataset_lineage(dataset_id: str) -> list[dict]:
        try:
            return store.dataset_lineage(dataset_id)
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/paper-gap-reports/{report_id}", dependencies=[Depends(local_only)])
    def paper_gap_report(report_id: str, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.paper_gap_report(report_id)
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/datasets/{dataset_id}", dependencies=[Depends(local_only)])
    def get_dataset(dataset_id: str, response: Response,
                    view: Literal["full", "chart"] = Query(default="full")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            meta, candles = store.dataset(dataset_id)
            if view == "chart":
                return {**meta, "candles": [], "times": [bar["t"] for bar in candles],
                        "candles_windowed": True}
            return {**meta, "candles": candles}
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/datasets/{dataset_id}/candles", dependencies=[Depends(local_only)])
    def get_dataset_candles(dataset_id: str, response: Response,
                            from_t: int = Query(ge=0, le=253402300799),
                            to_t: int = Query(ge=0, le=253402300799),
                            after_index: int = Query(default=-1, ge=-1),
                            limit: int = Query(default=2000, ge=1, le=MAX_DATASET_CANDLES),
                            view: Literal["exact", "plot"] = Query(default="exact")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        _validate_chart_window(from_t, to_t, limit, view)
        epoch = lab_access.epoch(store.path) if view == "plot" else None
        try:
            page = store.dataset_chart_candles(dataset_id, from_t, to_t, after_index, limit)
            if view == "plot":
                page["candles"] = _plot_points(page["candles"], "o", "h", "l", "c")
                with lab_access.guard(store.path, epoch):
                    return page
            return page
        except LabError as exc:
            status = 404 if str(exc) == "Dataset no encontrado" else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.post("/strategies", dependencies=[Depends(local_only)])
    def create_strategy(body: StrategyCreate) -> dict:
        try:
            return store.create_strategy(body.name, validate_strategy(body.definition))
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/strategies/{strategy_id}/versions", dependencies=[Depends(local_only)])
    def new_version(strategy_id: str, body: StrategyVersionCreate) -> dict:
        try:
            return store.add_version(strategy_id, validate_strategy(body.definition))
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/strategies/{strategy_id}/versions/{version_id}/export",
                dependencies=[Depends(local_only)])
    def export_strategy(strategy_id: str, version_id: str, response: Response) -> dict:
        try:
            source = store.strategy_version(strategy_id, version_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            canonical = validate_strategy(source["definition"])
        except LabError:
            raise HTTPException(status_code=409, detail="La estrategia guardada no es válida") from None
        if content_hash(canonical) != content_hash(source["definition"]):
            raise HTTPException(status_code=409, detail="La definición guardada no es canónica")
        response.headers["Cache-Control"] = "no-store"
        return {"schema": STRATEGY_BUNDLE_SCHEMA, "environment": "LAB",
                "theoretical_only": True, "definition_hash": content_hash(canonical),
                "strategy": source}

    @router.post("/strategies/import", dependencies=[Depends(local_only)])
    def import_strategy(bundle: StrategyBundle) -> dict:
        if len(encoded(bundle.model_dump())) > 100_000:
            raise HTTPException(status_code=422, detail="Archivo de estrategia demasiado grande")
        try:
            canonical = validate_strategy(bundle.strategy.definition)
            if (content_hash(bundle.strategy.definition) != bundle.definition_hash
                    or content_hash(canonical) != bundle.definition_hash):
                raise LabError("La huella de la estrategia no coincide")
            name = (bundle.strategy.name.strip()[:87] + " · importada")
            origin = {
                "strategy_id": bundle.strategy.id,
                "version_id": bundle.strategy.version_id,
                "version": bundle.strategy.version,
                "definition_hash": bundle.definition_hash}
            return store.import_strategy(name, canonical, origin)
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/strategies", dependencies=[Depends(local_only)])
    def list_strategies() -> list[dict]:
        return store.strategies()

    @router.get("/strategy-versions", dependencies=[Depends(local_only)])
    def list_all_strategy_versions() -> list[dict]:
        return store.all_versions()

    @router.get("/strategies/{strategy_id}/versions", dependencies=[Depends(local_only)])
    def list_versions(strategy_id: str) -> list[dict]:
        return store.versions(strategy_id)

    @router.post("/runs", dependencies=[Depends(local_only)])
    def execute(body: RunCreate, response: Response,
                view: Literal["full", "chart"] = Query(default="full"),
                markers: Literal["all", "window"] = Query(default="all"),
                curves: Literal["all", "window"] = Query(default="all")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        if view != "chart" and curves == "window":
            raise HTTPException(status_code=422, detail="La ventana de curvas requiere view=chart")
        try:
            dataset, raw = store.dataset(body.dataset_id)
            if dataset["gaps"]:
                raise LabError("El dataset tiene huecos; se rechaza el backtest hasta corregirlos")
            version = store.version(body.version_id)
            bars, _ = validate_candles(raw, dataset["timeframe"], now=int(datetime.now(timezone.utc).timestamp()))
            result = run_backtest(bars, version["definition"], dataset["timeframe"],
                                  engine_version=ENGINE_VERSION)
            saved = store.save_run(body.dataset_id, body.version_id, result, ENGINE_VERSION,
                                   expected_dataset_hash=dataset["hash"],
                                   expected_strategy_hash=content_hash(version["definition"]))
            displayed = store.run_chart_view(saved["id"], include_markers=markers != "window") if view == "chart" else saved
            risk = equity_risk(displayed["equity"])
            return chart_response(displayed, risk, curves_windowed=curves == "window") if view == "chart" else {**displayed, "risk": risk}
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/runs", dependencies=[Depends(local_only)])
    def list_runs(response: Response) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return store.runs()

    @router.post("/runs/{run_id}/replay-as-new", dependencies=[Depends(local_only)])
    def replay_as_new(run_id: str, response: Response) -> dict:
        """Repite entradas conservadas con el motor actual; nunca sustituye el run anterior."""
        response.headers["Cache-Control"] = "no-store"
        try:
            source = store.run(run_id)
            dataset, raw = store.dataset(source["dataset_id"])
            version = store.version(source["version_id"])
            if (dataset["hash"] != source.get("dataset_hash")
                    or (source.get("dataset_source") is not None
                        and source["dataset_source"] != dataset["source"])
                    or (source.get("strategy_hash") is not None
                        and source["strategy_hash"] != content_hash(version["definition"]))):
                raise HTTPException(status_code=409, detail="Las entradas originales ya no coinciden")
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps:
                raise HTTPException(status_code=409, detail="El histórico original contiene huecos")
            result = run_backtest(bars, version["definition"], dataset["timeframe"],
                                  engine_version=ENGINE_VERSION)
            saved = store.save_run(source["dataset_id"], source["version_id"], result,
                                   ENGINE_VERSION, expected_dataset_hash=source["dataset_hash"],
                                   expected_strategy_hash=content_hash(version["definition"]),
                                   replay_of_run_id=run_id)
            return {"run_id": saved["id"], "replay_of_run_id": run_id,
                    "engine_version": ENGINE_VERSION}
        except HTTPException:
            raise
        except LabError as exc:
            status = 404 if str(exc) == "Backtest no encontrado" else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    def verified_comparison(body: RunComparisonCreate) -> tuple[dict, tuple[tuple[dict, dict, dict], ...]]:
        """Compara retornos normalizados sólo tras repetir ambos runs exactos."""
        if body.left_id == body.right_id:
            raise HTTPException(status_code=422, detail="Elige dos backtests distintos")
        try:
            left, right = store.run(body.left_id), store.run(body.right_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        for saved in (left, right):
            check = verify_backtest_run(store, saved)
            if not check["verified"]:
                raise HTTPException(status_code=409,
                                    detail="No se compara un backtest sin verificación reproducible")
        try:
            left_dataset, _ = store.dataset(left["dataset_id"])
            right_dataset, _ = store.dataset(right["dataset_id"])
            left_version = store.version(left["version_id"])
            right_version = store.version(right["version_id"])
            left_strategy = store.strategy_version(left_version["strategy_id"], left["version_id"])
            right_strategy = store.strategy_version(right_version["strategy_id"], right["version_id"])
        except LabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if (left_dataset["hash"] != left["dataset_hash"]
                or right_dataset["hash"] != right["dataset_hash"]
                or content_hash(left_version["definition"]) != left["strategy_hash"]
                or content_hash(right_version["definition"]) != right["strategy_hash"]):
            raise HTTPException(status_code=409, detail="Las entradas cambiaron durante la comparación")

        def summary(saved: dict, dataset: dict, strategy: dict) -> dict:
            definition = strategy["definition"]
            return {"run_id": saved["id"], "version_id": saved["version_id"],
                    "strategy_name": strategy["name"], "engine_version": saved["engine_version"],
                    "dataset": {key: dataset[key] for key in
                                ("id", "hash", "source", "symbol", "timeframe", "start_t", "end_t", "count")},
                    "metrics": saved["metrics"],
                    "assumptions": {key: definition[key] for key in
                                    ("direction", "initial_capital", "allocation", "fee_rate",
                                     "slippage_rate", "stop_loss", "take_profit")}
                    | {key: definition[key] for key in ("trailing_stop", "max_holding_bars") if key in definition}}

        sources = ((left, left_dataset, left_version), (right, right_dataset, right_version))
        result = {"environment": "LAB", "theoretical_only": True,
                  "left": summary(left, left_dataset, left_strategy),
                  "right": summary(right, right_dataset, right_strategy)}
        if left_dataset["hash"] != right_dataset["hash"]:
            return {**result, "comparable": False,
                    "reason": "Los backtests usan datasets distintos; no se calculan diferencias"}, sources
        if left["engine_version"] != right["engine_version"]:
            return {**result, "comparable": False,
                    "reason": "Los backtests usan motores distintos; no se calculan diferencias"}, sources
        left_metrics, right_metrics = left["metrics"], right["metrics"]
        return {**result, "comparable": True,
                "deltas": {
                    "return_percentage_points": str(Decimal(right_metrics["return_pct"]) - Decimal(left_metrics["return_pct"])),
                    "drawdown_percentage_points": str(Decimal(right_metrics["max_drawdown_pct"]) - Decimal(left_metrics["max_drawdown_pct"])),
                    "trades": right_metrics["trades"] - left_metrics["trades"]},
                "assumptions_equal": result["left"]["assumptions"] == result["right"]["assumptions"],
                "note": "Diferencia derecha menos izquierda en el mismo histórico; no es validación fuera de muestra ni rendimiento real"}, sources

    @router.post("/runs/compare", dependencies=[Depends(local_only)])
    def compare_runs(body: RunComparisonCreate) -> dict:
        comparison, _ = verified_comparison(body)
        return comparison

    @router.get("/runs/{run_id}", dependencies=[Depends(local_only)])
    def get_run(run_id: str, response: Response,
                view: Literal["full", "chart"] = Query(default="full"),
                markers: Literal["all", "window"] = Query(default="all"),
                curves: Literal["all", "window"] = Query(default="all"),
                require_verified: bool = Query(default=False)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        if view != "chart" and curves == "window":
            raise HTTPException(status_code=422, detail="La ventana de curvas requiere view=chart")
        try:
            source = store.run(run_id) if require_verified else None
            if source is not None:
                checked = verify_backtest_run(store, source)
                if not checked["verified"]:
                    raise HTTPException(status_code=409, detail=checked["reason"])
            saved = store.run_chart_view(run_id, include_markers=markers != "window") if view == "chart" else store.run(run_id)
            if source is not None and (saved.get("result_hash") != source.get("result_hash")
                                       or saved.get("dataset_hash") != source.get("dataset_hash")
                                       or saved.get("version_id") != source.get("version_id")):
                raise HTTPException(status_code=409, detail="El backtest cambió durante la lectura")
            target = None
            if source is not None:
                # Reuse the verification already performed above. Reread only
                # its input identity; a handoff does not need a second replay.
                try:
                    dataset, _ = store.dataset(source["dataset_id"])
                    target = paper_target(source, dataset, store.version(source["version_id"]))
                except LabError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            risk = equity_risk(saved["equity"])
            displayed = chart_response(saved, risk, curves_windowed=curves == "window") if view == "chart" else {**saved, "risk": risk}
            return {**displayed, "verified": True, "paper_target": target} if source is not None else displayed
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/curves", dependencies=[Depends(local_only)])
    def get_run_curves(run_id: str, response: Response,
                       from_t: int = Query(ge=0, le=253402300799),
                       to_t: int = Query(ge=0, le=253402300799),
                       after_index: int = Query(default=-1, ge=-1),
                       limit: int = Query(default=2000, ge=1, le=MAX_DATASET_CANDLES),
                       view: Literal["exact", "plot"] = Query(default="exact")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        _validate_chart_window(from_t, to_t, limit, view)
        epoch = lab_access.epoch(store.path) if view == "plot" else None
        try:
            page = store.run_chart_curves(run_id, from_t, to_t, after_index, limit)
            if view == "plot":
                page["equity"] = _plot_points(page["equity"], "equity")
                page["drawdown"] = _plot_points(page["drawdown"], "drawdown_pct")
                with lab_access.guard(store.path, epoch):
                    return page
            return page
        except LabError as exc:
            status = 404 if str(exc) == "Backtest no encontrado" else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/markers", dependencies=[Depends(local_only)])
    def get_run_markers(run_id: str, response: Response,
                        from_t: int = Query(ge=0, le=253402300799),
                        to_t: int = Query(ge=0, le=253402300799)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        if from_t > to_t:
            raise HTTPException(status_code=422, detail="El inicio debe preceder al final")
        try:
            return store.run_chart_markers(run_id, from_t, to_t)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/events", dependencies=[Depends(local_only)])
    def get_run_events(run_id: str, offset: int = Query(default=0, ge=0),
                       limit: int = Query(default=50, ge=1, le=100)) -> dict:
        try:
            return store.run_rows_page(run_id, "events", offset, limit)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/trades", dependencies=[Depends(local_only)])
    def get_run_trades(run_id: str, offset: int = Query(default=0, ge=0),
                       limit: int = Query(default=50, ge=1, le=100)) -> dict:
        try:
            return store.run_rows_page(run_id, "trades", offset, limit)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/verify", dependencies=[Depends(local_only)])
    def verify_run(run_id: str) -> dict:
        """Comprueba entradas, salida persistida y repetición sin modificar el run."""
        try:
            saved = store.run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        checked = verify_backtest_run(store, saved)
        if not checked["verified"]:
            return checked
        try:
            dataset, raw = store.dataset(saved["dataset_id"])
            version = store.version(saved["version_id"])
            target = paper_target(saved, dataset, version)
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps:
                return {**checked, "verified": False, "status": "mismatch",
                        "reason": "El histórico contiene huecos durante el diagnóstico de cobertura"}
            coverage = higher_timeframe_coverage(bars, version["definition"], dataset["timeframe"])
        except LabError as exc:
            return {**checked, "verified": False, "status": "mismatch", "reason": str(exc)}
        return {**checked, "coverage": coverage, "paper_target": target}

    @router.get("/runs/{run_id}/sequence-risk", dependencies=[Depends(local_only)])
    def run_sequence_risk(run_id: str, response: Response,
                          paths: int = Query(default=250, ge=10, le=2000),
                          seed: int | None = Query(default=None, ge=0, le=(1 << 64) - 1)) -> dict:
        """Sensibilidad del orden de cierres de un run reproducible; no pronóstico."""
        response.headers["Cache-Control"] = "no-store"
        try:
            saved = store.run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        checked = verify_backtest_run(store, saved)
        if not checked["verified"]:
            raise HTTPException(status_code=409, detail="El backtest no es reproducible; no se calcula sensibilidad")
        try:
            version = store.version(saved["version_id"])
            if content_hash(version["definition"]) != saved["strategy_hash"]:
                raise LabError("La versión LAB cambió durante el cálculo")
            chosen_seed = seed if seed is not None else int(saved["result_hash"][:16], 16)
            result = sequence_risk(saved["trades"],
                                   initial_capital=version["definition"]["initial_capital"],
                                   paths=paths, seed=chosen_seed)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"environment": "LAB", "run_id": run_id,
                "result_hash": saved["result_hash"], "dataset_hash": saved["dataset_hash"],
                "strategy_version_id": saved["version_id"], "verified": True, **result}

    @router.get("/runs/{run_id}/export", dependencies=[Depends(local_only)])
    def export_run(run_id: str, response: Response,
                   schema_version: Literal["v1", "v2", "v3"] = "v2") -> dict:
        """Exporta sólo evidencia LAB verificada, separando señales y futuro.

        Es un paquete local para investigación posterior, no un conjunto de
        entrenamiento depurado ni un permiso para operar con dinero real.
        """
        try:
            saved = store.run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        check = verify_backtest_run(store, saved)
        if not check["verified"]:
            raise HTTPException(status_code=409, detail="La prueba no supera la verificación; no se exporta")
        response.headers["Cache-Control"] = "no-store"
        try:
            dataset, candles = store.dataset(saved["dataset_id"])
            version = store.version(saved["version_id"])
        except LabError as exc:
            raise HTTPException(status_code=409, detail="El histórico o la estrategia no están disponibles") from exc
        if (dataset["hash"] != saved["dataset_hash"]
                or dataset["source"] != saved["dataset_source"]
                or content_hash(version["definition"]) != saved["strategy_hash"]):
            raise HTTPException(status_code=409, detail="Las entradas cambiaron después de verificar; no se exporta")
        signals = []
        last_candle_t = candles[-1]["t"]
        for event in saved["events"]:
            full_week = event["signal_time"] + 604800 <= last_candle_t
            signals.append({
                "features_at_close": {
                    "signal_time": event["signal_time"],
                    "candle": {key: event[key] for key in ("open", "high", "low", "close", "volume")},
                    "conditions": event["explanation"],
                },
                "future_labels": {
                    "outcomes_pct": event["outcomes_pct"],
                    "mfe_pct": event["mfe_pct"] if full_week else None,
                    "mae_pct": event["mae_pct"] if full_week else None,
                    "max_at": event["max_at"] if full_week else None,
                    "min_at": event["min_at"] if full_week else None,
                    "favorable_at": event["favorable_at"] if full_week else None,
                    "adverse_at": event["adverse_at"] if full_week else None,
                    "time_to_max_seconds": _time_to_extreme(event, "max_at", full_week),
                    "time_to_min_seconds": _time_to_extreme(event, "min_at", full_week),
                    "time_to_favorable_seconds": _time_to_extreme(event, "favorable_at", full_week),
                    "time_to_adverse_seconds": _time_to_extreme(event, "adverse_at", full_week),
                    "full_7d_followup": full_week,
                    "followup_end_time": min(event["signal_time"] + 604800, last_candle_t),
                },
                "simulation": {"action": event["action"], "entry_time": event["entry_time"]},
            })
            if schema_version == "v1":
                for field in ("time_to_max_seconds", "time_to_min_seconds",
                              "time_to_favorable_seconds", "time_to_adverse_seconds"):
                    signals[-1]["future_labels"].pop(field)
        try:
            split = partition_evidence(candles, signals, dataset["timeframe"]) if schema_version == "v3" else None
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="No se pudo dividir la evidencia verificada") from exc
        return {
            "schema": f"orbis.lab.evidence.{schema_version}", "environment": "LAB", "theoretical_only": True,
            "run": {"id": saved["id"], "created_at": saved["created_at"],
                    "engine_version": saved["engine_version"], "result_hash": saved["result_hash"],
                    "metrics": saved["metrics"], "assumptions": saved["assumptions"]},
            "dataset": {key: dataset[key] for key in
                        ("id", "symbol", "timeframe", "source", "start_t", "end_t", "hash", "count", "verified_public")},
            "strategy": {"id": version["strategy_id"], "version_id": version["id"],
                         "version": version["version"], "hash": saved["strategy_hash"],
                         "definition": version["definition"]},
            "candles": candles, "signals": signals, "trades": saved["trades"],
            "verification": check,
            **({"split": split} if split is not None else {}),
            "warnings": ["Sólo LAB: ninguna señal es una orden real",
                         "Las etiquetas sin horizonte completo son nulas, no resultados de 7 días",
                         "Los eventos pueden solaparse y no son muestras independientes",
                         "Separar desarrollo y validación cronológicamente antes de entrenar o comparar modelos"] +
                        (["La división 70/30 no es independiente si la estrategia o parámetros se eligieron mirando todo el histórico"]
                         if split is not None else []),
        }

    def ai_preview(evidence: dict, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        if contains_sensitive_ai_data(evidence):
            raise HTTPException(status_code=422, detail="La evidencia LAB contiene un identificador sensible; no se enviará a IA")
        return {"model": AI_MODEL, "evidence": evidence,
                "evidence_sha256": evidence_digest(evidence),
                "external_destination": "OpenAI API", "key_persisted": False,
                "reply_persisted": False,
                "archive_available": store.encrypted and lab_format(store.path) == "encrypted_or_invalid"}

    def ai_access_marker(*, archive: bool = False) -> tuple[str, object | None]:
        kind = lab_format(store.path)
        epoch = lab_access.epoch(store.path)
        if kind == "encrypted_or_invalid" and epoch is None:
            raise HTTPException(status_code=423, detail="LAB cifrado bloqueado; desbloquéalo en Ajustes")
        if archive and kind != "encrypted_or_invalid":
            raise HTTPException(status_code=409, detail="El archivo de IA requiere LAB cifrado")
        return kind, epoch

    @contextmanager
    def ai_session(marker: tuple[str, object | None]):
        kind, epoch = marker
        try:
            with lab_access.guard(store.path, epoch):
                if lab_format(store.path) != kind:
                    raise HTTPException(status_code=423, detail="La sesión LAB cambió durante la operación")
                yield
        except LabError:
            if lab_access.epoch(store.path) is not epoch:
                raise HTTPException(status_code=423, detail="La sesión LAB cambió durante la operación") from None
            raise

    @router.get("/ai-archive", dependencies=[Depends(local_only)])
    def list_ai_archive(response: Response, kind: Literal["research", "backtest"] | None = None,
                        limit: int = Query(25, ge=1, le=50), offset: int = Query(0, ge=0, le=1_000_000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with ai_session(ai_access_marker(archive=True)):
                return store.ai_archive_page(kind=kind, limit=limit, offset=offset)
        except LabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/ai-archive/export-jsonl", dependencies=[Depends(local_only)])
    def export_ai_corpus(body: AiCorpusExport, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        expected = {str(record.id): record.expected_hash for record in body.records}
        if len(expected) != len(body.records):
            raise HTTPException(status_code=422, detail="La selección contiene análisis duplicados")
        try:
            with ai_session(ai_access_marker(archive=True)):
                return build_ai_corpus(store.ai_archive_selection(expected))
        except LabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/ai-archive/{analysis_id}", dependencies=[Depends(local_only)])
    def read_ai_archive(analysis_id: UUID, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with ai_session(ai_access_marker(archive=True)):
                record = store.ai_archive_record(str(analysis_id))
                if record is None:
                    raise HTTPException(status_code=404, detail="Análisis de IA no encontrado")
                return record
        except LabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete("/ai-archive/{analysis_id}", dependencies=[Depends(local_only)])
    def remove_ai_archive(analysis_id: UUID, body: DeleteAiAnalysisRequest, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with ai_session(ai_access_marker(archive=True)):
                record = store.ai_archive_record(str(analysis_id))
                if record is None:
                    raise HTTPException(status_code=404, detail="Análisis de IA no encontrado")
                return delete_ai_analysis(record["kind"], record["run_id"], str(analysis_id), body)
        except LabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def ai_answer(evidence: dict, body: ResearchAiRequest, response: Response,
                  kind: str, run_id: str, marker: tuple[str, object | None]) -> dict:
        response.headers["Cache-Control"] = "no-store"
        if contains_sensitive_ai_text(body.question, question=True) or contains_sensitive_ai_data(evidence):
            raise HTTPException(status_code=422, detail="La consulta contiene una dirección completa o un identificador sensible; elimínalo antes de enviar")
        if evidence_digest(evidence) != body.evidence_sha256:
            raise HTTPException(status_code=409, detail="La evidencia cambió; revísala antes de enviar")
        if body.save_locally and marker[0] != "encrypted_or_invalid":
            raise HTTPException(status_code=409, detail="Para guardar análisis de IA, cifra y desbloquea LAB")
        with ai_session(marker):
            pass
        try:
            result = analyze_research(evidence, body.question.strip(), body.api_key.get_secret_value())
        except Exception:
            # Never echo provider errors: they may contain request details or credentials.
            raise HTTPException(status_code=502, detail="No se pudo completar el análisis externo; la clave no se guardó") from None
        result = {**result, "saved_locally": False}
        with ai_session(marker):
            if body.save_locally:
                try:
                    saved = store.save_ai_analysis(kind, run_id, evidence, body.question.strip(), result)
                    result["saved_locally"] = True
                    result["analysis_id"] = saved["id"]
                except Exception:
                    result["archive_warning"] = "La respuesta se obtuvo, pero no pudo guardarse localmente"
            return result

    @router.post("/runs/compare/ai-preview", dependencies=[Depends(local_only)])
    def preview_comparison_ai(body: RunComparisonCreate, response: Response) -> dict:
        marker = ai_access_marker()
        comparison, sources = verified_comparison(body)
        with ai_session(marker):
            return ai_preview(comparison_evidence(comparison, sources), response)

    @router.post("/runs/compare/ai-analyze", dependencies=[Depends(local_only)])
    def ask_comparison_ai(response: Response, payload: object = Body()) -> dict:
        try:
            body = ComparisonAiRequest.model_validate(payload)
        except ValidationError:
            raise HTTPException(status_code=422, detail="Confirma el envío y revisa los datos de la consulta") from None
        if len(body.question.strip()) < 3:
            raise HTTPException(status_code=422, detail="Escribe una pregunta para el análisis")
        marker = ai_access_marker()
        comparison, sources = verified_comparison(body)
        evidence = comparison_evidence(comparison, sources)
        return ai_answer(evidence, body, response, "backtest", body.left_id, marker)

    @router.get("/runs/{run_id}/ai-preview", dependencies=[Depends(local_only)])
    def preview_backtest_ai(run_id: str, response: Response) -> dict:
        evidence = backtest_evidence(export_run(run_id, response))
        return ai_preview(evidence, response)

    @router.post("/runs/{run_id}/ai-analyze", dependencies=[Depends(local_only)])
    def ask_backtest_ai(run_id: str, body: ResearchAiRequest, response: Response) -> dict:
        marker = ai_access_marker()
        evidence = backtest_evidence(export_run(run_id, response))
        return ai_answer(evidence, body, response, "backtest", run_id, marker)

    @router.get("/runs/{run_id}/ai-analyses", dependencies=[Depends(local_only)])
    def backtest_ai_analyses(run_id: str, response: Response) -> list[dict]:
        export_run(run_id, response)
        response.headers["Cache-Control"] = "no-store"
        return store.ai_analyses("backtest", run_id)

    @router.get("/runs/{run_id}/ai-archive", dependencies=[Depends(local_only)])
    def export_backtest_ai_archive(run_id: str, response: Response) -> dict:
        export_run(run_id, response)
        response.headers["Cache-Control"] = "no-store"
        return ai_archive("backtest", run_id)

    @router.delete("/runs/{run_id}/ai-analyses/{analysis_id}", dependencies=[Depends(local_only)])
    def delete_backtest_ai_analysis(run_id: str, analysis_id: str,
                                    body: DeleteAiAnalysisRequest, response: Response) -> dict:
        export_run(run_id, response)
        response.headers["Cache-Control"] = "no-store"
        return delete_ai_analysis("backtest", run_id, analysis_id, body)

    def verify_experiment(saved: dict, kind: Literal["validation", "optimization"]) -> dict:
        return verify_lab_experiment(store, saved, kind)

    @router.post("/validations", dependencies=[Depends(local_only)])
    def validate_holdout(body: ValidationCreate) -> dict:
        try:
            dataset, raw = store.dataset(body.dataset_id)
            if dataset["gaps"]:
                raise LabError("El histórico tiene huecos; no se valida una muestra incompleta")
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps:
                raise LabError("El histórico tiene huecos")
            version = store.version(body.version_id)
            result = chronological_holdout(bars, version["definition"], dataset["timeframe"],
                                           body.train_fraction, engine_version=ENGINE_VERSION)
            saved = store.save_validation(body.dataset_id, body.version_id, dataset["hash"],
                                          result, VALIDATION_ENGINE_VERSION,
                                          lab_engine_version=ENGINE_VERSION,
                                          expected_strategy_hash=content_hash(version["definition"]))
            return with_reserve_reuse("validation", {
                **saved, "risk": {"train": equity_risk(saved["train"]["equity"]),
                                  "out_of_sample": equity_risk(saved["out_of_sample"]["equity"])}})
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/rolling-validations", dependencies=[Depends(local_only)])
    def validate_rolling(body: RollingCreate) -> dict:
        try:
            dataset, raw = store.dataset(body.dataset_id)
            if dataset["gaps"]:
                raise LabError("El histórico tiene huecos; no se valida una muestra incompleta")
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps:
                raise LabError("El histórico tiene huecos")
            version = store.version(body.version_id)
            result = rolling_fixed_validation(bars, version["definition"],
                                              dataset["timeframe"], body.folds,
                                              engine_version=ENGINE_VERSION)
            saved = store.save_validation(body.dataset_id, body.version_id, dataset["hash"],
                                          result, ROLLING_ENGINE_VERSION,
                                          lab_engine_version=ENGINE_VERSION,
                                          expected_strategy_hash=content_hash(version["definition"]))
            return with_reserve_reuse("validation", saved)
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/validations", dependencies=[Depends(local_only)])
    def list_validations() -> list[dict]:
        return store.validations()

    @router.get("/validations/{validation_id}", dependencies=[Depends(local_only)])
    def get_validation(validation_id: str) -> dict:
        try:
            saved = store.validation(validation_id)
            if saved["method"] == "ROLLING_FIXED_VERSION":
                return with_reserve_reuse("validation", saved)
            return with_reserve_reuse("validation", {
                **saved, "risk": {"train": equity_risk(saved["train"]["equity"]),
                                  "out_of_sample": equity_risk(saved["out_of_sample"]["equity"])}})
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/validations/{validation_id}/verify", dependencies=[Depends(local_only)])
    def verify_validation(validation_id: str) -> dict:
        try:
            return verify_experiment(store.validation(validation_id), "validation")
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/validations/{validation_id}/reference", dependencies=[Depends(local_only)])
    def validation_reference(validation_id: str, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return strategy_reference(store, validation_id).exported()
        except ResearchReferenceError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/optimizations/walk-forward", dependencies=[Depends(local_only)])
    def optimize_walk_forward(body: WalkForwardOptimizationCreate) -> dict:
        try:
            dataset, raw = store.dataset(body.dataset_id)
            if dataset["gaps"]:
                raise LabError("El histórico tiene huecos; no se exploran parámetros sobre datos incompletos")
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps:
                raise LabError("El histórico tiene huecos")
            version = store.version(body.version_id)
            result = walk_forward_stops(bars, version["definition"], dataset["timeframe"],
                                        body.stop_losses, body.take_profits, body.folds,
                                        engine_version=ENGINE_VERSION,
                                        final_holdout_fraction=body.final_holdout_fraction)
            saved = store.save_optimization(body.dataset_id, body.version_id, dataset["hash"], result,
                                            ENGINE_VERSION,
                                            expected_strategy_hash=content_hash(version["definition"]))
            return with_reserve_reuse("optimization", saved)
        except LabError as exc:
            raise fail(exc) from exc

    @router.post("/optimizations", dependencies=[Depends(local_only)])
    def optimize(body: OptimizationCreate) -> dict:
        try:
            dataset, raw = store.dataset(body.dataset_id)
            if dataset["gaps"]:
                raise LabError("El histórico tiene huecos; no se exploran parámetros sobre datos incompletos")
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps:
                raise LabError("El histórico tiene huecos")
            version = store.version(body.version_id)
            result = optimize_stops(bars, version["definition"], dataset["timeframe"],
                                    body.stop_losses, body.take_profits,
                                    body.train_fraction,
                                    engine_version=ENGINE_VERSION)
            saved = store.save_optimization(body.dataset_id, body.version_id, dataset["hash"], result,
                                            ENGINE_VERSION,
                                            expected_strategy_hash=content_hash(version["definition"]))
            return with_reserve_reuse("optimization", saved)
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/optimizations", dependencies=[Depends(local_only)])
    def list_optimizations() -> list[dict]:
        return store.optimizations()

    @router.get("/optimizations/{optimization_id}", dependencies=[Depends(local_only)])
    def get_optimization(optimization_id: str) -> dict:
        try:
            return with_reserve_reuse("optimization", store.optimization(optimization_id))
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/optimizations/{optimization_id}/verify", dependencies=[Depends(local_only)])
    def verify_optimization(optimization_id: str) -> dict:
        try:
            return verify_experiment(store.optimization(optimization_id), "optimization")
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/research/runs", dependencies=[Depends(local_only)])
    def research(body: ResearchRunCreate) -> dict:
        try:
            dataset, raw = store.dataset(body.dataset_id)
            rule = validate_research_rule(body.rule)
            bars, gaps = validate_candles(raw, dataset["timeframe"], now=int(datetime.now(timezone.utc).timestamp()))
            if gaps != dataset["gaps"]:
                raise LabError("La cobertura guardada no coincide con las velas del dataset")
            result = run_research(bars, rule, dataset["timeframe"])
            return store.save_research_run(dataset["id"], dataset["hash"], result, RESEARCH_ENGINE_VERSION,
                                           lab_engine_version=ENGINE_VERSION,
                                           expected_rule_hash=content_hash(rule))
        except LabError as exc:
            raise fail(exc) from exc

    @router.get("/research/runs", dependencies=[Depends(local_only)])
    def list_research_runs() -> list[dict]:
        return store.research_runs()

    @router.get("/research/runs/{run_id}", dependencies=[Depends(local_only)])
    def get_research_run(run_id: str) -> dict:
        try:
            return store.research_run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/research/runs/{run_id}/verify", dependencies=[Depends(local_only)])
    def verify_research_run(run_id: str) -> dict:
        """Repite la investigación exacta sin modificar el archivo de casos."""
        try:
            saved = store.research_run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        checks: dict[str, bool | None] = {"dataset": None, "rule": None,
                                          "stored_result": None, "replay": None}

        def report(status: str, reason: str) -> dict:
            return {"run_id": run_id, "verified": status == "verified",
                    "status": status, "reason": reason, "checks": checks}

        try:
            dataset, raw = store.dataset(saved["dataset_id"])
        except LabError as exc:
            return report("mismatch", str(exc))
        checks["dataset"] = dataset["hash"] == saved["dataset_hash"]
        if not checks["dataset"]:
            return report("mismatch", "El dataset guardado no coincide con la investigación")
        if saved.get("rule_hash") is not None:
            checks["rule"] = content_hash(saved["rule"]) == saved["rule_hash"]
            if not checks["rule"]:
                return report("mismatch", "La regla guardada fue alterada")
        metadata = {"id", "dataset_id", "dataset_hash", "created_at", "engine_version",
                    "lab_engine_version", "rule_hash", "result_hash"}
        stored_result = {key: value for key, value in saved.items() if key not in metadata}
        if saved.get("result_hash") is not None:
            checks["stored_result"] = content_hash(stored_result) == saved["result_hash"]
            if not checks["stored_result"]:
                return report("mismatch", "Los casos o estadísticas guardados fueron alterados")
        if saved["engine_version"] not in (RESEARCH_ENGINE_VERSION, SEGMENTED_RESEARCH_ENGINE_VERSION,
                                           LEGACY_RESEARCH_ENGINE_VERSION):
            return report("engine_unavailable", "El motor original de investigación no está disponible")
        if saved.get("lab_engine_version") is None:
            return report("legacy_unpinned", "El registro antiguo no fijó el evaluador de reglas")
        if not supports_engine_version(saved["lab_engine_version"], {"entry": saved["rule"]}):
            return report("engine_unavailable", "El evaluador original de reglas no está disponible")
        try:
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps != dataset["gaps"]:
                return report("mismatch", "La cobertura guardada no coincide con las velas del dataset")
            replay = run_research(bars, validate_research_rule(saved["rule"]), dataset["timeframe"],
                                  engine_version=saved["engine_version"])
        except (KeyError, TypeError, ValueError, LabError) as exc:
            return report("mismatch", f"No se pudo repetir la investigación: {exc}")
        checks["replay"] = content_hash(replay) == content_hash(stored_result)
        if not checks["replay"]:
            return report("mismatch", "La repetición difiere de los casos guardados")
        if checks["rule"] is None or checks["stored_result"] is None:
            return report("legacy_unpinned", "Coincide, pero el registro antiguo no fijó todas sus huellas")
        return report("verified", "Dataset, regla y casos coinciden con la repetición")

    @router.get("/research/runs/{run_id}/volatility-regimes", dependencies=[Depends(local_only)])
    def research_volatility_regimes(run_id: str, response: Response,
                                    lookback: int = Query(default=20, ge=2, le=300),
                                    threshold_pct: str = Query(default="2", max_length=32),
                                    train_fraction: str = Query(default="0.7", max_length=16)) -> dict:
        """Segmentación posterior de casos verificados; no cambia la regla LAB."""
        evidence = export_research_run(run_id, response)
        try:
            result = analyze_volatility_regimes(
                evidence["candles"], evidence["cases"], evidence["dataset"]["timeframe"],
                lookback=lookback, threshold_pct=threshold_pct,
                train_fraction=train_fraction,
            )
        except LabError as exc:
            raise fail(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"environment": "LAB", "run_id": run_id,
                "dataset_id": evidence["dataset"]["id"],
                "dataset_hash": evidence["dataset"]["hash"],
                "verification": evidence["verification"], **result}

    @router.get("/research/runs/{run_id}/export", dependencies=[Depends(local_only)])
    def export_research_run(run_id: str, response: Response,
                            schema_version: Literal["v1", "v2", "v3"] = "v2") -> dict:
        """Evidencia LAB local; condiciones causales separadas del futuro."""
        try:
            saved = store.research_run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        check = verify_research_run(run_id)
        if not check["verified"]:
            raise HTTPException(status_code=409, detail="La investigación no supera la verificación; no se exporta")
        try:
            dataset, candles = store.dataset(saved["dataset_id"])
            current = store.research_run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=409, detail="La investigación cambió después de verificar") from exc
        if (content_hash(current) != content_hash(saved)
                or dataset["hash"] != saved["dataset_hash"]
                or content_hash(saved["rule"]) != saved["rule_hash"]):
            raise HTTPException(status_code=409, detail="La investigación cambió después de verificar")
        by_time = {candle["t"]: candle for candle in candles}
        if any(event["signal_time"] not in by_time for event in saved["events"]):
            raise HTTPException(status_code=409, detail="Los casos ya no corresponden al histórico")
        cases = []
        for event in saved["events"]:
            full_week = event["full_7d_followup"]
            cases.append({
                "features_at_close": {
                    "signal_time": event["signal_time"],
                    "candle": by_time[event["signal_time"]],
                    "conditions": event["explanation"],
                },
                "future_labels": {
                    "outcomes_pct": event["outcomes_pct"],
                    "max_up_pct": event["max_up_pct"] if full_week else None,
                    "max_down_pct": event["max_down_pct"] if full_week else None,
                    "max_at": event["max_at"] if full_week else None,
                    "min_at": event["min_at"] if full_week else None,
                    "time_to_max_seconds": _time_to_extreme(event, "max_at", full_week),
                    "time_to_min_seconds": _time_to_extreme(event, "min_at", full_week),
                    "followup_end_time": event["followup_end_time"],
                    "full_7d_followup": full_week,
                },
            })
            if schema_version == "v1":
                cases[-1]["future_labels"].pop("time_to_max_seconds")
                cases[-1]["future_labels"].pop("time_to_min_seconds")
        try:
            split = partition_evidence(candles, cases, dataset["timeframe"]) if schema_version == "v3" else None
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="No se pudo dividir la evidencia verificada") from exc
        response.headers["Cache-Control"] = "no-store"
        return {
            "schema": f"orbis.lab.research.evidence.{schema_version}",
            "environment": "LAB", "theoretical_only": True,
            "run": {key: saved[key] for key in
                    ("id", "created_at", "engine_version", "lab_engine_version",
                     "result_hash", "summary", "assumptions")},
            "dataset": {key: dataset[key] for key in
                        ("id", "symbol", "timeframe", "source", "start_t", "end_t",
                         "hash", "count", "verified_public")},
            "rule": {"hash": saved["rule_hash"], "definition": saved["rule"]},
            "candles": candles, "cases": cases, "verification": check,
            **({"split": split} if split is not None else {}),
            "warnings": ["Sólo LAB: no hay órdenes ni rentabilidad ejecutable",
                         "Las etiquetas futuras no son condiciones de entrada",
                         "Los casos pueden solaparse y no son muestras independientes",
                         "Separar desarrollo y validación cronológicamente antes de entrenar un modelo"] +
                        (["La división 70/30 no es independiente si la regla se eligió mirando todo el histórico"]
                         if split is not None else []),
        }

    @router.post("/research/export-cohort", dependencies=[Depends(local_only)])
    def export_research_cohort(body: ResearchCohortExport, response: Response) -> dict:
        """Read-only export of verified LAB evidence on one UTC split."""
        if len(set(body.run_ids)) != len(body.run_ids):
            raise HTTPException(status_code=422, detail="Selecciona consultas distintas")
        try:
            result = build_research_cohort(
                (export_research_run(run_id, response) for run_id in sorted(body.run_ids)),
                boundary=body.boundary_open_t,
            )
        except LabError as exc:
            raise fail(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/research/runs/{run_id}/ai-preview", dependencies=[Depends(local_only)])
    def preview_research_ai(run_id: str, response: Response) -> dict:
        """Exact public LAB aggregate that would be sent after explicit confirmation."""
        evidence = research_evidence(export_research_run(run_id, response))
        return ai_preview(evidence, response)

    @router.post("/research/runs/{run_id}/ai-analyze", dependencies=[Depends(local_only)])
    def ask_research_ai(run_id: str, body: ResearchAiRequest, response: Response) -> dict:
        marker = ai_access_marker()
        evidence = research_evidence(export_research_run(run_id, response))
        return ai_answer(evidence, body, response, "research", run_id, marker)

    @router.get("/research/runs/{run_id}/ai-analyses", dependencies=[Depends(local_only)])
    def research_ai_analyses(run_id: str, response: Response) -> list[dict]:
        export_research_run(run_id, response)
        response.headers["Cache-Control"] = "no-store"
        return store.ai_analyses("research", run_id)

    @router.get("/research/runs/{run_id}/ai-archive", dependencies=[Depends(local_only)])
    def export_research_ai_archive(run_id: str, response: Response) -> dict:
        export_research_run(run_id, response)
        response.headers["Cache-Control"] = "no-store"
        return ai_archive("research", run_id)

    @router.delete("/research/runs/{run_id}/ai-analyses/{analysis_id}", dependencies=[Depends(local_only)])
    def delete_research_ai_analysis(run_id: str, analysis_id: str,
                                    body: DeleteAiAnalysisRequest, response: Response) -> dict:
        export_research_run(run_id, response)
        response.headers["Cache-Control"] = "no-store"
        return delete_ai_analysis("research", run_id, analysis_id, body)

    def ai_archive(kind: str, run_id: str) -> dict:
        return {"schema": "orbis.lab.ai-archive.v1", "environment": "LAB",
                "theoretical_only": True, "kind": kind, "run_id": run_id,
                "records": store.ai_analyses(kind, run_id)}

    def delete_ai_analysis(kind: str, run_id: str, analysis_id: str,
                           body: DeleteAiAnalysisRequest) -> dict:
        try:
            deleted = store.delete_ai_analysis(kind, run_id, analysis_id, body.expected_hash)
        except LabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Análisis de IA no encontrado")
        return {"deleted": True, "analysis_id": analysis_id}

    return router
