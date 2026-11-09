"""Protected read-only DEX bot preparation; no activation or signing endpoint."""
from datetime import datetime,timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field

from app.live_execution.dex_bot_source import DexBotSources, SourceKind
from app.portfolio.models import RealAccount


class DexBotPreview(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    source_kind:Literal['STRATEGY','DCA']
    paper_session_id:UUID=Field(strict=False)
    research_validation_id:UUID|None=Field(default=None,strict=False)
    entry_usdt:str=Field(min_length=1,max_length=100)
    budget_usdt:str=Field(min_length=1,max_length=100)
    exposure_usdt:str=Field(min_length=1,max_length=100)
    loss_usdt:str=Field(min_length=1,max_length=100)
    wbnb_quota:str=Field(min_length=1,max_length=100)
    monthly_usdt:str|None=Field(default=None,min_length=1,max_length=100)


def create_dex_bot_router(access,*,authorized,authorized_passive,sources:DexBotSources) -> APIRouter:
    router=APIRouter(prefix='/accounts/{account_id}/dex-bots',tags=['dex-bot-preparation'])

    def call(account_id,token,response,action,*,passive=False):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                access.require(token,touch=not passive)
                generation=access.vault.session_generation
                with access.vault.session() as db:
                    if db.get(RealAccount,str(account_id)) is None:
                        raise HTTPException(status_code=404,detail='Cuenta REAL no encontrada',headers={'Cache-Control':'no-store'})
            # Backtest/PAPER verification can be slow. Do not hold REAL or a
            # vault lease: STOP/lock must remain available while it runs.
            result=action(datetime.now(timezone.utc))
            with access._lock:
                access.require(token,touch=False)
                if access.vault.session_generation!=generation:
                    raise ValueError('La sesión REAL cambió durante la preparación')
            return result
        except ValueError as exc:
            raise HTTPException(status_code=409,detail=str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.get('/sources',dependencies=[Depends(authorized_passive)])
    def candidates(account_id:UUID,response:Response,
                   token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return call(account_id,token,response,lambda now:sources.candidates(now=now),passive=True)

    @router.get('/sources/{kind}/{session_id}',dependencies=[Depends(authorized_passive)])
    def selected_candidate(account_id:UUID,kind:SourceKind,session_id:UUID,response:Response,
                           token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return call(account_id,token,response,
            lambda now:sources.candidates(now=now,selection=(kind,str(session_id))),passive=True)

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(account_id:UUID,body:DexBotPreview,response:Response,
                token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return call(account_id,token,response,lambda now:sources.researched_preview(kind=body.source_kind,
            session_id=str(body.paper_session_id),now=now,
            research_validation_id=str(body.research_validation_id) if body.research_validation_id else None,
            **body.model_dump(exclude={'source_kind','paper_session_id','research_validation_id'})))

    return router
