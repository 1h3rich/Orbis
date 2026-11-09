"""Owner-proven gas funding: prepare a wallet call, never send from the API."""
from datetime import datetime,timezone
import hashlib,hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import Field

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_funding import read_funding_state,collect_funding_receipt
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.portfolio import dex_gas_funding as funding,dex_bot_bindings as bindings
from app.portfolio.dex_deployment_api import Body
from app.portfolio.dex_deployments import deployment_record
from app.portfolio.domain import PortfolioError
from app.portfolio.models import RealAccount
from app.portfolio.wallet_proof import canonical_wallet_address


class Scope(Body):
    address:str=Field(min_length=42,max_length=42,pattern='^0x[0-9a-fA-F]{40}$')


class Review(Scope):
    binding_id:UUID=Field(strict=False)
    draft_id:UUID=Field(strict=False)


class Prepare(Review):
    request_id:UUID=Field(strict=False)
    amount_bnb:str=Field(min_length=1,max_length=100)
    gas:str=Field(min_length=1,max_length=10,pattern='^[0-9]+$')
    gas_price:str=Field(min_length=1,max_length=20,pattern='^[0-9]+$')
    max_gas_bnb:str=Field(min_length=1,max_length=100)


class Claim(Scope):
    plan_digest:str=Field(pattern='^[0-9a-f]{64}$')
    confirmation:Literal['FINANCIAR_GAS_OPERADORA_DEX']


class Hash(Scope):
    tx_hash:str=Field(pattern='^0x[0-9a-fA-F]{64}$')


class Observation(Scope):
    tx_hash:str|None=Field(default=None,pattern='^0x[0-9a-fA-F]{64}$')


class Rejected(Scope):
    confirmation:Literal['WALLET_RECHAZADA_4001']


