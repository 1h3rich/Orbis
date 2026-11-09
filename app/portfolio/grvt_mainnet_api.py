"""Operativa MAINNET local con revisión ligada a sesión y contratos persistentes."""

from decimal import Decimal
from typing import Annotated,Literal

from fastapi import APIRouter,Body,Depends,Header,HTTPException,Query,Response
from pydantic import BaseModel,ConfigDict,Field,SecretStr,StrictInt,StrictStr

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, MainnetBracketDraft
from app.live_execution.grvt_mainnet_session import GrvtMainnetSession
from app.portfolio.grvt_bracket_api import _body,_wire,Amount
from app.portfolio.vault import VaultError


Sub=Annotated[StrictStr,Field(pattern=r'^[1-9][0-9]{0,19}$')]
Revision=Annotated[list[Annotated[StrictInt,Field(ge=0,le=9007199254740991)]],Field(min_length=2,max_length=2)]


class Action(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    sub_account_id:Sub
    permission_revision:Revision


class Connect(Action):
    api_key:SecretStr=Field(min_length=8,max_length=256)
    dedicated_private_key:SecretStr=Field(min_length=64,max_length=66)
    expected_signer:Annotated[StrictStr,Field(pattern=r'^0x[0-9a-fA-F]{40}$')]
    confirmation:Literal['CONECTAR_GRVT_MAINNET']


class ConnectView(Action):
    api_key:SecretStr=Field(min_length=8,max_length=256)


class Preview(Action):
    instrument:Annotated[StrictStr,Field(pattern=r'^[A-Z0-9]+_USDT_Perp$')]
    direction:Literal['LONG','SHORT']
    size:Amount
    entry_price:Amount
    stop_loss:Amount
    take_profit:Amount
    entry_cap:Amount
    exit_cap:Amount
    exit_seconds:Annotated[StrictInt,Field(ge=120,le=86400)]=3600
    max_spread_bps:Annotated[StrictInt,Field(ge=0,le=100)]=25
    max_slippage_bps:Annotated[StrictInt,Field(ge=0,le=100)]=25


class Send(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    review:dict
    review_mac:Annotated[StrictStr,Field(pattern=r'^[a-f0-9]{64}$')]
    permission_revision:Revision
    confirmation:Literal['ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET']


class Stop(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    sub_account_id:Sub
    confirmation:Literal['DETENER_GRVT_MAINNET']


class Disconnect(Action):
    confirmation:Literal['DESCONECTAR_GRVT_MAINNET']


class GrvtFillExport(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    sub_account_id: Sub
    revision: Annotated[StrictStr,Field(pattern=r'^[a-f0-9]{64}$')]
    confirmation: Literal['EXPORTAR_JSONL_GRVT_REAL']


def export_fill_archive(access, token, response: Response, identifier: str,
                        body: GrvtFillExport, *, limit: bool) -> dict:
    from app.live_execution.grvt_mainnet_fill_export import export_grvt_fills

    no_cache = {'Cache-Control':'no-store'}
    response.headers.update(no_cache)
    access.require(token,touch=False)
    generation = access._vault_generation
    try:
        with access.vault.try_guard_session_generation(generation) as available:
            if not available:
                raise HTTPException(503,'Archivo GRVT en uso · vuelve a intentarlo',headers=no_cache)
            with access.vault.session() as db:
                db.connection().exec_driver_sql('BEGIN')
                result = export_grvt_fills(db,identifier,sub_account_id=body.sub_account_id,
                                           revision=body.revision,limit=limit)
    except GrvtMainnetError as exc:
        raise HTTPException(409,str(exc),headers=no_cache) from None
    except (KeyError,ValueError,TypeError,AttributeError,VaultError):
        raise HTTPException(409,'Archivo de fills GRVT no verificable',headers=no_cache) from None
    access.require(token,touch=False)
    return result


class ReadAction(Action):
    confirmation:Literal['OBSERVAR_GRVT_MAINNET','AUDITAR_GRVT_MAINNET','CONCILIAR_GRVT_MAINNET']


class Recover(Action):
    ttl_seconds:Annotated[StrictInt,Field(ge=30,le=300)]=180
    max_attempts:Annotated[StrictInt,Field(ge=1,le=3)]=3
    confirmation:Literal['RECUPERAR_POSICION_GRVT_MAINNET']


class RenewPreview(Action):
    ttl_seconds:Annotated[StrictInt,Field(ge=30,le=300)]=180
    max_attempts:Annotated[StrictInt,Field(ge=1,le=3)]=3
    exit_cap:Amount|None=None


class Renew(Send):
    confirmation:Literal['RENOVAR_CIERRE_GRVT_MAINNET']


class Sizing(Action):
    instrument:Annotated[StrictStr,Field(pattern=r'^[A-Z0-9]+_USDT_Perp$')]
    direction:Literal['LONG','SHORT']
    entry_cap:Amount
    exit_cap:Amount
    stop_percent:Amount
    take_percent:Amount


class Rearm(Action):
    stop_revision:Annotated[StrictInt,Field(ge=0,le=9007199254740991)]
    confirmation:Literal['REARMAR_GRVT_MAINNET']


def create_grvt_mainnet_router(access,*,authorized,authorized_passive,client_factory=None,public_post=None,clock_ns=None,waiter=None,lab_path=None,paper_path=None,bot_poll_seconds=2):
    router=APIRouter(prefix='/exchanges/grvt/mainnet',tags=['grvt-mainnet'])
    service=GrvtMainnetSession(access.vault,client_factory=client_factory,public=public_post,clock_ns=clock_ns,waiter=waiter)
    access._grvt_mainnet=service
    def fail():return HTTPException(status_code=409,detail='Operación MAINNET sin confirmar; consulta el registro y revisa la sesión',headers={'Cache-Control':'no-store'})
    def session(token):
        access.require(token,touch=False)
        return access._vault_generation

    @router.get('/subaccounts',dependencies=[Depends(authorized_passive)])
    def subaccounts(response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from sqlalchemy import select
        from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetBotRun
        response.headers['Cache-Control']='no-store';generation=session(token)
        try:
            with access.vault.try_guard_session_generation(generation) as available:
                if not available:return {'network':'MAINNET','pending':True,'sub_account_ids':[]}
                with access.vault.session() as db:
                    ids=list(db.scalars(select(GrvtMainnetBracket.sub_account_id).union(select(GrvtMainnetBotRun.sub_account_id)).order_by(GrvtMainnetBracket.sub_account_id).limit(501)))
            return {'network':'MAINNET','pending':False,'sub_account_ids':ids[:500],'has_more':len(ids)>500}
        except Exception:raise fail() from None

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(response:Response,sub_account_id:str=Query(pattern=r'^[1-9][0-9]{0,19}$'),
            offset:int=Query(default=0,ge=0,le=1_000_000),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:return _wire(service.history(token,sub_account_id,session(token),offset=offset))
        except HTTPException:raise
        except Exception:raise fail() from None

    @router.post('/connect',dependencies=[Depends(authorized)])
    def connect(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Connect,body);generation=session(token)
        try:
            if access._grvt_mainnet_bots is not None and access._grvt_mainnet_bots.active_sub(data.sub_account_id):raise ValueError('bot active')
            return service.connect(token,data.sub_account_id,tuple(data.permission_revision),generation,
            api_key=data.api_key.get_secret_value(),private_key=data.dedicated_private_key.get_secret_value(),signer=data.expected_signer)
        except Exception:raise fail() from None

    @router.post('/connect-view',dependencies=[Depends(authorized)])
    def connect_view(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(ConnectView,body);generation=session(token)
        try:
            return service.connect_view(token,data.sub_account_id,tuple(data.permission_revision),generation,
                api_key=data.api_key.get_secret_value())
        except Exception:
            raise HTTPException(status_code=409,detail='No se pudo conectar View a la subcuenta registrada',
                headers={'Cache-Control':'no-store'}) from None

    @router.post('/disconnect',dependencies=[Depends(authorized)])
    def disconnect(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Disconnect,body);generation=session(token)
        try:
            service.capture(token,data.sub_account_id,tuple(data.permission_revision),generation)
            if access._grvt_mainnet_bots is not None:access._grvt_mainnet_bots.stop_subaccount(data.sub_account_id)
            service.disconnect(data.sub_account_id);return service.status(token,data.sub_account_id,generation)
        except Exception:raise fail() from None

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Preview,body);generation=session(token)
        try:
            raw=data.model_dump(exclude={'permission_revision'})
            for name in ('size','entry_price','stop_loss','take_profit','entry_cap','exit_cap'):raw[name]=Decimal(raw[name])
            return _wire(service.preview(token,MainnetBracketDraft(**raw),tuple(data.permission_revision),generation))
        except Exception:raise fail() from None

    @router.post('/sizing',dependencies=[Depends(authorized)])
    def sizing(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Sizing,body);generation=session(token)
        try:return _wire(service.sizing(token,data.sub_account_id,tuple(data.permission_revision),generation,
            **data.model_dump(exclude={'sub_account_id','permission_revision'})))
        except Exception:raise fail() from None

    @router.post('/rearm',dependencies=[Depends(authorized)])
    def rearm(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Rearm,body);generation=session(token)
        try:return _wire(service.rearm(token,data.sub_account_id,tuple(data.permission_revision),generation,stop_revision=data.stop_revision))
        except Exception:raise fail() from None

    @router.post('/send',dependencies=[Depends(authorized)])
    def send(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Send,body);generation=session(token)
        try:
            value=service.send(token,data.review,data.review_mac,tuple(data.permission_revision),generation)
            return _wire({'network':'MAINNET','group':service.group(value['parent_client_order_id'])})
        except Exception:raise fail() from None

    @router.post('/stop',dependencies=[Depends(authorized)])
    def stop(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Stop,body);session(token)
        if access._grvt_mainnet_bots is not None:access._grvt_mainnet_bots.stop_subaccount(data.sub_account_id)
        try:
            result=service.stop(data.sub_account_id)
            if access._grvt_mainnet_bots is not None:access._grvt_mainnet_bots.retire_interrupted(data.sub_account_id)
            return _wire(result)
        except Exception:raise fail() from None

    @router.get('/groups/{parent}',dependencies=[Depends(authorized_passive)])
    def group(parent:str,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';generation=session(token)
        try:
            with access.vault.try_guard_session_generation(generation) as available:
                if not available:
                    cached=access._grvt_mainnet_bots.cached_group(parent) if access._grvt_mainnet_bots is not None else None
                    job=service.job(parent)
                    if cached is not None and job['status']!='IDLE':cached['job']=job
                    return _wire({'network':'MAINNET','pending':True,'parent_client_order_id':parent,'job':job,'group':cached})
                return _wire({'network':'MAINNET','pending':False,'group':service.group(parent)})
        except Exception:raise fail() from None

    @router.get('/groups/{parent}/executions-page',dependencies=[Depends(authorized_passive)])
    def executions(parent:str,response:Response,limit:int=Query(default=500,ge=1,le=500),offset:int=Query(default=0,ge=0,le=10000),
            revision:str|None=Query(default=None,pattern=r'^[a-f0-9]{64}$'),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_mainnet_executions import read_executions,executions_page
        response.headers['Cache-Control']='no-store';generation=session(token)
        try:
            with access.vault.try_guard_session_generation(generation) as available:
                if available:
                    with access.vault.session() as db:value=read_executions(db,parent)
                else:
                    value=access._grvt_mainnet_bots.cached_executions(parent) if access._grvt_mainnet_bots is not None else None
                    if value is None:raise ValueError('execution history busy')
                return _wire(executions_page(value,limit=limit,offset=offset,revision=revision))
        except Exception:raise fail() from None

    @router.post('/groups/{parent}/research-export',dependencies=[Depends(authorized_passive)])
    def research_export(parent: str, body: GrvtFillExport, response: Response,
                        token: str | None=Header(None,alias='X-Orbis-Vault-Token')):
        return export_fill_archive(access,token,response,parent,body,limit=False)

    @router.post('/groups/{parent}/action',dependencies=[Depends(authorized)])
    def action(parent:str,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(ReadAction,body);generation=session(token)
        try:
            saved=service.group(parent)
            if saved['sub_account_id']!=data.sub_account_id:raise ValueError('scope')
            _,checked,factory=service.capture(token,data.sub_account_id,tuple(data.permission_revision),generation)
            if data.confirmation=='OBSERVAR_GRVT_MAINNET':
                from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
                observe_mainnet_bracket(access.vault,parent,client_factory=factory,require_permission=checked,clock_ns=service.clock)
            elif data.confirmation=='AUDITAR_GRVT_MAINNET':
                from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket
                audit_mainnet_bracket(access.vault,parent,client_factory=factory,require_permission=checked,clock_ns=service.clock)
            else:
                from app.live_execution.grvt_mainnet_finance import reconcile_mainnet_bracket
                checked();reconcile_mainnet_bracket(access.vault,parent,now_ns=service.clock())
            return _wire({'network':'MAINNET','group':service.group(parent)})
        except Exception:raise fail() from None

    @router.post('/groups/{parent}/live',dependencies=[Depends(authorized_passive)])
    def live(parent:str,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Action,body);generation=session(token)
        try:return _wire({'network':'MAINNET','group':service.live(token,parent,data.sub_account_id,tuple(data.permission_revision),generation)})
        except Exception:raise fail() from None

    @router.post('/groups/{parent}/renew-preview',dependencies=[Depends(authorized)])
    def renew_preview(parent:str,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(RenewPreview,body);generation=session(token)
        try:return _wire(service.preview_renewal(token,parent,data.sub_account_id,tuple(data.permission_revision),generation,
            ttl_seconds=data.ttl_seconds,max_attempts=data.max_attempts,exit_cap=data.exit_cap))
        except Exception:raise fail() from None

    @router.post('/groups/{parent}/renew',dependencies=[Depends(authorized)])
    def renew(parent:str,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Renew,body);generation=session(token)
        try:return _wire({'network':'MAINNET','job':service.renew_recovery(token,parent,data.review,data.review_mac,tuple(data.permission_revision),generation)})
        except Exception:raise fail() from None

    @router.post('/groups/{parent}/recover',dependencies=[Depends(authorized)])
    def recover(parent:str,response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(Recover,body);generation=session(token)
        try:return _wire({'network':'MAINNET','job':service.start_recovery(token,parent,data.sub_account_id,
            tuple(data.permission_revision),generation,ttl_seconds=data.ttl_seconds,max_attempts=data.max_attempts)})
        except Exception:raise fail() from None
    from app.portfolio.grvt_mainnet_bot_api import create_mainnet_bot_router
    router.include_router(create_mainnet_bot_router(access,service,authorized=authorized,authorized_passive=authorized_passive,
        lab_path=lab_path,paper_path=paper_path,poll_seconds=bot_poll_seconds))
    return router
