"""Private joint review of saved receipts; no RPC, signatures or trading calls."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime,timedelta
import hashlib,hmac,json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import Field,field_validator,model_validator

from app.portfolio import economic_external_group_acceptance as acceptance,economic_external_booking as booking
from app.portfolio import economic_external_group as groups
from app.portfolio.economic_external_api import ExternalReview,_declaration,_selection_digest
from app.portfolio.economic_external_batch import prepare_external_batch,MAX_DECLARATIONS
from app.portfolio.economic_external_capture import capture_external_effects,WalletParticipant
from app.portfolio.economic_review_api import StrictModel,Key,ManualOrder,current_snapshots
from app.portfolio.economic_projection import ProjectionError,TieOrder
from app.portfolio.economic_projection_review import _snapshot
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.domain import PortfolioError,AccountSnapshot
from app.portfolio.vault import VaultError
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_funding import _encoded,_time


class Declaration(StrictModel):
    source_kind:Literal['WALLET_SWAP','OPERATOR']
    source_id:Key
    context_account_id:Key
    review:ExternalReview

    @model_validator(mode='after')
    def group_metadata(self):
        if self.review.recorded_at is not None or self.review.tie_orders:
            raise ValueError('La fecha y el orden manual se indican para todo el conjunto')
        return self


class GroupReview(StrictModel):
    recorded_at:str|None=Field(default=None,min_length=20,max_length=40)
    declarations:list[Declaration]=Field(min_length=2,max_length=MAX_DECLARATIONS)
    tie_orders:list[ManualOrder]=Field(default_factory=list,max_length=10000)

    @field_validator('recorded_at')
    @classmethod
    def stamp(cls,value):return ExternalReview.stamp(value)

    @model_validator(mode='after')
    def unique(self):
        sources={(item.source_kind,item.source_id)for item in self.declarations}
        if len(sources)!=len(self.declarations)or len({item.review.review_id for item in self.declarations})!=len(self.declarations):
            raise ValueError('El conjunto repite una fuente o revisión')
        return self


class GroupConfirmation(GroupReview):
    recorded_at:str=Field(min_length=20,max_length=40)
    preview_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['ACEPTAR_EFECTOS_EXTERNOS_CONJUNTOS']


def _contexts(body):
    return tuple(sorted({(context,canonical_wallet_address(address))for declaration in body.declarations
        for context,address in [(declaration.context_account_id,declaration.review.address),
            *((item.context_account_id,item.address)for item in declaration.review.wallets)]}))


def _selection(body,stamp,root,group_id,salt):
    values=[{'source_kind':item.source_kind,'source_id':item.source_id,'context_account_id':item.context_account_id,
        'selection_digest':_selection_digest(item.review,stamp,item.source_kind,salt)}for item in body.declarations]
    return _encoded({'schema':'orbis.external.group.selection.v1','root_account_id':root,'group_id':group_id,
        'recorded_at':stamp.isoformat(),'declarations':sorted(values,key=lambda item:(item['source_kind'],item['source_id'])),
        'tie_orders':[item.model_dump()for item in body.tie_orders]})[1]


def create_external_group_router(access,*,authorized):
    router=APIRouter(prefix='/accounts/{account_id}/external-effect-groups/{group_id}',tags=['external-effects'])

    def verified(token,contexts,*,generation=None):
        access.require(token,touch=False)
        if generation is not None and access.vault.session_generation!=generation:raise VaultError('The real vault session changed')
        for context,wallet in contexts:
            digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if(context,'0x38',digest)not in access._wallet_verified:
                raise ProjectionError('Verifica el control de cada wallet y su contexto en BNB Chain')

    def run(account_id,group_id,body,response,token,*,save):
        response.headers['Cache-Control']='no-store';root=str(account_id);identifier=str(group_id)
        contexts=_contexts(body);wallets=tuple(sorted({wallet for _,wallet in contexts}))
        def capture(db):
            return tuple(capture_external_effects(db,item.source_kind,item.source_id,
                context_account_id=item.context_account_id,owner_address=item.review.address,
                wallets=tuple(WalletParticipant(value.context_account_id,value.address)for value in item.review.wallets))
                for item in body.declarations)
        def idle():
            if any(item.source_kind=='OPERATOR'for item in body.declarations):access._dex_bots.require_idle()
        @contextmanager
        def guard():
            with access._lock:verified(token,contexts,generation=generation);idle();yield
        try:
            now=_time(access._dex_bots.now());stamp=_time(datetime.fromisoformat(body.recorded_at))if body.recorded_at else now
            with access._lock:
                verified(token,contexts);generation=access.vault.session_generation;idle()
                with access.vault.session()as db:
                    inputs=capture(db);selection=_selection(body,stamp,root,identifier,_existing_salt(db))
                    existing=groups.group_record(db,identifier)
                    if existing is not None:
                        for item in inputs:
                            recorded=booking.booking_record(db,booking.identifier(item.observation_id))
                            if recorded is None or recorded[1]['plan']['custody']!=json.loads(item.custody_json):
                                raise ProjectionError('Cambió la custodia de una declaración guardada')
            if existing is not None:
                if not save:raise ProjectionError('Este grupo ya está aceptado; recupera el resultado guardado')
                result=acceptance.recover_group(access.vault,identifier,root_account_id=root,selection_digest=selection)
                if result['preview_digest']!=body.preview_digest:raise ProjectionError('La respuesta pertenece a otra revisión conjunta')
                with guard():return result
            if stamp>now+timedelta(seconds=5):raise ProjectionError('La revisión no puede fecharse en el futuro')
            declarations=tuple(_declaration(item,selected.review)for item,selected in zip(inputs,body.declarations,strict=True))
            ties=tuple(TieOrder(item.account_id,datetime.fromisoformat(item.at),tuple(item.nodes))for item in body.tie_orders)
            plan=prepare_external_batch(declarations,batch_id=identifier,root_account_id=root,recorded_at=stamp,tie_orders=ties)
            complete=json.loads(plan.body_json)['manual_review_complete']
            prepared=acceptance.prepare_group(access.vault,inputs,plan,capture=capture,generation=generation,
                selection_digest=selection,wallet_addresses=wallets,tie_orders=ties)if complete else None
            if save:
                if prepared is None:raise ProjectionError('Revisa los efectos desconocidos antes de aceptar')
                return acceptance.confirm_group(access.vault,prepared,capture=capture,preview_digest=body.preview_digest,
                    now=_time(access._dex_bots.now()),generation=generation,commit_guard=guard,wallet_addresses=wallets)
            base=inputs[0];baseline={}
            for accounts,current in base.currents:
                baseline.update(current_snapshots(tuple(event for event in base.events if event.account_id in accounts),accounts,current))
            comparisons=[{'account_id':account,'name':name,'current':baseline[account][0],'current_error':baseline[account][1],
                'proposed':_snapshot(plan.projection.snapshots.get(account,AccountSnapshot(account))),
                'changed':baseline[account][0]!=_snapshot(plan.projection.snapshots.get(account,AccountSnapshot(account)))}for account,name in base.account_names]
            with guard(),access.vault.session()as db:acceptance._unchanged(db,acceptance._captures(inputs),capture)
            operating=json.loads(prepared.staged.operating_json)if prepared else{'can_accept':False,'blockers':[{'code':'EFFECTS_UNKNOWN'}]}
            return {'group_id':identifier,'recorded_at':stamp.isoformat(),'preview_digest':prepared.digest if prepared else plan.digest,
                'root_account_id':root,'manual_review_complete':complete,'accounts':comparisons,
                'declarations':[{'observation_id':item.observation_id,'order_record_id':item.order_id}for item in acceptance._captures(inputs)],
                'events_to_create':len(plan.created_events),'events_count':len(base.events)+len(plan.created_events),
                'checked_receipts':len(base.claims)+len(inputs),'saved':False,'acceptance_available':complete,
                'can_accept':operating['can_accept'],'operating':operating,'accounting_acceptance_active':False,
                'effects_proven_complete':False,'strategy_executed':False,'resolves_intent':False,'moves_funds':False,'trading_authority':False}
        except(PortfolioError,VaultError,ValueError)as exc:
            with access._lock:access.require(token,touch=False)
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})from None

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(account_id:UUID,group_id:UUID,body:GroupReview,response:Response,
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,group_id,body,response,token,save=False)

    @router.post('/confirm',dependencies=[Depends(authorized)])
    def confirm(account_id:UUID,group_id:UUID,body:GroupConfirmation,response:Response,
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,group_id,body,response,token,save=True)

    @router.get('',dependencies=[Depends(authorized)])
    def saved(account_id:UUID,group_id:UUID,response:Response,
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:access.require(token,touch=False);generation=access.vault.session_generation
            result=acceptance.recover_group(access.vault,str(group_id),root_account_id=str(account_id))
            with access._lock:
                access.require(token,touch=False)
                if generation!=access.vault.session_generation:raise VaultError('The real vault session changed')
            return result or{'saved':False,'accounting_method_active':False,'resolves_intent':False,'trading_authority':False}
        except(PortfolioError,VaultError,ValueError)as exc:
            with access._lock:access.require(token,touch=False)
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})from None
    return router
