"""Local-only manual PAPER maintenance with bounded, redacted secret bodies."""
import json
from typing import Literal

from fastapi import APIRouter,BackgroundTasks,HTTPException,Request,Response
from pydantic import BaseModel,ConfigDict,Field,SecretStr,ValidationError

from app.core.manual_paper_protection import ManualPaperProtection
from app.core.storage_protection import ProtectionError
from app.security.loopback import _allowed


class ManualProtectionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    id: str=Field(min_length=36,max_length=36)
    passphrase: SecretStr=Field(min_length=12,max_length=1024)
    repeated: SecretStr=Field(min_length=12,max_length=1024)
    confirmation: Literal['CIFRAR PAPER']


async def secret_body(request: Request,model: type[BaseModel]) -> BaseModel:
    raw=bytearray()
    try:
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>16384:raise HTTPException(413,'Solicitud PAPER demasiado grande')
        return model.model_validate(json.loads(raw))
    except (ValidationError,ValueError,TypeError,UnicodeError):
        raise HTTPException(422,'Revisa las contraseñas y la confirmación PAPER') from None
    finally:raw.clear()


def create_manual_protection_router(control: ManualPaperProtection) -> APIRouter:
    router=APIRouter(prefix='/paper/legacy/protection',tags=['local-settings'])
    def local(request: Request,response: Response) -> None:
        if not _allowed(request.scope):raise HTTPException(403,'Acceso sólo local')
        response.headers['Cache-Control']='no-store'

    @router.get('')
    def status(request: Request,response: Response):
        local(request,response);return control.status()

    @router.get('/jobs/{identifier}')
    def job(identifier: str,request: Request,response: Response):
        local(request,response)
        try:return control.job(identifier)
        except ProtectionError:raise HTTPException(404,'Trabajo PAPER no disponible') from None

    @router.post('/jobs',status_code=202)
    async def submit(request: Request,response: Response,tasks: BackgroundTasks):
        local(request,response)
        body=await secret_body(request,ManualProtectionRequest)
        try:result=control.queue(body.id,body.passphrase.get_secret_value(),body.repeated.get_secret_value())
        except ProtectionError as exc:raise HTTPException(409,str(exc)) from None
        except Exception:raise HTTPException(409,'No se pudo preparar la protección PAPER') from None
        tasks.add_task(control.request_pause)
        return result
    return router
