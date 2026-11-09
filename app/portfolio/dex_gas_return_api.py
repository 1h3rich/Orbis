"""Explicit protected BNB recovery to the verified operator's original owner."""
from uuid import UUID
import hashlib,hmac
from typing import Literal

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_return import read_return_state,plan_return
from app.live_execution.dex_gas_return_outbox import prepare_return_execution
from app.live_execution.dex_gas_return_transport import BnbGasReturnSender
from app.live_execution.dex_gas_funding import collect_funding_receipt
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.live_execution.evm_transaction import quantity
from app.portfolio import dex_gas_return as journal,dex_bot_bindings as bindings,dex_operator_journal as operator
from app.portfolio.models import RealAccount,RealDexGasReturnIntent
from app.portfolio.wallet_funding import exact_units
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.vault import VaultError


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')


class Review(Scope):
    request_id:UUID=Field(strict=False)
    binding_id:UUID=Field(strict=False)
    draft_id:UUID=Field(strict=False)
    gas_price:str=Field(pattern=r'^[1-9][0-9]{0,12}$')
    max_gas_bnb:str=Field(min_length=1,max_length=100)


class Submit(Scope):
    draft_id:UUID=Field(strict=False)
    plan_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['DEVOLVER_GAS_OPERADORA_DEX']


class Observe(Scope):
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class Cancel(Scope):
    confirmation:Literal['CANCELAR_REVISION_DEVOLUCION_GAS']


class Abandon(Scope):
    confirmation:Literal['DESCARTAR_DEVOLUCION_NO_EMITIDA']


