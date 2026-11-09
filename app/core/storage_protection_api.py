"""Small local maintenance API; secrets are manually validated/redacted."""
from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.responses import JSONResponse

from app.core.storage_protection import StorageProtection, ProtectionError
from app.security.loopback import _allowed


class ProtectionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    id: str = Field(min_length=36,max_length=36)
    targets: list[Literal['LAB','DCA','SPOT','GRVT']] = Field(min_length=1,max_length=4)
    passphrase: SecretStr
    repeated: SecretStr
    confirmation: Literal['PROTEGER DATOS']


def create_storage_protection_router(control: StorageProtection) -> APIRouter:
    router=APIRouter(prefix='/system/storage-protection',tags=['local-settings'])
    def local(request: Request) -> None:
        if not _allowed(request.scope):raise HTTPException(403,'Acceso sólo local')

    @router.get('')
    def status(request: Request,response: Response) -> dict:
        local(request);response.headers['Cache-Control']='no-store'
        return control.status()

    @router.get('/jobs/{identifier}')
    def job(identifier: str,request: Request,response: Response) -> dict:
        local(request);response.headers['Cache-Control']='no-store'
        try:return control.job(identifier)
        except ProtectionError:raise HTTPException(404,'Trabajo no disponible') from None

    @router.post('/jobs',status_code=202)
    async def submit(request: Request,response: Response,tasks: BackgroundTasks) -> dict:
        local(request);response.headers['Cache-Control']='no-store'
        # FastAPI's default ValidationError JSON can echo an entire input body
        # when a field is missing. Do not put a password-bearing DTO on that path.
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>16384:raise HTTPException(413,'Solicitud demasiado grande')
        try:
            value=ProtectionRequest.model_validate(json.loads(raw))
        except (ValidationError,ValueError,TypeError,UnicodeError):
            raise HTTPException(422,'Revisa los almacenes, las contraseñas y la confirmación') from None
        finally:
            raw.clear()
        try:
            result=control.queue(value.id,value.targets,value.passphrase.get_secret_value(),value.repeated.get_secret_value())
        except ProtectionError as exc:
            raise HTTPException(409,str(exc)) from None
        except Exception:
            raise HTTPException(409,'No se pudo preparar la protección') from None
        # Uvicorn stops only after this response has been transmitted.
        tasks.add_task(control.request_pause)
        return result
    return router


class StorageProtectionMiddleware:
    """Freeze new data requests while accepted work drains the server."""
    def __init__(self,app,control: StorageProtection):
        self.app=app;self.control=control

    async def __call__(self,scope,receive,send):
        if scope['type'] in ('http','websocket') and self.control.frozen and _allowed(scope):
            path=scope.get('path','')
            allowed=(scope['type']=='http' and scope.get('method')=='GET'
                and (path=='/system/storage-protection' or path.startswith('/system/storage-protection/jobs/')
                    or path=='/paper/legacy/protection' or path.startswith('/paper/legacy/protection/jobs/')
                    or path.startswith('/ui/') or path=='/ui'))
            if not allowed:
                if scope['type']=='websocket':
                    await send({'type':'websocket.close','code':1013});return
                response=JSONResponse({'detail':'Protección de datos en curso'},status_code=503,
                    headers={'Cache-Control':'no-store','Retry-After':'2'})
                await response(scope,receive,send);return
        await self.app(scope,receive,send)
