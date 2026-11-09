"""Explicit, owner-proven cancellation of an uncertain operator BNB return."""
from uuid import UUID
import hashlib,hmac
from typing import Literal
from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_cancellation import read_cancellation_state,plan_cancellation
from app.live_execution.dex_gas_cancellation_outbox import prepare_cancellation_execution
from app.live_execution.dex_gas_cancellation_transport import BnbGasCancellationSender
from app.live_execution.dex_native_resolution import collect_resolution
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.portfolio import dex_gas_cancellation as journal,dex_gas_return as original
from app.portfolio.models import RealAccount,RealDexGasCancellationIntent
from app.portfolio.wallet_funding import exact_units
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.vault import VaultError


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')


class Review(Scope):
    request_id:UUID=Field(strict=False)
    return_id:UUID=Field(strict=False)
    draft_id:UUID=Field(strict=False)
    gas_price:str=Field(pattern=r'^[1-9][0-9]{0,12}$')
    bump_percent:int=Field(ge=1,le=1000)
    max_gas_bnb:str=Field(min_length=1,max_length=100)


class Submit(Scope):
    draft_id:UUID=Field(strict=False)
    plan_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    original_hash:str|None=Field(default=None,pattern=r'^0x[0-9a-fA-F]{64}$')
    confirmation:Literal['CANCELAR_DEVOLUCION_BNB']


class Observe(Scope):
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class Cancel(Scope):
    confirmation:Literal['RETIRAR_REVISION_CANCELACION_BNB']


class Abandon(Scope):
    confirmation:Literal['DESCARTAR_CANCELACION_BNB_NO_EMITIDA']


