"""Durable tested-source binding, without a signing or activation endpoint."""
from datetime import datetime,timezone
import hashlib,hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import Field

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_origin import prepare_origin
from app.live_execution.dex_state import BnbReadClient,read_executor_state
from app.portfolio import dex_bot_bindings as bindings
from app.portfolio.dex_bot_api import DexBotPreview
from app.portfolio.dex_deployment_api import Body
from app.portfolio.dex_deployments import deployment_record,_identity
from app.portfolio.models import RealAccount
from app.portfolio.wallet_funding import exact_units
from app.portfolio.wallet_proof import canonical_wallet_address


class Bind(DexBotPreview):
    request_id:UUID=Field(strict=False)
    deployment_id:UUID=Field(strict=False)
    address:str=Field(min_length=42,max_length=42)
    gas_budget_bnb:str=Field(min_length=1,max_length=100)
    confirmation:Literal['VINCULAR_BOT_DEX']


class Stop(Body):
    confirmation:Literal['RETIRAR_VINCULO_DEX']


def create_dex_bot_binding_router(access,*,sources,authorized,authorized_passive,rpc_factory=None) -> APIRouter:
    router=APIRouter(prefix='/accounts/{account_id}/dex-bots/bindings',tags=['dex-bot-bindings'])
    factory=rpc_factory or BnbReadClient

    def verified(token,account_id,wallet=None,*,passive=False):
        access.require(token,touch=not passive)
        if wallet is not None:
            digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if (str(account_id),'0x38',digest) not in access._wallet_verified:
                raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión REAL')

    def failure(exc):return HTTPException(status_code=409,detail=str(exc),headers={'Cache-Control':'no-store'})

    def local(account_id,token,response,action,*,passive=False):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=passive)
                with access.vault.session() as db:
                    if db.get(RealAccount,str(account_id)) is None:
                        raise HTTPException(status_code=404,detail='Cuenta REAL no encontrada',headers={'Cache-Control':'no-store'})
                    return action(db,datetime.now(timezone.utc))
        except ValueError as exc:raise failure(exc) from None

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(account_id:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,token,response,lambda db,now:bindings.list_bindings(db,str(account_id),now=now),passive=True)

    @router.get('/{identifier}',dependencies=[Depends(authorized_passive)])
    def detail(account_id:UUID,identifier:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        def read(db,now):
            row,_,_=bindings.binding_record(db,str(identifier))
            if row.account_id!=str(account_id):raise DexExecutorError('El vínculo no pertenece a esta cuenta')
            return bindings.binding_view(db,str(identifier),now=now)
        return local(account_id,token,response,read,passive=True)

    @router.get('/{identifier}/research-options',dependencies=[Depends(authorized_passive)])
    def research_options(account_id:UUID,identifier:UUID,response:Response,
                         token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=True)
                generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,terms,_=bindings.binding_record(db,str(identifier))
                    if row.account_id!=str(account_id):raise DexExecutorError('El vínculo no pertenece a esta cuenta')
                    review=terms['origin']['review']
            result=sources.research_options(review['source_kind'],review['source'])
            with access._lock:
                verified(token,account_id,passive=True)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la consulta')
            return {'verification':'ON_ACTIVATION','options':result}
        except ValueError as exc:raise failure(exc) from None

    @router.post('',dependencies=[Depends(authorized)])
    def bind(account_id:UUID,body:Bind,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);gas=exact_units(body.gas_budget_bnb,18)
            parameters=body.model_dump(exclude={'request_id','deployment_id','address','gas_budget_bnb','confirmation','source_kind','paper_session_id','research_validation_id'})
            parameters.update(kind=body.source_kind,session_id=str(body.paper_session_id))
            request=bindings.request_terms(account_id=str(account_id),deployment_id=str(body.deployment_id),
                source_kind=body.source_kind,paper_session_id=str(body.paper_session_id),gas_budget_wei=gas,
                **{k:v for k,v in parameters.items() if k not in ('kind','session_id')})
            with access._lock:
                verified(token,account_id,wallet)
                generation=access.vault.session_generation
                with access.vault.session() as db:
                    replay=bindings.replay_binding(db,str(body.request_id),request=request,wallet_address=wallet,now=datetime.now(timezone.utc))
                    if replay:
                        proof=replay['research_qualification']
                        if body.research_validation_id and (proof is None or proof['evidence']['research']['artifact_id']!=str(body.research_validation_id)):
                            raise DexExecutorError('La solicitud de vínculo ya tiene otra reserva')
                        return replay
                    if body.research_validation_id is None:raise DexExecutorError('Selecciona una reserva fuera de muestra')
                    parent,terms,state=deployment_record(db,str(body.deployment_id))
                    if (parent.account_id!=str(account_id) or parent.wallet_digest!=_identity(db,'wallet',wallet)
                            or not (parent.status=='FINALIZED' or state['code_observation'] is not None)):
                        raise DexExecutorError('Despliegue no verificado para esta cuenta y wallet')
                    nonce,fee=int(terms['nonce']),terms['fee']
            # No REAL/vault locks during either backtest verification or RPC.
            prepared=prepare_origin(sources,now=datetime.now(timezone.utc),research_validation_id=str(body.research_validation_id),**parameters)
            with factory() as rpc:
                snapshot=read_executor_state(rpc,wallet,nonce,fee,now=datetime.now(timezone.utc),block_tag='finalized')
            with access._lock:
                verified(token,account_id,wallet)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la vinculación')
                with access.vault.session() as db:
                    return bindings.create_binding(db,identifier=str(body.request_id),account_id=str(account_id),
                        deployment_id=str(body.deployment_id),wallet_address=wallet,prepared=prepared,snapshot=snapshot,
                        gas_budget_wei=gas,now=datetime.now(timezone.utc))
        except ValueError as exc:raise failure(exc) from None

    @router.post('/{identifier}/stop',dependencies=[Depends(authorized)])
    def stop(account_id:UUID,identifier:UUID,body:Stop,response:Response,
             token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,token,response,lambda db,now:bindings.stop_binding(db,str(identifier),account_id=str(account_id),now=now))

    return router