def create_dex_gas_funding_router(access,*,authorized,authorized_passive,rpc_factory=None)->APIRouter:
    router=APIRouter(prefix='/accounts/{account_id}/dex-gas-funding',tags=['dex-gas-funding'])
    factory=rpc_factory or BnbOperatorReadClient
    manager=access._dex_operator_drafts

    def verified(token,account,wallet=None,*,passive=False):
        access.require(token,touch=not passive)
        if wallet is not None:
            digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if (str(account),'0x38',digest) not in access._wallet_verified:
                raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión REAL')

    def failure(exc,token):
        with access._lock:access.require(token,touch=False)
        return HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})

    def context(db,account,wallet,*,binding_id=None,identifier=None):
        if identifier is not None:
            row,terms,state=funding.funding_record(db,str(identifier))
            if row.account_id!=str(account) or row.wallet_digest!=funding._identity(db,'wallet',wallet):
                raise DexExecutorError('Envío BNB de otra cuenta o wallet')
            binding_id=row.binding_id
        else:row=terms=state=None
        parent,binding,_=bindings.binding_record(db,str(binding_id))
        if parent.account_id!=str(account) or binding['wallet_digest']!=funding._identity(db,'wallet',wallet):
            raise DexExecutorError('Vínculo DEX de otra cuenta o wallet')
        deployment=deployment_record(db,parent.deployment_id)[1]
        return int(deployment['nonce']),deployment['fee'],row,terms,state

    def local(account,response,token,action,*,wallet=None,passive=False):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account,wallet,passive=passive)
                with access.vault.session() as db:
                    if db.get(RealAccount,str(account)) is None:raise DexExecutorError('Cuenta REAL no encontrada')
                    return action(db,datetime.now(timezone.utc))
        except (PortfolioError,ValueError) as exc:raise failure(exc,token) from None

    def observe(account,identifier,body,response,token,operation):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    nonce,fee,row,terms,state=context(db,account,wallet,
                        binding_id=body.binding_id if operation in {'prepare','state'} else None,identifier=identifier)
                    if operation in {'prepare','state'}:
                        manager.require_verified(identifier=str(body.draft_id),account_id=str(account),wallet_address=wallet,now=datetime.now(timezone.utc))
                    if operation=='claim' and (terms is None or row.payload_digest!=body.plan_digest):
                        raise DexExecutorError('La revisión BNB cambió')
                    if operation=='claim' and row.status!='PREPARED':
                        return {**funding._public(row,terms,state,datetime.now(timezone.utc)),'claim_granted':False}
                    if operation=='observe':
                        if body.tx_hash and state['tx_ref'] is not None and state['tx_ref']!=funding._identity(db,'tx',body.tx_hash):
                            raise DexExecutorError('Conserva el hash BNB original')
                        if row.status in {'FINALIZED','REVERTED'}:return funding._public(row,terms,state,datetime.now(timezone.utc))
                        if row.status not in {'WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED'}:
                            raise DexExecutorError('No hay un envío BNB solicitado que comprobar')
                        if body.tx_hash is None and state['nonce_observation'] is not None:
                            return funding._public(row,terms,state,datetime.now(timezone.utc))
            # Public reads hold neither the REAL access lock nor a DB session.
            with factory() as rpc:
                evidence=(collect_funding_receipt(rpc,body.tx_hash) if operation=='observe' and body.tx_hash
                    else read_funding_state(rpc,wallet,nonce,fee,now=datetime.now(timezone.utc)))
            with access._lock:
                verified(token,account,wallet)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la lectura BNB')
                with access.vault.session() as db:
                    now=datetime.now(timezone.utc)
                    _,_,row,terms,state=context(db,account,wallet,
                        binding_id=body.binding_id if operation in {'prepare','state'} else None,identifier=identifier)
                    if operation=='prepare':
                        return funding.prepare_funding(db,identifier=str(body.request_id),account_id=str(account),binding_id=str(body.binding_id),
                            draft_id=str(body.draft_id),wallet_address=wallet,drafts=manager,amount_bnb=body.amount_bnb,
                            gas=body.gas,gas_price=body.gas_price,max_gas_bnb=body.max_gas_bnb,snapshot=evidence,now=now)
                    if operation=='state':
                        _,_,budget,recipient=funding._live(db,binding_id=str(body.binding_id),account_id=str(account),wallet=wallet,
                            drafts=manager,draft_id=str(body.draft_id),snapshot=evidence,now=now)
                        return {'binding_id':str(body.binding_id),'draft_id':str(body.draft_id),'operator_address':recipient,
                            'operator_balance_wei':str(evidence.operator_balance),'owner_balance_wei':str(evidence.executor.native_balance),
                            'maximum_funding_wei':str(max(0,int(budget['remaining_gas_wei'])-evidence.operator_balance)),
                            'remaining_gas_wei':budget['remaining_gas_wei'],'automatic_execution':False}
                    if operation=='claim':
                        # End the metadata read before the journal's BEGIN IMMEDIATE.
                        db.commit()
                        return funding.claim_funding(db,str(identifier),account_id=str(account),wallet_address=wallet,
                            drafts=manager,plan_digest=body.plan_digest,snapshot=evidence,now=now)
                    if row.status in {'FINALIZED','REVERTED'}:return funding._public(row,terms,state,now)
                    db.commit()
                    if evidence is None:return {**funding._public(row,terms,state,now),'observation_status':'PENDING'}
                    if body.tx_hash:return funding.check_funding(db,str(identifier),account_id=str(account),evidence=evidence,now=now)
                    return funding.recover_funding(db,str(identifier),account_id=str(account),wallet_address=wallet,snapshot=evidence,now=now)
        except (PortfolioError,ValueError) as exc:raise failure(exc,token) from None

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(account_id:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,response,token,lambda db,now:funding.list_funding(db,str(account_id),now=now),passive=True)

    @router.post('/state',dependencies=[Depends(authorized)])
    def state(account_id:UUID,body:Review,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return observe(account_id,None,body,response,token,'state')

    @router.post('',dependencies=[Depends(authorized)])
    def prepare(account_id:UUID,body:Prepare,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return observe(account_id,None,body,response,token,'prepare')

    @router.post('/{identifier}/claim',dependencies=[Depends(authorized)])
    def claim(account_id:UUID,identifier:UUID,body:Claim,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return observe(account_id,identifier,body,response,token,'claim')

    @router.post('/{identifier}/observe',dependencies=[Depends(authorized)])
    def check(account_id:UUID,identifier:UUID,body:Observation,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return observe(account_id,identifier,body,response,token,'observe')

    def report(account,identifier,body,response,token,*,rejected=False):
        wallet=canonical_wallet_address(body.address)
        def action(db,now):
            context(db,account,wallet,identifier=identifier);db.commit()
            return funding.report_funding(db,str(identifier),account_id=str(account),now=now,
                tx_hash=None if rejected else body.tx_hash,rejected=rejected)
        return local(account,response,token,action,wallet=wallet)

    @router.post('/{identifier}/hash',dependencies=[Depends(authorized)])
    def hashed(account_id:UUID,identifier:UUID,body:Hash,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return report(account_id,identifier,body,response,token)

    @router.post('/{identifier}/rejected',dependencies=[Depends(authorized)])
    def rejected(account_id:UUID,identifier:UUID,body:Rejected,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return report(account_id,identifier,body,response,token,rejected=True)

    return router
