"""Protected backend-RPC replacement recovery. Never asks the wallet to spend."""
import hashlib,hmac
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_deployment_resolution import collect_resolution
from app.live_execution.dex_nonce_search import find_finalized_nonce
from app.live_execution.dex_operator_transport import BnbNonceReadClient, BnbOperatorReadClient
from app.portfolio import dex_deployment_resolution as resolution,dex_deployments as deployments
from app.portfolio.domain import PortfolioError
from app.portfolio.wallet_proof import canonical_wallet_address


class Discover(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(min_length=42,max_length=42)
    accept_fee_overrun:bool=False


class Review(Discover):
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class Confirm(Review):
    preview_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    source_state_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['RESOLVER_REEMPLAZO_DEX']


def create_deployment_resolution_router(access,*,authorized,rpc_factory=None):
    router=APIRouter(prefix='/accounts/{account_id}/dex-deployments',tags=['dex-deployment-resolution'])
    factory=rpc_factory or BnbOperatorReadClient;clock=access._dex_bots.now
    finder=rpc_factory or BnbNonceReadClient
    def verified(token,account,wallet,generation=None):
        access.require(token)
        if generation is not None and access.vault.session_generation!=generation:
            raise DexExecutorError('La sesión cambió durante la lectura')
        digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
        if (str(account),'0x38',digest) not in access._wallet_verified:raise DexExecutorError('Verifica el control de esta wallet en BNB Chain')

    def run(account,identifier,body,response,token,*,commit,discover=False):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            tx_hash=None if discover else body.tx_hash
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,terms,_=resolution._context(db,str(identifier),str(account),wallet)
                    existing=resolution.resolution_record(db,str(identifier))
                    if existing:
                        if tx_hash is not None and existing[1]['proof']['tx_ref']!=deployments._identity(db,'tx',tx_hash):raise DexExecutorError('La solicitud se resolvió con otra transacción')
                        return resolution.public_record(existing)
                    if row.status not in resolution.PENDING:raise DexExecutorError('La solicitud no necesita resolver un reemplazo')
                    pin=row.state_digest;nonce,fee=int(terms['nonce']),terms['fee']
            # No private/session/DB lock crosses the network. Client cannot
            # provide RPC URLs, arbitrary evidence, calldata or signing keys.
            if discover:
                with finder() as rpc:
                    match=find_finalized_nonce(rpc,wallet,nonce)
                with access._lock:
                    verified(token,account,wallet,generation)
                if match is None:
                    raise DexExecutorError('El nonce todavía no tiene una transacción finalizada')
                tx_hash=match.tx_hash
            with factory() as rpc:evidence=collect_resolution(rpc,wallet,nonce,fee,tx_hash)
            if discover and evidence is not None:
                match.check_receipt(evidence)
            with access._lock:
                verified(token,account,wallet,generation)
                with access.vault.session() as db:
                    row,_,_=resolution._context(db,str(identifier),str(account),wallet)
                    existing=resolution.resolution_record(db,str(identifier))
                    if existing:
                        if existing[1]['proof']['tx_ref']!=deployments._identity(db,'tx',tx_hash):raise DexExecutorError('La solicitud se resolvió con otra transacción')
                        return resolution.public_record(existing)
                    if row.state_digest!=pin:raise DexExecutorError('La solicitud cambió; vuelve a revisar')
                    if evidence is None:raise DexExecutorError('Todavía no hay un recibo finalizado para ese hash')
                    common=dict(account_id=str(account),wallet_address=wallet,evidence=evidence,accept_fee_overrun=body.accept_fee_overrun,now=clock())
                    if not commit:
                        preview=resolution.preview_resolution(db,str(identifier),**common)
                        return {**preview,'discovered_tx_hash':tx_hash} if discover else preview
                    return resolution.confirm_resolution(db,str(identifier),preview_digest=body.preview_digest,source_state_digest=body.source_state_digest,**common)
        except (PortfolioError,ValueError) as exc:raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.post('/{identifier}/resolution-preview',dependencies=[Depends(authorized)])
    def preview(account_id:UUID,identifier:UUID,body:Review,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,identifier,body,response,token,commit=False)

    @router.post('/{identifier}/discover',dependencies=[Depends(authorized)])
    def discover(account_id:UUID,identifier:UUID,body:Discover,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,identifier,body,response,token,commit=False,discover=True)

    @router.post('/{identifier}/resolve',dependencies=[Depends(authorized)])
    def confirm(account_id:UUID,identifier:UUID,body:Confirm,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,identifier,body,response,token,commit=True)
    return router
