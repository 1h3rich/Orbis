"""Local research endpoints, inheriting the LAB router's origin/access boundary."""
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.lab.perp_validation import PerpValidationPlan
from app.lab.perp_validation_store import PerpValidations
from app.lab.store import LabStore


class PerpValidationCreate(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    dataset_id: str=Field(min_length=36,max_length=36)
    profile_id: str=Field(min_length=36,max_length=36)
    plan: PerpValidationPlan


def create_perp_validation_router(store: LabStore) -> APIRouter:
    router=APIRouter(prefix='/validations')
    archive=PerpValidations(store)

    @router.post('',status_code=201)
    def create(body: PerpValidationCreate,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return archive.create(body.dataset_id,body.profile_id,body.plan)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc

    @router.get('')
    def page(response: Response,offset: int=Query(0,ge=0,le=1_000_000_000),limit: int=Query(25,ge=1,le=100)) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return archive.page(offset=offset,limit=limit)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc

    @router.get('/{identifier}')
    def read(identifier: str,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return archive.read(identifier)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc

    @router.get('/{identifier}/export')
    def export(identifier: str,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:
            bundle=archive.read(identifier)
            response.headers['Content-Disposition']='attachment; filename="orbis-perp-validation-'+bundle['validation']['id']+'.json"'
            return bundle
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc

    @router.get('/{identifier}/reference')
    def reference(identifier: str,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return archive.reference(identifier)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc

    @router.post('/{identifier}/windows/{index}/inspect',status_code=201)
    def inspect(identifier: str,index: int,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return archive.inspect_window(identifier,index)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc

    return router
