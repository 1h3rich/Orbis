"""API local de simulación DCA; no dispone de rutas para órdenes reales."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from app.security.local_session import LocalStoreUnlock
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.automation.dca import dca_engine_for_rule, simulate_dca, validate_dca
from app.automation.dca_bundle import DcaBundleReview, MAX_DCA_BUNDLE_BYTES, export_dca_bundle, review_dca_bundle
from app.automation.store import AutomationStore
from app.automation.access import automation_access, automation_format
from app.automation.verification import verify_dca_run, verify_dca_validation
from app.automation.validation import VALIDATION_ENGINE_VERSION, chronological_dca_holdout
from app.lab.engine import LabError, validate_candles
from app.lab.store import LabStore, content_hash
from app.research.references import ResearchReferenceError, dca_reference


LOCAL_HOSTS = frozenset(("127.0.0.1", "::1", "localhost", "testserver", "testclient"))


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    definition: dict[str, Any]


class VersionCreate(BaseModel):
    definition: dict[str, Any]


class RunCreate(BaseModel):
    dataset_id: str
    version_id: str


class AutomationUnlock(LocalStoreUnlock):
    pass


class ValidationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)
    train_fraction: str = Field(default="0.7", min_length=1, max_length=32)


async def review_import(request: Request) -> DcaBundleReview:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_DCA_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail="El archivo DCA supera 32 KiB")
        raw.extend(chunk)
    try:
        return review_dca_bundle(bytes(raw))
    except LabError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_automation_router(path: Path, lab_path: Path) -> APIRouter:
    store, lab = AutomationStore(path, allow_create=False), LabStore(lab_path, allow_create=False)
    router = APIRouter(prefix="/automation/dca", tags=["dca-simulation"])

    def local_only(request: Request) -> None:
        client_host = request.client.host if request.client else None
        if client_host not in LOCAL_HOSTS or request.url.hostname not in LOCAL_HOSTS:
            raise HTTPException(status_code=403, detail="Automatizaciones sólo locales")
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Origen externo rechazado")

    def available(request: Request) -> None:
        local_only(request)
        try:
            encrypted = automation_format(store.path) == "encrypted_or_invalid"
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if encrypted and automation_access.get(store.path) is None:
            raise HTTPException(status_code=423, detail="DCA cifrado bloqueado; desbloquéalo en Ajustes")

    def require_configured() -> None:
        if automation_format(store.path) in ("missing", "empty"):
            raise HTTPException(status_code=423, detail="Crea primero la base DCA cifrada en Ajustes")

    @router.get("/status", dependencies=[Depends(local_only)])
    def automation_status(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return automation_access.status(store.path)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/unlock", dependencies=[Depends(local_only)])
    def unlock_automation(body: AutomationUnlock, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return automation_access.unlock(store.path, body.passphrase.get_secret_value(),
                                          session_seconds=body.session_seconds)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/create", dependencies=[Depends(local_only)])
    def create_encrypted_automation(body: AutomationUnlock, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return automation_access.create(store.path, body.passphrase.get_secret_value(),
                                          session_seconds=body.session_seconds)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/lock", dependencies=[Depends(local_only)])
    def lock_automation(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return automation_access.lock(store.path)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/rules", dependencies=[Depends(available)])
    def create_rule(body: RuleCreate) -> dict:
        try:
            require_configured()
            return store.create_rule(body.name, validate_dca(body.definition))
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/rules", dependencies=[Depends(available)])
    def list_rules() -> list[dict]:
        return store.rules()

    @router.post("/rules/{rule_id}/versions", dependencies=[Depends(available)])
    def add_version(rule_id: str, body: VersionCreate) -> dict:
        try:
            return store.add_version(rule_id, validate_dca(body.definition))
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/rules/{rule_id}/versions", dependencies=[Depends(available)])
    def list_versions(rule_id: str) -> list[dict]:
        return store.versions(rule_id)

    @router.get("/rules/{rule_id}/versions/{version_id}/export", dependencies=[Depends(available)])
    def export_rule(rule_id: UUID, version_id: UUID, response: Response) -> dict:
        try:
            source = store.shareable_version(str(rule_id), str(version_id))
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            bundle = export_dca_bundle(source)
        except LabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return bundle

    @router.post("/rules/import/preview", dependencies=[Depends(local_only)])
    async def preview_rule(request: Request, response: Response) -> dict:
        review = await review_import(request)
        response.headers["Cache-Control"] = "no-store"
        return review

    @router.post("/rules/import", dependencies=[Depends(available)])
    async def import_rule(request: Request, response: Response,
                          review_hash: str = Header(alias="X-Orbis-Import-Hash", pattern=r"^[0-9a-f]{64}$"),
                          import_id: UUID = Header(alias="X-Orbis-Import-Id")) -> dict:
        review = await review_import(request)
        if review["review_hash"] != review_hash:
            raise HTTPException(status_code=409, detail="El archivo cambió desde la revisión")

        def save_copy():
            require_configured()
            return store.create_rule(review["name"], review["definition"], import_id=import_id)

        try:
            saved = await run_in_threadpool(save_copy)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return saved

    @router.post("/runs", dependencies=[Depends(available)])
    def execute(body: RunCreate) -> dict:
        try:
            dataset, raw = lab.dataset(body.dataset_id)
            if dataset["gaps"]:
                raise LabError("El histórico tiene huecos; DCA no se simula con cobertura incompleta")
            bars, gaps = validate_candles(raw, dataset["timeframe"], now=int(datetime.now(timezone.utc).timestamp()))
            if gaps:
                raise LabError("El histórico tiene huecos")
            version = store.version(body.version_id)
            rule = version["definition"]
            engine = dca_engine_for_rule(rule)
            result = simulate_dca(bars, rule, timeframe=dataset["timeframe"], engine_version=engine)
            return store.save_run(body.version_id, body.dataset_id, dataset["hash"], result, engine,
                                  lab_path=lab_path,
                                  lab_passphrase=lab.current_passphrase(),
                                  expected_definition_hash=content_hash(version["definition"]))
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/runs", dependencies=[Depends(available)])
    def list_runs() -> list[dict]:
        return store.runs()

    @router.get("/runs/{run_id}", dependencies=[Depends(available)])
    def get_run(run_id: str) -> dict:
        try:
            return store.run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/verify", dependencies=[Depends(available)])
    def verify_run(run_id: str) -> dict:
        """Repite las tres variantes DCA con el dataset y la regla fijados."""
        try:
            saved = store.run(run_id)
        except LabError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return verify_dca_run(store, lab, saved)

    @router.post("/validations", dependencies=[Depends(available)])
    def validate_reserve(body: ValidationCreate, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            dataset, raw = lab.dataset(body.dataset_id)
            bars, gaps = validate_candles(raw, dataset["timeframe"],
                                          now=int(datetime.now(timezone.utc).timestamp()))
            if gaps or dataset["gaps"]:
                raise LabError("La reserva DCA requiere un histórico sin huecos")
            version = store.version(body.version_id)
            result = chronological_dca_holdout(bars, version["definition"], dataset["timeframe"],
                                               body.train_fraction)
            return store.save_validation(body.version_id, body.dataset_id, dataset["hash"],
                result, VALIDATION_ENGINE_VERSION, lab_path=lab_path,
                lab_passphrase=lab.current_passphrase(),
                expected_definition_hash=content_hash(version["definition"]))
        except LabError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/validations", dependencies=[Depends(available)])
    def list_validations(response: Response) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.validations()
        except LabError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/validations/{validation_id}", dependencies=[Depends(available)])
    def get_validation(validation_id: str, response: Response,
                       require_verified: bool = False,
                       view: Literal["full", "summary"] = Query(default="full")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            saved = store.validation(validation_id)
        except LabError as exc:
            raise HTTPException(404 if str(exc) == "Validación DCA no encontrada" else 409, str(exc)) from exc
        checked = verify_dca_validation(store, lab, saved) if require_verified else None
        if checked is not None and not checked["verified"]:
            raise HTTPException(409, checked["reason"])
        if view == "summary":
            try:
                saved = {**saved, **{part: {"results": {mode: {"metrics": result["metrics"]}
                    for mode, result in saved[part]["results"].items()}} for part in ("train", "out_of_sample")}}
            except (KeyError, TypeError):
                raise HTTPException(409, "Resultado de validación DCA incompatible") from None
        return {**saved, "verification": checked} if checked is not None else saved

    @router.get("/validations/{validation_id}/verify", dependencies=[Depends(available)])
    def verify_validation(validation_id: str, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return verify_dca_validation(store, lab, store.validation(validation_id))
        except LabError as exc:
            raise HTTPException(404 if str(exc) == "Validación DCA no encontrada" else 409, str(exc)) from exc

    @router.get("/validations/{validation_id}/reference", dependencies=[Depends(available)])
    def validation_reference(validation_id: str, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return dca_reference(store, lab, validation_id).exported()
        except ResearchReferenceError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
