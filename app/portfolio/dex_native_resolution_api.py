"""Owner-proven native replacement review. Reads never hold private locks."""
import hashlib,hmac
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_native_resolution import collect_resolution
from app.live_execution.dex_nonce_search import find_finalized_nonce, read_granted_operator
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.portfolio import dex_native_resolution as resolution
from app.portfolio.dex_deployments import _identity, deployment_record
from app.portfolio.dex_operator_journal import grant_record
from app.portfolio.domain import PortfolioError
from app.portfolio.wallet_proof import canonical_wallet_address

Kind=Literal['GAS_FUNDING','GAS_RETURN']


class Discover(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')
    accept_changes:bool=False


class Review(Discover):
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class Confirm(Review):
    preview_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    source_state_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['RESOLVER_REEMPLAZO_BNB']


def create_native_resolution_router(access,*,authorized,rpc_factory=None):
    router=APIRouter(prefix='/accounts/{account_id}/dex-native-resolutions/{kind}/{identifier}',tags=['dex-native-resolution'])
    factory=rpc_factory or BnbOperatorReadClient;clock=access._dex_bots.now

    def verified(token,account,wallet,generation=None):
        access.require(token)
        if generation is not None and access.vault.session_generation!=generation:
            raise DexExecutorError('La sesión cambió durante la lectura BNB')
        digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
        if (str(account),'0x38',digest) not in access._wallet_verified:raise DexExecutorError('Verifica el control de esta wallet en BNB Chain')

    def existing(db,kind,identifier,tx_hash):
        record=resolution.resolution_record(db,resolution.identifier(kind,identifier))
        if record and tx_hash is not None and record[1]['proof']['tx_ref']!=_identity(db,'tx',tx_hash):raise DexExecutorError('La solicitud BNB se resolvió con otra transacción')
        return resolution.public_record(record) if record else None

    def run(account,kind,identifier,body,response,token,*,commit,discover=False):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);identifier=str(identifier)
            tx_hash = None if discover else body.tx_hash
            grant_anchor = None
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    row,terms,_=resolution._context(db,kind,identifier,str(account),wallet)
                    prior=existing(db,kind,identifier,tx_hash)
                    if prior:return prior
                    if row.status not in resolution.PENDING[kind]:raise DexExecutorError('Esta solicitud BNB no necesita resolver un reemplazo')
                    pin=row.state_digest
                    if discover and kind == 'GAS_RETURN':
                        parent,grant,_ = grant_record(db,terms['grant_id'])
                        _,deployment,_ = deployment_record(db,parent.deployment_id)
                        anchor = grant['anchor']
                        grant_anchor = (int(deployment['nonce']),int(anchor['block_number']),anchor['block_hash'])
            if discover:
                sender = wallet
                if grant_anchor is not None:
                    with factory() as rpc:
                        sender = read_granted_operator(rpc,wallet,*grant_anchor)
                    with access._lock:
                        verified(token,account,wallet,generation)
                        with access.vault.session() as db:
                            if _identity(db,'wallet',sender) != terms['operator_digest']:
                                raise DexExecutorError('La operadora histórica no coincide con la devolución')
                anchor = terms['anchor']
                with factory() as rpc:
                    match = find_finalized_nonce(rpc,sender,int(terms['nonce']),
                        anchor_height=int(anchor['block_number']),anchor_hash=anchor['block_hash'])
                with access._lock:
                    verified(token,account,wallet,generation)
                if match is None:
                    raise DexExecutorError('El nonce todavía no tiene una transacción finalizada')
                tx_hash = match.tx_hash
            with factory() as rpc:evidence=collect_resolution(rpc,tx_hash)
            if discover and evidence is not None:
                match.check_receipt(evidence)
            with access._lock:
                verified(token,account,wallet,generation)
                with access.vault.session() as db:
                    row,_,_=resolution._context(db,kind,identifier,str(account),wallet)
                    prior=existing(db,kind,identifier,tx_hash)
                    if prior:return prior
                    if row.state_digest!=pin:raise DexExecutorError('La solicitud BNB cambió; vuelve a revisar')
                    if evidence is None:raise DexExecutorError('Todavía no hay recibo BNB finalizado para ese hash')
                    arguments=dict(account_id=str(account),wallet_address=wallet,evidence=evidence,accept_changes=body.accept_changes,now=clock())
                    if not commit:
                        preview = resolution.preview_resolution(db,kind,identifier,**arguments)
                        return {**preview,'discovered_tx_hash':tx_hash} if discover else preview
                    return resolution.confirm_resolution(db,kind,identifier,preview_digest=body.preview_digest,source_state_digest=body.source_state_digest,**arguments)
        except (PortfolioError,ValueError) as exc:
            with access._lock:access.require(token,touch=False)
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.post('/resolution-preview',dependencies=[Depends(authorized)])
    def preview(account_id:UUID,kind:Kind,identifier:UUID,body:Review,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,commit=False)

    @router.post('/discover',dependencies=[Depends(authorized)])
    def discover(account_id:UUID,kind:Kind,identifier:UUID,body:Discover,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,commit=False,discover=True)

    @router.post('/resolve',dependencies=[Depends(authorized)])
    def confirm(account_id:UUID,kind:Kind,identifier:UUID,body:Confirm,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,commit=True)
    return router
