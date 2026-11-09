"""Prepare owner renewal and apply its exact receipt; never activate or sign."""
import hashlib,hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_state import BnbReadClient,read_executor_state
from app.portfolio import dex_bot_rollover as rollover,dex_bot_bindings as bindings,dex_bot_journal as bots,dex_controls as controls
from app.portfolio.dex_deployments import deployment_record
from app.portfolio.models import RealDexBotBinding
from app.portfolio.wallet_proof import canonical_wallet_address
from app.live_execution.dex_versions import ROLLOVER_CONTROLS


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(min_length=42,max_length=42)


class Prepare(Scope):
    request_id:UUID=Field(strict=False)
    previous_run_id:UUID=Field(strict=False)
    draft_id:UUID=Field(strict=False)
    expires_at:int=Field(ge=1,le=2**64-1)
    gas:str=Field(pattern=r'^[1-9][0-9]{0,6}$')
    gas_price:str=Field(pattern=r'^[1-9][0-9]{0,12}$')
    max_gas_bnb:str=Field(min_length=1,max_length=100)


class Apply(Scope):
    confirmation:Literal['APLICAR_RENOVACION_BOT_DEX']


def create_dex_bot_rollover_router(access,*,authorized,rpc_factory=None)->APIRouter:
    router=APIRouter(prefix='/accounts/{account_id}/dex-bots/renewals',tags=['dex-bot-renewal'])
    factory=rpc_factory or BnbReadClient;supervisor=access._dex_bots

    def verified(token,account,wallet):
        access.require(token)
        digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
        if (account,'0x38',digest) not in access._wallet_verified:
            raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión REAL')

    def execute(account,body,response,token,*,identifier=None):
        response.headers['Cache-Control']='no-store';account=str(account)
        try:
            wallet=canonical_wallet_address(body.address)
            def resolve(db):
                if identifier is None:
                    operator=access._dex_operator_drafts.require_verified(identifier=str(body.draft_id),account_id=account,
                        wallet_address=wallet,now=supervisor.now())
                    replay=rollover.replay_rollover(db,identifier=str(body.request_id),account_id=account,
                        previous_run_id=str(body.previous_run_id),wallet_address=wallet,operator_address=operator,
                        expires_at=body.expires_at,gas=body.gas,gas_price=body.gas_price,max_gas_bnb=body.max_gas_bnb,now=supervisor.now())
                    if replay is not None:return None,None,replay
                    run=bots.run_record(db,str(body.previous_run_id))[0]
                    parent,binding,_=bindings.binding_record(db,run.binding_id)
                else:
                    row,control,_=controls.control_record(db,identifier)
                    if (row.account_id!=account or control['schema'] not in ROLLOVER_CONTROLS
                            or control['wallet_digest']!=controls._identity(db,'wallet',wallet)):
                        raise DexExecutorError('La renovación pertenece a otra cuenta o wallet')
                    if db.get(RealDexBotBinding,rollover.successor_id(identifier)) is not None:
                        return None,None,rollover.apply_rollover(db,identifier=identifier,account_id=account,
                            wallet_address=wallet,snapshot=None,now=supervisor.now())
                    if row.status!='FINALIZED':raise DexExecutorError('Confirma el recibo original de la renovación')
                    parent,binding,_=bindings.binding_record(db,control['bot_rollover']['previous_binding_id'])
                    operator=None
                if parent.account_id!=account or binding['wallet_digest']!=controls._identity(db,'wallet',wallet):
                    raise DexExecutorError('El bot pertenece a otra cuenta o wallet')
                return deployment_record(db,parent.deployment_id)[1],operator,None

            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:deployment,operator,replay=resolve(db)
                if replay is not None:return replay
                supervisor.require_idle()
            # Public RPC does not hold REAL access or vault locks. Recheck the
            # proof, epoch and stopped worker before persisting anything.
            with factory() as rpc:
                snapshot=read_executor_state(rpc,wallet,int(deployment['nonce']),deployment['fee'],
                    candidate_operator=operator,now=supervisor.now(),block_tag='finalized')
            with access._lock:
                verified(token,account,wallet)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la renovación')
                supervisor.require_idle()
                with access.vault.session() as db:
                    if identifier is not None:
                        return rollover.apply_rollover(db,identifier=identifier,account_id=account,wallet_address=wallet,
                            snapshot=snapshot,now=supervisor.now())
                    current=access._dex_operator_drafts.require_verified(identifier=str(body.draft_id),account_id=account,
                        wallet_address=wallet,now=supervisor.now())
                    if current!=operator:raise DexExecutorError('La operadora cambió durante la revisión')
                    return rollover.prepare_rollover(db,identifier=str(body.request_id),account_id=account,
                        previous_run_id=str(body.previous_run_id),wallet_address=wallet,operator_address=operator,snapshot=snapshot,
                        expires_at=body.expires_at,gas=body.gas,gas_price=body.gas_price,max_gas_bnb=body.max_gas_bnb,now=supervisor.now())
        except ValueError as exc:
            with access._lock:access.require(token,touch=False)
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.post('',dependencies=[Depends(authorized)])
    def prepare(account_id:UUID,body:Prepare,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return execute(account_id,body,response,token)

    @router.post('/{identifier}/apply',dependencies=[Depends(authorized)])
    def apply(account_id:UUID,identifier:UUID,body:Apply,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return execute(account_id,body,response,token,identifier=str(identifier))

    return router
