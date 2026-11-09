"""Owner-proven recovery of a mined call. No secret, signer or transaction send.

RPC runs outside access/SQL locks. Both sides authenticate scope and generation;
the sealed journal rechecks source/quiescence and the exact reviewed outcome.
"""
import hashlib,hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_observation import source_reference, verify_external_call
from app.live_execution.dex_nonce_search import find_finalized_nonce, read_granted_operator
from app.live_execution.dex_operator_transport import BnbNonceReadClient, BnbOperatorReadClient
from app.portfolio import dex_call_resolution as resolution
from app.portfolio import dex_external_observation as external_journal
from app.portfolio.dex_deployments import _identity
from app.portfolio.domain import PortfolioError
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.wallet_proof import canonical_wallet_address

Kind=Literal['WALLET_SWAP','OPERATOR']


class Discover(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')
    accept_changes:bool=False


class Review(Discover):
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class Confirm(Review):
    preview_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    source_state_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['RESOLVER_LLAMADA_DEX']


class Original(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


def create_call_resolution_router(access,*,authorized,authorized_passive,rpc_factory=None):
    router=APIRouter(prefix='/accounts/{account_id}/dex-call-resolutions/{kind}/{identifier}',tags=['dex-call-resolution'])
    factory=rpc_factory or BnbOperatorReadClient;clock=access._dex_bots.now
    finder=rpc_factory or BnbNonceReadClient

    def verified(token,account,wallet,*,passive=False,generation=None):
        access.require(token,touch=not passive)
        if generation is not None and access.vault.session_generation!=generation:
            raise DexExecutorError('La sesión cambió durante la lectura DEX')
        digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
        if (str(account),'0x38',digest) not in access._wallet_verified:
            raise DexExecutorError('Verifica el control de esta wallet en BNB Chain')

    def existing(db,kind,identifier,tx_hash=None):
        record=resolution.resolution_record(db,resolution.identifier(kind,identifier))
        if record and tx_hash is not None and record[0].tx_digest!=_identity(db,'tx',tx_hash):
            raise DexExecutorError('Esta llamada se resolvió con otra transacción')
        return resolution.public_record(record) if record else None

    def idle(db,kind,row):
        if kind=='OPERATOR':
            access._dex_bots.require_idle()
            resolution._quiescence(db,row,clock(),verify_runs=True)

    def run(account,kind,identifier,body,response,token,*,commit,discover=False,original=False):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);identifier=str(identifier)
            tx_hash=None if discover else body.tx_hash
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,_,_,source=resolution._context(db,kind,identifier,str(account),wallet)
                    prior=existing(db,kind,identifier,tx_hash)
                    if prior:return prior
                    if discover and external_journal.status(db,kind,identifier) is not None:
                        return {'external_review_required':True}
                    if row.status not in resolution.PENDING[kind]:raise DexExecutorError('La llamada no necesita resolver otro recibo')
                    if original and (source.original_tx_ref is None
                            or source.original_tx_ref != resolution._reference(db,kind)('tx',tx_hash)):
                        raise DexExecutorError('Indica el hash registrado de la acción DEX')
                    idle(db,kind,row);pin=(row.payload_digest,row.state_digest)
            if discover:
                sender=wallet
                search={}
                if kind=='OPERATOR':
                    grant_anchor=source.grant['anchor']
                    with factory() as rpc:
                        sender=read_granted_operator(rpc,wallet,int(source.terms['creation_nonce']),
                            int(grant_anchor['block_number']),grant_anchor['block_hash'])
                    with access._lock:
                        verified(token,account,wallet,generation=generation)
                        with access.vault.session() as db:
                            if _identity(db,'wallet',sender)!=source.signer_digest:
                                raise DexExecutorError('La operadora histórica no coincide con la llamada DEX')
                    anchor=source.terms['anchor']
                    search={'anchor_height':int(anchor['block_number']),'anchor_hash':anchor['block_hash']}
                with finder() as rpc:
                    match=find_finalized_nonce(rpc,sender,source.nonce,**search)
                with access._lock:
                    verified(token,account,wallet,generation=generation)
                if match is None:
                    raise DexExecutorError('El nonce todavía no tiene una transacción finalizada')
                tx_hash=match.tx_hash
            with factory() as rpc:evidence=collect_call_resolution(rpc,source,tx_hash)
            if discover and evidence is not None:
                match.check_receipt(evidence)
            with access._lock:
                verified(token,account,wallet,generation=generation)
                with access.vault.session() as db:
                    row,_,_,_=resolution._context(db,kind,identifier,str(account),wallet)
                    prior=existing(db,kind,identifier,tx_hash)
                    if prior:return prior
                    if (row.payload_digest,row.state_digest)!=pin:raise DexExecutorError('La llamada cambió; vuelve a revisar')
                    if discover and external_journal.status(db,kind,identifier) is not None:
                        return {'external_review_required':True}
                    idle(db,kind,row)
                    if evidence is None:raise DexExecutorError('Todavía no hay un recibo finalizado para ese hash')
                    scope=dict(account_id=str(account),wallet_address=wallet,evidence=evidence,
                        accept_changes=False if original else body.accept_changes,now=clock())
                    if original:
                        preview=resolution.preview_resolution(db,kind,identifier,**scope)
                        proof=preview['proof']
                        if proof['matches_original'] is not True or not proof['matches_call'] or not preview['available']:
                            raise DexExecutorError('La llamada o el gas difieren del plan; abre Revisar resultado')
                        return resolution.confirm_resolution(db,kind,identifier,preview_digest=preview['preview_digest'],
                            source_state_digest=preview['source_state_digest'],**scope)
                    if not commit:
                        try:
                            preview=resolution.preview_resolution(db,kind,identifier,**scope)
                        except DexExecutorError as original_error:
                            if not discover or resolution._version(db)<76:
                                raise
                            try:
                                verify_external_call(source,evidence,reference=source_reference(kind,_existing_salt(db)),now=scope['now'])
                            except DexExecutorError:
                                raise original_error from None
                            return {'external_review_required':True,'discovered_tx_hash':tx_hash}
                        return {**preview,'discovered_tx_hash':tx_hash} if discover else preview
                    return resolution.confirm_resolution(db,kind,identifier,preview_digest=body.preview_digest,
                        source_state_digest=body.source_state_digest,**scope)
        except (PortfolioError,ValueError) as exc:
            with access._lock:access.require(token,touch=False)
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.get('/resolution',dependencies=[Depends(authorized_passive)])
    def status(account_id:UUID,kind:Kind,identifier:UUID,response:Response,
               wallet:str=Header(alias='X-Orbis-Wallet',pattern=r'^0x[0-9a-fA-F]{40}$'),
               token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        # The transient wallet stays out of URLs and access logs.
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(wallet)
            with access._lock:
                verified(token,account_id,wallet,passive=True)
                with access.vault.session() as db:
                    row,_,_,_=resolution._context(db,kind,str(identifier),str(account_id),wallet)
                    return existing(db,kind,str(identifier)) or {'id':resolution.identifier(kind,str(identifier)),
                        'source_kind':kind,'source_id':row.id,'resolved':False,'source_status':row.status,
                        'needs_resolution':row.status in resolution.PENDING[kind],'automatic_execution':False}
        except (PortfolioError,ValueError) as exc:
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.post('/resolution-preview',dependencies=[Depends(authorized)])
    def preview(account_id:UUID,kind:Kind,identifier:UUID,body:Review,response:Response,
                token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,commit=False)

    @router.post('/discover',dependencies=[Depends(authorized)])
    def discover(account_id:UUID,kind:Kind,identifier:UUID,body:Discover,response:Response,
                 token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,commit=False,discover=True)

    @router.post('/resolve',dependencies=[Depends(authorized)])
    def confirm(account_id:UUID,kind:Kind,identifier:UUID,body:Confirm,response:Response,
                token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,commit=True)

    @router.post('/check-original',dependencies=[Depends(authorized)])
    def check_original(account_id:UUID,kind:Literal['WALLET_SWAP'],identifier:UUID,body:Original,response:Response,
                       token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,commit=True,original=True)

    return router