def create_dex_gas_cancellation_router(access,*,authorized,authorized_passive,rpc_factory=None,sender_factory=None):
    router=APIRouter(prefix='/accounts/{account_id}/dex-gas-cancellations',tags=['dex-gas-cancellation'])
    factory=rpc_factory or BnbOperatorReadClient;sender=sender_factory or BnbGasCancellationSender
    drafts=access._dex_operator_drafts;supervisor=access._dex_bots;now=supervisor.now

    def verified(token,account,wallet=None,*,passive=False):
        access.require(token,touch=not passive)
        with access.vault.session() as db:
            if db.get(RealAccount,str(account)) is None:raise DexExecutorError('Cuenta REAL no encontrada')
        if wallet is not None:
            reference=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if (str(account),'0x38',reference) not in access._wallet_verified:raise DexExecutorError('Verifica esta wallet propietaria en BNB Chain')

    def failure(exc,token):
        with access._lock:access.require(token,touch=False)
        return HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})

    def source(db,account,wallet,return_id,draft_id):
        row,terms,state=original.return_record(db,str(return_id))
        address=drafts.require_verified(identifier=str(draft_id),account_id=str(account),wallet_address=wallet,now=now())
        if (row.account_id!=str(account) or terms['wallet_digest']!=journal._identity(db,'wallet',wallet)
                or terms['operator_digest']!=journal._identity(db,'wallet',address)):
            raise DexExecutorError('La operadora o wallet de cancelación cambió')
        return row,terms,state,address

    def same_generation(generation):
        if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la cancelación')

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(account_id:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=True)
                with access.vault.session() as db:
                    return [journal.view_cancellation(db,identifier,now=now()) for identifier in db.scalars(select(RealDexGasCancellationIntent.id).where(
                        RealDexGasCancellationIntent.account_id==str(account_id)).order_by(RealDexGasCancellationIntent.created_at.desc()).limit(100))]
        except ValueError as exc:raise failure(exc,token) from None

    @router.get('/{identifier}',dependencies=[Depends(authorized_passive)])
    def detail(account_id:UUID,identifier:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=True)
                with access.vault.session() as db:
                    row,_,_=journal._owned(db,str(identifier),str(account_id))
                    return journal.view_cancellation(db,row.id,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('',dependencies=[Depends(authorized)])
    def review(account_id:UUID,body:Review,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);maximum=exact_units(body.max_gas_bnb,18);price=int(body.gas_price)
            with access._lock:
                verified(token,account_id,wallet);supervisor.require_idle();generation=access.vault.session_generation
                with access.vault.session() as db:
                    if db.get(RealDexGasCancellationIntent,str(body.request_id)) is not None:
                        row,terms,state=journal._owned(db,str(body.request_id),str(account_id),wallet)
                        if (row.return_id!=str(body.return_id) or terms['draft_id']!=str(body.draft_id) or terms['gas_price']!=str(price)
                                or terms['bump_percent']!=body.bump_percent or terms['max_gas_wei']!=str(maximum)):
                            raise DexExecutorError('La revisión de cancelación ya tiene otros datos')
                        return {**journal.view_cancellation(db,row.id,now=now()),'replayed':True}
                    _,terms,_,address=source(db,account_id,wallet,body.return_id,body.draft_id);nonce=int(terms['nonce'])
            with factory() as rpc:state=read_cancellation_state(rpc,wallet,address,nonce=nonce,now=now())
            with access._lock:
                verified(token,account_id,wallet);supervisor.require_idle();same_generation(generation)
                with access.vault.session() as db:
                    return journal.prepare_cancellation(db,identifier=str(body.request_id),return_id=str(body.return_id),account_id=str(account_id),
                        wallet_address=wallet,draft_id=str(body.draft_id),drafts=drafts,snapshot=state,gas_price=price,
                        bump_percent=body.bump_percent,max_gas_wei=maximum,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/submit',dependencies=[Depends(authorized)])
    def submit(account_id:UUID,identifier:UUID,body:Submit,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';packet=None;generation=None;attempted=False
        try:
            wallet=canonical_wallet_address(body.address);account=str(account_id);identifier=str(identifier)
            with access._lock:
                verified(token,account,wallet);supervisor.require_idle();generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,terms,_=journal._owned(db,identifier,account,wallet)
                    if row.payload_digest!=body.plan_digest:raise DexExecutorError('La revisión de cancelación cambió')
                    if row.status!='PREPARED':return {**journal.view_cancellation(db,row.id,now=now()),'replayed_without_post':True,'new_order_post_attempted':False}
                    _,_,pstate,address=source(db,account,wallet,row.return_id,body.draft_id)
                    if body.original_hash is not None and original._identity(db,'tx',body.original_hash)!=pstate['tx_ref']:
                        raise DexExecutorError('Conserva el hash original de la devolución')
            with factory() as rpc:state=read_cancellation_state(rpc,wallet,address,nonce=int(terms['nonce']),now=now())
            plan=plan_cancellation(state,original_nonce=int(terms['nonce']),original_gas_price=int(terms['base_gas_price']),
                gas_price=int(terms['gas_price']),bump_percent=terms['bump_percent'],max_gas_wei=int(terms['max_gas_wei']),now=now())
            with access._lock:
                verified(token,account,wallet);supervisor.require_idle();same_generation(generation)
                access._dex_connection.disconnect(account_id=account,wallet_address=wallet)
                packet=prepare_cancellation_execution(access.vault,drafts,identifier=identifier,account_id=account,wallet_address=wallet,
                    draft_id=str(body.draft_id),plan=plan,plan_digest=body.plan_digest,now=now())
                if isinstance(packet,dict):return packet
            tx_hash=packet.transaction_hash
            def active():
                with access._lock:
                    verified(token,account,wallet);supervisor.require_idle();same_generation(generation)
            with sender(plan) as transport:
                result=packet.broadcast_once(transport.send,now=now(),require_active=active);attempted=True
            evidence=None
            for candidate in (tx_hash,body.original_hash):
                if candidate is None:continue
                try:
                    with factory() as rpc:evidence=collect_resolution(rpc,candidate)
                except ValueError:evidence=None
                if evidence is not None:break
            with access._lock:
                verified(token,account,wallet);same_generation(generation)
                with access.vault.session() as db:
                    checked=(journal.observe_cancellation(db,identifier,account_id=account,wallet_address=wallet,evidence=evidence,now=now())
                        if evidence is not None else journal.view_cancellation(db,identifier,now=now()))
            return {**checked,'tx_hash':tx_hash,'send_result':result['send_result'],'new_order_post_attempted':True}
        except ValueError as exc:raise failure(exc,token) from None
        finally:
            if packet is not None and not isinstance(packet,dict) and not attempted:
                try:
                    with access.vault.guard_session_generation(generation),access.vault.session() as db:
                        journal.abandon_unsent(db,str(identifier),account_id=str(account_id),now=now())
                except (ValueError,VaultError):pass

    @router.post('/{identifier}/observe',dependencies=[Depends(authorized)])
    def observe(account_id:UUID,identifier:UUID,body:Observe,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);identifier=str(identifier);account=str(account_id)
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,_,_=journal._owned(db,identifier,account,wallet)
                    if row.status=='SETTLED':return journal.view_cancellation(db,identifier,now=now())
                    if journal._known(db,row) is not None:
                        db.commit();return journal.settle_known(db,identifier,account_id=account,now=now())
                    journal.require_observation_hash(db,row,body.tx_hash)
            with factory() as rpc:evidence=collect_resolution(rpc,body.tx_hash)
            with access._lock:
                verified(token,account,wallet);same_generation(generation)
                with access.vault.session() as db:
                    if evidence is None:return {**journal.view_cancellation(db,identifier,now=now()),'observation_status':'PENDING'}
                    return journal.observe_cancellation(db,identifier,account_id=account,wallet_address=wallet,evidence=evidence,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    def close_review(account_id,identifier,body,response,token,*,abandon=False):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            with access._lock:
                verified(token,account_id,wallet)
                with access.vault.session() as db:
                    journal._owned(db,str(identifier),str(account_id),wallet);db.commit()
                    action=journal.abandon_unsent if abandon else journal.cancel_review
                    return action(db,str(identifier),account_id=str(account_id),now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/cancel',dependencies=[Depends(authorized)])
    def cancel(account_id:UUID,identifier:UUID,body:Cancel,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return close_review(account_id,identifier,body,response,token)

    @router.post('/{identifier}/abandon',dependencies=[Depends(authorized)])
    def abandon(account_id:UUID,identifier:UUID,body:Abandon,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return close_review(account_id,identifier,body,response,token,abandon=True)

    return router
