"""Local research routes behind the LAB access and origin checks."""
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.lab.perp_optimization import PerpOptimizationPlan
from app.lab.perp_optimization_store import PerpOptimizations
from app.lab.perp_validation_store import PerpValidations
from app.lab.store import LabStore


class OptimizationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    dataset_id: str = Field(min_length=36, max_length=36)
    profile_id: str = Field(min_length=36, max_length=36)
    plan: PerpOptimizationPlan


class OptimizationIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


def create_perp_optimization_router(store: LabStore) -> APIRouter:
    router = APIRouter(prefix='/optimizations')
    archive = PerpOptimizations(store)
    validations = PerpValidations(store)

    @router.post('', status_code=201)
    def create(body: OptimizationCreate, response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        try:
            return archive.create(body.dataset_id, body.profile_id, body.plan)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('')
    def page(response: Response, offset: int = Query(0, ge=0, le=1_000_000_000),
             limit: int = Query(10, ge=1, le=25)) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        try:
            return archive.page(offset, limit)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/{identifier}')
    def read(identifier: str, response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        try:
            return archive.read(identifier)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/{identifier}/export')
    def export(identifier: str, response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        try:
            bundle = archive.read(identifier, include_originals=True)
            response.headers['Content-Disposition'] = f'attachment; filename="orbis-perp-optimization-{identifier}.json"'
            return bundle
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post('/{identifier}/variant', status_code=201)
    def materialize(identifier: str, body: OptimizationIdentity, response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        try:
            return archive.materialize(identifier, body.expected_hash)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post('/{identifier}/paper-validation', status_code=201)
    def paper_validation(identifier: str, body: OptimizationIdentity, response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        try:
            return validations.create_from_optimization(identifier, body.expected_hash)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
