"""Owner-proven native funding cancellation. No server signer or sends."""
from uuid import UUID
import hashlib,hmac
from typing import Literal
from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_wallet_cancellation import read_wallet_cancellation_state,plan_wallet_cancellation
from app.live_execution.dex_native_resolution import collect_resolution
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.portfolio import dex_wallet_cancellation as journal,dex_gas_funding as funding
from app.portfolio.models import RealAccount,RealDexWalletCancellationIntent
from app.portfolio.wallet_funding import exact_units
from app.portfolio.wallet_proof import canonical_wallet_address


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')


class ReadScope(Scope):
    recipient:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')
    original_hash:str|None=Field(default=None,pattern=r'^0x[0-9a-fA-F]{64}$')


class Review(ReadScope):
    request_id:UUID=Field(strict=False)
    funding_id:UUID=Field(strict=False)
    gas_price:str=Field(pattern=r'^[1-9][0-9]{0,12}$')
    bump_percent:int=Field(ge=1,le=1000)
    max_gas_bnb:str=Field(min_length=1,max_length=100)
    accept_unknown_fee:bool=False


class Claim(ReadScope):
    plan_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['CANCELAR_FINANCIACION_BNB_EN_WALLET']


class Hash(Scope):
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class Rejected(Scope):
    confirmation:Literal['WALLET_RECHAZADA_4001']


class Cancel(Scope):
    confirmation:Literal['RETIRAR_REVISION_CANCELACION_WALLET_BNB']