def create_dex_gas_return_router(access,*,authorized,authorized_passive,rpc_factory=None,sender_factory=None):
    router=APIRouter(prefix='/accounts/{account_id}/dex-gas-returns',tags=['dex-gas-return'])
    factory=rpc_factory or BnbOperatorReadClient;sender=sender_factory or BnbGasReturnSender
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

    def owned(db,account,wallet,identifier):
        row,terms,state=journal.return_record(db,str(identifier))
        if row.account_id!=str(account) or row.wallet_digest!=journal._identity(db,'wallet',wallet):raise DexExecutorError('La devolución pertenece a otra cuenta o wallet')
        return row,terms,state

    def operator_address(db,account,wallet,binding_id,draft_id):
        parent,binding,_=bindings.binding_record(db,str(binding_id));_,grant,_=operator.grant_record(db,parent.grant_id)
        address=drafts.require_verified(identifier=str(draft_id),account_id=str(account),wallet_address=wallet,now=now())
        if (parent.account_id!=str(account) or binding['wallet_digest']!=journal._identity(db,'wallet',wallet)
                or grant['operator_digest']!=journal._identity(db,'wallet',address)):raise DexExecutorError('La operadora o wallet de devolución cambió')
        return address

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(account_id:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=True)
                with access.vault.session() as db:
                    return [journal.view_return(db,row.id,now=now()) for row in db.scalars(select(RealDexGasReturnIntent).where(
                        RealDexGasReturnIntent.account_id==str(account_id)).order_by(RealDexGasReturnIntent.created_at.desc()).limit(100))]
        except ValueError as exc:raise failure(exc,token) from None

    @router.get('/{identifier}',dependencies=[Depends(authorized_passive)])
    def detail(account_id:UUID,identifier:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=True)
                with access.vault.session() as db:
                    row,terms,state=journal.return_record(db,str(identifier))
                    if row.account_id!=str(account_id):raise DexExecutorError('La devolución pertenece a otra cuenta')
                    return journal.view_return(db,str(identifier),now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('',dependencies=[Depends(authorized)])
    def review(account_id:UUID,body:Review,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);maximum=exact_units(body.max_gas_bnb,18);price=int(body.gas_price)
            with access._lock:
                verified(token,account_id,wallet);supervisor.require_idle();generation=access.vault.session_generation
                with access.vault.session() as db:
                    if db.get(RealDexGasReturnIntent,str(body.request_id)) is not None:
                        row,terms,state=owned(db,account_id,wallet,body.request_id)
                        if (row.binding_id!=str(body.binding_id) or terms['draft_id']!=str(body.draft_id)
                                or terms['gas_price']!=str(price) or terms['max_gas_wei']!=str(maximum)):raise DexExecutorError('La revisión ya tiene otros datos')
                        return {**journal.view_return(db,row.id,now=now()),'replayed':True}
                    address=operator_address(db,account_id,wallet,body.binding_id,body.draft_id)
            with factory() as rpc:state=read_return_state(rpc,wallet,address,now=now())
            plan=plan_return(state,gas_price=price,max_gas_wei=maximum,now=now())
            with access._lock:
                verified(token,account_id,wallet);supervisor.require_idle()
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la revisión')
                with access.vault.session() as db:return journal.prepare_return(db,identifier=str(body.request_id),account_id=str(account_id),
                    binding_id=str(body.binding_id),draft_id=str(body.draft_id),wallet_address=wallet,drafts=drafts,plan=plan,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/submit',dependencies=[Depends(authorized)])
    def submit(account_id:UUID,identifier:UUID,body:Submit,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';packet=None;generation=None;attempted=False
        try:
            wallet=canonical_wallet_address(body.address);account=str(account_id);identifier=str(identifier)
            with access._lock:
                verified(token,account,wallet);supervisor.require_idle();generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,terms,state=owned(db,account,wallet,identifier)
                    if row.payload_digest!=body.plan_digest:raise DexExecutorError('La revisión de devolución cambió')
                    if row.status!='PREPARED':return {**journal.view_return(db,row.id,now=now()),'replayed_without_post':True,'new_order_post_attempted':False}
                    address=operator_address(db,account,wallet,row.binding_id,body.draft_id)
                    price,maximum=int(terms['gas_price']),int(terms['max_gas_wei'])
            with factory() as rpc:state=read_return_state(rpc,wallet,address,now=now())
            plan=plan_return(state,gas_price=price,max_gas_wei=maximum,now=now())
            with access._lock:
                verified(token,account,wallet);supervisor.require_idle()
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la devolución')
                access._dex_connection.disconnect(account_id=account,wallet_address=wallet)
                packet=prepare_return_execution(access.vault,drafts,identifier=identifier,account_id=account,wallet_address=wallet,
                    draft_id=str(body.draft_id),plan=plan,plan_digest=body.plan_digest,now=now())
                if isinstance(packet,dict):return packet
            original=packet.transaction_hash
            def active():
                with access._lock:
                    verified(token,account,wallet)
                    if access.vault.session_generation!=generation:raise DexExecutorError('La sesión de devolución cambió')
            with sender(plan) as transport:
                result=packet.broadcast_once(transport.send,now=now(),require_active=active);attempted=True
            # Once a POST was claimed, read failure is uncertainty, not a reason
            # to issue another transfer. Preserve the original hash in RAM/UI.
            try:
                with factory() as rpc:evidence=collect_funding_receipt(rpc,original)
                finalized=evidence is not None and quantity(evidence['finalized']['number'])>=quantity(evidence['receipt']['blockNumber'])
            except (ValueError,KeyError,TypeError):evidence=None;finalized=False
            with access._lock:
                verified(token,account,wallet)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión cambió; conserva el hash original')
                with access.vault.session() as db:
                    if finalized:
                        checked=journal.check_return(db,identifier,account_id=account,evidence=evidence,now=now())
                    else:checked=journal.view_return(db,identifier,now=now())
            return {**checked,'tx_hash':original,'send_result':result['send_result'],'new_order_post_attempted':True}
        except ValueError as exc:raise failure(exc,token) from None
        finally:
            if packet is not None and not isinstance(packet,dict) and not attempted:
                try:
                    with access.vault.guard_session_generation(generation),access.vault.session() as db:
                        journal.abandon_unsent(db,identifier,account_id=str(account_id),now=now())
                except (ValueError,VaultError):pass

    @router.post('/{identifier}/observe',dependencies=[Depends(authorized)])
    def observe(account_id:UUID,identifier:UUID,body:Observe,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            with access._lock:
                verified(token,account_id,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    _,_,state=owned(db,account_id,wallet,identifier)
                    if state['tx_ref']!=journal._identity(db,'tx',body.tx_hash):raise DexExecutorError('Conserva el hash original de la devolución')
            with factory() as rpc:evidence=collect_funding_receipt(rpc,body.tx_hash)
            with access._lock:
                verified(token,account_id,wallet)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la observación')
                with access.vault.session() as db:
                    if evidence is None:return {**journal.view_return(db,str(identifier),now=now()),'observation_status':'PENDING'}
                    return journal.check_return(db,str(identifier),account_id=str(account_id),evidence=evidence,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    def close_review(account_id,identifier,body,response,token,*,abandon=False):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            with access._lock:
                verified(token,account_id,wallet)
                with access.vault.session() as db:
                    owned(db,account_id,wallet,identifier);db.commit()
                    action=journal.abandon_unsent if abandon else journal.cancel_return
                    return action(db,str(identifier),account_id=str(account_id),now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/cancel',dependencies=[Depends(authorized)])
    def cancel(account_id:UUID,identifier:UUID,body:Cancel,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return close_review(account_id,identifier,body,response,token)

    @router.post('/{identifier}/abandon',dependencies=[Depends(authorized)])
    def abandon(account_id:UUID,identifier:UUID,body:Abandon,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return close_review(account_id,identifier,body,response,token,abandon=True)

    return router
