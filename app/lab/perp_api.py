"""Local encrypted derivative experiments. No PAPER/live authority routes."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter,HTTPException,Query,Response
from pydantic import BaseModel,ConfigDict,Field

from app.lab.engine import LabError,validate_strategy
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_profile import LimitEntry, PartialTarget, PathKind, PerpProfile, read_profile
from app.lab.store import LabStore,content_hash


class ProfileCreate(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    version_id: str=Field(min_length=36,max_length=36)
    margin_mode: Literal['CROSS','ISOLATED']
    leverage: str=Field(min_length=1,max_length=80)
    trade_path: PathKind
    mark_path: PathKind
    entry_order: LimitEntry | None = None
    partial_targets: list[PartialTarget] = Field(default_factory=list,max_length=5)


class PerpRunCreate(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    dataset_id: str=Field(min_length=36,max_length=36)
    profile_id: str=Field(min_length=36,max_length=36)
    entry_start_index: int=Field(default=0,ge=0,le=4999)
    end_index: int|None=Field(default=None,ge=1,le=5000)


class CompareRules(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    observation_hash: str=Field(pattern=r'^[0-9a-f]{64}$')


class CompareRuns(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    base_id: str=Field(min_length=36,max_length=36)
    base_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    other_id: str=Field(min_length=36,max_length=36)
    other_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    expected_hash: str | None=Field(default=None,pattern=r'^[0-9a-f]{64}$')


class CompareCosts(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    run_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    fee_rate: str=Field(min_length=1,max_length=80)
    slippage_rate: str=Field(min_length=1,max_length=80)
    expected_hash: str | None=Field(default=None,pattern=r'^[0-9a-f]{64}$')


class CompareMargin(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    run_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    leverages: list[Annotated[str,Field(min_length=1,max_length=80)]]=Field(min_length=1,max_length=3)
    margin_modes: list[Literal['CROSS','ISOLATED']]=Field(min_length=1,max_length=2)
    expected_hash: str | None=Field(default=None,pattern=r'^[0-9a-f]{64}$')


def create_perp_experiment_router(store: LabStore) -> APIRouter:
    router=APIRouter(prefix='/perp')
    experiments=PerpExperiments(store)
    from app.lab.perp_validation_api import create_perp_validation_router
    router.include_router(create_perp_validation_router(store))
    from app.lab.perp_optimization_api import create_perp_optimization_router
    router.include_router(create_perp_optimization_router(store))

    def fail(exc: Exception) -> HTTPException:
        return HTTPException(422,str(exc))

    @router.get('/profiles')
    def profiles(response: Response) -> list[dict]:
        response.headers['Cache-Control']='no-store'
        try:return experiments.profiles()
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.post('/profiles',status_code=201)
    def profile(body: ProfileCreate,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:
            definition=validate_strategy(store.version(body.version_id)['definition'])
            configured=PerpProfile(schema_name='orbis.lab.perp.profile.v1',
                strategy_version_id=body.version_id,strategy_hash=content_hash(definition),
                signal_engine='lab-1.16',account_engine='perp-account-0.1',mode='EXPERIMENTAL_CURRENT_RULES',
                margin_mode=body.margin_mode,leverage=body.leverage,trade_path=body.trade_path,mark_path=body.mark_path,
                boundary_order='PREVIOUS_CLOSE_THEN_FUNDING_THEN_OPEN',execution='MARKET_WITH_ADVERSE_SLIPPAGE',
                sizing='CASH_ALLOCATION_INCLUDING_MARGIN_LOSS_BUFFER_AND_FEES',simultaneous_positions=1,
                liquidation_action='STOP_NEW_ENTRIES',end_action='CLOSE_AT_LAST_MODELED_TRADE')
            payload=configured.payload()
            if body.entry_order is not None:
                payload.update({'schema':'orbis.lab.perp.profile.v2',
                    'execution':'LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE',
                    'end_action':'CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE',
                    'entry_order':body.entry_order.model_dump()})
            if body.partial_targets:
                payload.update(schema='orbis.lab.perp.profile.v3',
                    partial_targets=[row.model_dump() for row in body.partial_targets])
            return experiments.save_profile(read_profile(payload))
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.get('/runs')
    def runs(response: Response,offset: int=Query(0,ge=0,le=1_000_000_000),
             limit: int=Query(25,ge=1,le=100)) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.page(offset=offset,limit=limit)
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.post('/runs',status_code=201)
    def run(body: PerpRunCreate,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.run(**body.model_dump())
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.post('/runs/compare')
    def compare_runs(body: CompareRuns,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.compare_runs(**body.model_dump())
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.get('/runs/{identifier}')
    def read(identifier: str,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.read(identifier)
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.get('/runs/{identifier}/export')
    def export(identifier: str,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:
            bundle=experiments.read(identifier)
            response.headers['Content-Disposition']='attachment; filename="orbis-perp-run-'+bundle['run']['id']+'.json"'
            return bundle
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.post('/runs/{identifier}/compare-rules')
    def compare_rules(identifier: str,body: CompareRules,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.compare_rules(identifier,body.observation_hash)
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.post('/runs/{identifier}/compare-costs')
    def compare_costs(identifier: str,body: CompareCosts,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.compare_costs(identifier,**body.model_dump())
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.post('/runs/{identifier}/compare-margin')
    def compare_margin(identifier: str,body: CompareMargin,response: Response) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.compare_margin(identifier,**body.model_dump())
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    @router.get('/runs/{identifier}/rule-window')
    def rule_window(identifier: str,response: Response,run_hash: str=Query(...,pattern=r'^[0-9a-f]{64}$')) -> dict:
        response.headers['Cache-Control']='no-store'
        try:return experiments.rules_in_window(identifier,run_hash)
        except (ValueError,OSError) as exc:raise fail(exc) from exc

    return router