def create_dex_wallet_cancellation_router(access,*,authorized,authorized_passive,rpc_factory=None):
    router=APIRouter(prefix='/accounts/{account_id}/dex-wallet-cancellations',tags=['dex-wallet-cancellation'])
    factory=rpc_factory or BnbOperatorReadClient;now=access._dex_bots.now

    def verified(token,account,wallet=None,*,passive=False):
        access.require(token,touch=not passive)
        with access.vault.session() as db:
            if db.get(RealAccount,str(account)) is None:raise DexExecutorError('Cuenta REAL no encontrada')
        if wallet is not None:
            reference=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if (str(account),'0x38',reference) not in access._wallet_verified:raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión')

    def failure(exc,token):
        with access._lock:access.require(token,touch=False)
        return HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})

    def same_generation(generation):
        if access.vault.session_generation!=generation:raise DexExecutorError('La sesión cambió durante la lectura de cancelación')

    def context(db,account,wallet,identifier,recipient,tx_hash):
        row,terms,state=funding.funding_record(db,str(identifier))
        if row.account_id!=str(account) or terms['wallet_digest']!=journal._identity(db,'wallet',wallet):raise DexExecutorError('Financiación de otra cuenta o wallet')
        recipient=canonical_wallet_address(recipient)
        if terms['operator_digest']!=journal._identity(db,'wallet',recipient):raise DexExecutorError('El destinatario original no coincide')
        if tx_hash is not None and state['tx_ref'] is not None and state['tx_ref']!=funding._identity(db,'tx',tx_hash):
            raise DexExecutorError('Conserva el hash original de financiación')
        return row,terms,recipient

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(account_id:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=True)
                with access.vault.session() as db:
                    return [journal.view_cancellation(db,identifier,now=now()) for identifier in db.scalars(select(RealDexWalletCancellationIntent.id).where(
                        RealDexWalletCancellationIntent.account_id==str(account_id)).order_by(RealDexWalletCancellationIntent.created_at.desc()).limit(100))]
        except ValueError as exc:raise failure(exc,token) from None

    @router.get('/{identifier}',dependencies=[Depends(authorized_passive)])
    def detail(account_id:UUID,identifier:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=True)
                with access.vault.session() as db:
                    row,_,_=journal._owned(db,str(identifier),str(account_id));return journal.view_cancellation(db,row.id,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('',dependencies=[Depends(authorized)])
    def review(account_id:UUID,body:Review,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);maximum=exact_units(body.max_gas_bnb,18);price=int(body.gas_price)
            with access._lock:
                verified(token,account_id,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    _,source,recipient=context(db,account_id,wallet,body.funding_id,body.recipient,body.original_hash)
                    if db.get(RealDexWalletCancellationIntent,str(body.request_id)) is not None:
                        row,terms,_=journal._owned(db,str(body.request_id),str(account_id),wallet)
                        reference=journal._identity(db,'tx',body.original_hash) if body.original_hash is not None else None
                        if terms['original_hash_ref']!=reference:raise DexExecutorError('El hash original de esta revisión cambió')
                        db.commit()
                        return journal.prepare_cancellation(db,identifier=row.id,funding_id=str(body.funding_id),account_id=str(account_id),wallet_address=wallet,
                            snapshot=None,gas_price=price,bump_percent=body.bump_percent,max_gas_wei=maximum,accept_unknown_fee=body.accept_unknown_fee,now=now())
            with factory() as rpc:
                snapshot=read_wallet_cancellation_state(rpc,wallet,recipient,nonce=int(source['nonce']),amount_wei=int(source['amount_wei']),original_hash=body.original_hash,now=now())
            with access._lock:
                verified(token,account_id,wallet);same_generation(generation)
                with access.vault.guard_session_generation(generation),access.vault.session() as db:
                    return journal.prepare_cancellation(db,identifier=str(body.request_id),funding_id=str(body.funding_id),account_id=str(account_id),wallet_address=wallet,
                        snapshot=snapshot,gas_price=price,bump_percent=body.bump_percent,max_gas_wei=maximum,accept_unknown_fee=body.accept_unknown_fee,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/claim',dependencies=[Depends(authorized)])
    def claim(account_id:UUID,identifier:UUID,body:Claim,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);account=str(account_id);identifier=str(identifier)
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,terms,_=journal._owned(db,identifier,account,wallet)
                    if row.payload_digest!=body.plan_digest:raise DexExecutorError('La revisión de wallet cambió')
                    if row.status!='PREPARED':return {**journal.view_cancellation(db,identifier,now=now()),'claim_granted':False}
                    _,source,recipient=context(db,account,wallet,row.funding_id,body.recipient,body.original_hash)
                    stored=journal.view_cancellation(db,identifier,now=now())
            with factory() as rpc:
                snapshot=read_wallet_cancellation_state(rpc,wallet,recipient,nonce=int(source['nonce']),amount_wei=int(source['amount_wei']),original_hash=body.original_hash,now=now())
            plan=plan_wallet_cancellation(snapshot,gas_price=stored['gas_price'],bump_percent=stored['bump_percent'],max_gas_wei=int(stored['max_gas_wei']),
                accept_unknown_fee=stored['accept_unknown_fee'],previous_prices=tuple(stored['previous_prices']),previous_fees_unknown=stored['previous_fees_unknown'],now=now())
            with access._lock:
                verified(token,account,wallet);same_generation(generation)
                with access.vault.guard_session_generation(generation),access.vault.session() as db:
                    return journal.claim_cancellation(db,identifier,account_id=account,wallet_address=wallet,plan=plan,plan_digest=body.plan_digest,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/observe',dependencies=[Depends(authorized)])
    def observe(account_id:UUID,identifier:UUID,body:Hash,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);identifier=str(identifier);account=str(account_id)
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,_,_=journal._owned(db,identifier,account,wallet)
                    if row.status=='SETTLED':return journal.view_cancellation(db,identifier,now=now())
                    if row.status in {'CANCELLED','WALLET_REJECTED'}:raise DexExecutorError('La revisión ya se retiró o rechazó')
                    if journal._known(db,row) is not None:
                        db.commit();return journal.settle_known(db,identifier,account_id=account,now=now())
            with factory() as rpc:evidence=collect_resolution(rpc,body.tx_hash)
            with access._lock:
                verified(token,account,wallet);same_generation(generation)
                with access.vault.guard_session_generation(generation),access.vault.session() as db:
                    if evidence is None:return {**journal.view_cancellation(db,identifier,now=now()),'observation_status':'PENDING'}
                    return journal.observe_cancellation(db,identifier,account_id=account,wallet_address=wallet,evidence=evidence,now=now())
        except ValueError as exc:raise failure(exc,token) from None

    def local(account,identifier,body,response,token,operation):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            with access._lock:
                verified(token,account,wallet)
                with access.vault.session() as db:
                    journal._owned(db,str(identifier),str(account),wallet);db.commit()
                    if operation=='hash':return journal.report_hash(db,str(identifier),account_id=str(account),tx_hash=body.tx_hash,now=now())
                    action=journal.report_rejected if operation=='rejected' else journal.cancel_review
                    return action(db,str(identifier),account_id=str(account),now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/hash',dependencies=[Depends(authorized)])
    def report_hash(account_id:UUID,identifier:UUID,body:Hash,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,identifier,body,response,token,'hash')

    @router.post('/{identifier}/reconcile',dependencies=[Depends(authorized)])
    def reconcile(account_id:UUID,identifier:UUID,body:Scope,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            with access._lock:
                verified(token,account_id,wallet)
                with access.vault.session() as db:
                    journal._owned(db,str(identifier),str(account_id),wallet);db.commit()
                    return journal.settle_known(db,str(identifier),account_id=str(account_id),now=now())
        except ValueError as exc:raise failure(exc,token) from None

    @router.post('/{identifier}/rejected',dependencies=[Depends(authorized)])
    def rejected(account_id:UUID,identifier:UUID,body:Rejected,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,identifier,body,response,token,'rejected')

    @router.post('/{identifier}/cancel',dependencies=[Depends(authorized)])
    def cancel(account_id:UUID,identifier:UUID,body:Cancel,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,identifier,body,response,token,'cancel')

    return router
