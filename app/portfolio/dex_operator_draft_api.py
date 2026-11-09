"""Owner-proven generated operators. Only an encrypted key file may be exported."""
from datetime import datetime,timezone
import hashlib,hmac,json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Body,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field,SecretStr

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_permission import MAX_PERMISSION_SECONDS
from app.portfolio.domain import PortfolioError
from app.portfolio.models import RealAccount
from app.portfolio.wallet_proof import canonical_wallet_address


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(min_length=42,max_length=42)


class Create(Scope):
    request_id:UUID=Field(strict=False)
    ttl_seconds:int=Field(strict=True,ge=1,le=MAX_PERMISSION_SECONDS,default=900)


class Export(Scope):
    draft_id:UUID=Field(strict=False)
    password:SecretStr=Field(min_length=12,max_length=256)


class Verify(Export):
    backup_json:str=Field(min_length=1,max_length=8192)


class Restore(Create):
    password:SecretStr=Field(min_length=12,max_length=256)
    backup_json:str=Field(min_length=1,max_length=8192)


class Discard(Scope):
    draft_id:UUID=Field(strict=False)
    confirmation:Literal['RETIRAR_PREPARACION_OPERADORA_DEX']


def parse(model,raw):
    try:return model.model_validate(raw)
    except (ValueError,TypeError):
        # Never echo password/backup input in Pydantic validation details.
        raise HTTPException(422,'Formulario de operadora inválido',headers={'Cache-Control':'no-store'}) from None


def backup_body(text):
    try:return json.loads(text)
    except (ValueError,TypeError,RecursionError):raise DexExecutorError('Archivo cifrado de operadora no válido') from None


def create_dex_operator_draft_router(access,*,authorized) -> APIRouter:
    router=APIRouter(prefix='/accounts/{account_id}/dex-operator',tags=['dex-operator-preparation'])
    manager=access._dex_operator_drafts

    def verified(token,account,wallet):
        access.require(token)
        digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
        if (str(account),'0x38',digest) not in access._wallet_verified:
            raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión REAL')
        with access.vault.session() as db:
            if db.get(RealAccount,str(account)) is None:raise DexExecutorError('Cuenta REAL no encontrada')

    def run(account,body,response,token,action):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address)
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
            # Scrypt never holds the REAL access or vault lock.
            result=action(wallet,datetime.now(timezone.utc))
            with access._lock:
                verified(token,account,wallet)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la preparación')
            return result
        except (PortfolioError,ValueError) as exc:
            # Revocation takes precedence over a cipher/validation error.
            with access._lock:access.require(token,touch=False)
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.post('/status',dependencies=[Depends(authorized)])
    def status(account_id:UUID,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        data=parse(Scope,body)
        return run(account_id,data,response,token,lambda wallet,now:manager.status(account_id=str(account_id),wallet_address=wallet,now=now))

    @router.post('/draft',dependencies=[Depends(authorized)])
    def create(account_id:UUID,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        data=parse(Create,body)
        return run(account_id,data,response,token,lambda wallet,now:manager.create(identifier=str(data.request_id),account_id=str(account_id),wallet_address=wallet,ttl_seconds=data.ttl_seconds,now=now))

    @router.post('/backup',dependencies=[Depends(authorized)])
    def export(account_id:UUID,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        data=parse(Export,body)
        return run(account_id,data,response,token,lambda wallet,now:manager.export_backup(identifier=str(data.draft_id),account_id=str(account_id),wallet_address=wallet,password=data.password.get_secret_value(),now=now))

    @router.post('/verify-backup',dependencies=[Depends(authorized)])
    def verify(account_id:UUID,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        data=parse(Verify,body)
        return run(account_id,data,response,token,lambda wallet,now:manager.verify_backup(identifier=str(data.draft_id),account_id=str(account_id),wallet_address=wallet,password=data.password.get_secret_value(),backup=backup_body(data.backup_json),now=now))

    @router.post('/restore',dependencies=[Depends(authorized)])
    def restore(account_id:UUID,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        data=parse(Restore,body)
        return run(account_id,data,response,token,lambda wallet,now:manager.restore_backup(identifier=str(data.request_id),account_id=str(account_id),wallet_address=wallet,password=data.password.get_secret_value(),backup=backup_body(data.backup_json),ttl_seconds=data.ttl_seconds,now=now))

    @router.post('/discard',dependencies=[Depends(authorized)])
    def discard(account_id:UUID,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        data=parse(Discard,body)
        return run(account_id,data,response,token,lambda wallet,now:manager.discard(identifier=str(data.draft_id),account_id=str(account_id),wallet_address=wallet,now=now))

    return router
