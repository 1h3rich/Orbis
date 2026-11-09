"""Prueba manual de entrada protegida GRVT TESTNET; comparte motores y acceso."""

from __future__ import annotations

import json
import time
from decimal import Decimal,localcontext
from typing import Annotated,Literal
from uuid import UUID

from fastapi import APIRouter,Body,Depends,Header,HTTPException,Query,Request,Response
from pydantic import BaseModel,ConfigDict,Field,SecretStr,StrictBool,StrictInt,StrictStr,ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.derivatives.paper_store import PaperPerpStore
from app.lab.store import LabStore
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError,native_recovery_reason,summarise_bracket_state
from app.live_execution.grvt_testnet_bracket_journal import _public,_verified,read_bracket,abandon_bracket
from app.live_execution.grvt_testnet_bracket_finance import _receipt_payload
from app.live_execution.grvt_testnet_bracket_preview import (
    preview_bracket_entry,preview_mac,verify_preview,sign_reviewed_entry,ENTRY_SCHEMA,
)
from app.live_execution.grvt_testnet_market_archive import (
    append_market_sample, find_market_sample, list_market_samples,
)
from app.live_execution.grvt_testnet_market_comparison import (
    MAX_SAMPLE_BYTES,compare_market_probes,match_market_probe_plan,
)
from app.live_execution.grvt_testnet_market_probe import MarketProbeError,capture_market_probe
from app.live_execution.grvt_testnet_risk import _public_post
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetBracketStrategy,GrvtTestnetStop
from app.portfolio.vault import VaultError


Amount=Annotated[StrictStr,Field(pattern=r'^[0-9]{1,18}(\.[0-9]{1,18})?$',max_length=37)]
Identity=Annotated[StrictStr,Field(pattern=r'^[0-9]{19,20}$')]


class Credentials(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    api_key: SecretStr=Field(min_length=1,max_length=512)


class MarketProbeRequest(Credentials):
    archive_id: UUID|None=Field(default=None,strict=False)
    ioc_client_id: Identity|None=None
    ioc_order_id: Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9]{1,80}$')]|None=None


class EntryPreview(Credentials):
    sub_account_id: Annotated[StrictStr,Field(pattern=r'^[0-9]{1,30}$')]
    session_id: Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    action_request_id: Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    size: Amount
    limit_price: Amount
    entry_cap: Amount
    exit_cap: Amount
    stop_loss: Amount
    take_profit: Amount
    ttl_minutes: Annotated[StrictInt,Field(ge=2,le=1440)]=60
    max_spread_bps: Annotated[StrictInt,Field(ge=0,le=100)]=25
    max_slippage_bps: Annotated[StrictInt,Field(ge=0,le=100)]=25


class SignedReview(Credentials):
    preview: dict
    preview_mac: Annotated[StrictStr,Field(pattern=r'^[a-f0-9]{64}$')]
    dedicated_private_key: SecretStr=Field(min_length=64,max_length=66)
    expected_signer: Annotated[StrictStr,Field(pattern=r'^0x[0-9a-fA-F]{40}$')]
    confirmation: Literal['FIRMAR_ENTRADA_PROTEGIDA_GRVT_TESTNET']


class EmergencySend(SignedReview):
    confirmation: Literal['FIRMAR_CIERRE_EMERGENCIA_GRVT_TESTNET']


class EmergencyReconcile(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    confirmation: Literal['CONCILIAR_CIERRE_EMERGENCIA_TESTNET']


class ReconcileRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    confirmation: Literal['CONCILIAR_ENTRADA_PROTEGIDA_GRVT_TESTNET']


class CancelRequest(Credentials):
    confirmation: Literal['RETIRAR_PROTECCIONES_ENTRADA_TESTNET']
    acknowledge_unprotected_position: StrictBool


class AbandonRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    confirmation: Literal['DESCARTAR_ENTRADA_PROTEGIDA_TESTNET']


class StrategyClosePrepare(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    action_request_id: Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    max_attempts: Annotated[StrictInt,Field(ge=1,le=3)]=2
    ttl_seconds: Annotated[StrictInt,Field(ge=10,le=300)]=120
    confirmation: Literal['PREPARAR_CIERRE_ESTRATEGIA_GRVT_TESTNET']


class StrategyCloseStart(StrategyClosePrepare,Credentials):
    dedicated_private_key: SecretStr=Field(min_length=64,max_length=66)
    expected_signer: Annotated[StrictStr,Field(pattern=r'^0x[0-9a-fA-F]{40}$')]
    confirmation: Literal['EJECUTAR_CIERRE_ESTRATEGIA_GRVT_TESTNET']


class StrategyCloseStop(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    confirmation: Literal['DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET']


class StrategyExitStart(Credentials):
    dedicated_private_key: SecretStr=Field(min_length=64,max_length=66)
    expected_signer: Annotated[StrictStr,Field(pattern=r'^0x[0-9a-fA-F]{40}$')]
    max_attempts: Annotated[StrictInt,Field(ge=1,le=3)]=2
    ttl_seconds: Annotated[StrictInt,Field(ge=30,le=86400)]=900
    permission_revision: Annotated[list[Annotated[StrictInt,Field(ge=0,le=9007199254740991)]],Field(min_length=2,max_length=2)]
    confirmation: Literal['ACTIVAR_SALIDAS_ESTRATEGIA_GRVT_TESTNET']


class NativeRecoveryStart(StrategyExitStart):
    ttl_seconds: Annotated[StrictInt,Field(ge=30,le=300)]=120
    confirmation: Literal['RECUPERAR_GRUPO_GRVT_TESTNET']


def _body(model,value):
    try: return model.model_validate(value)
    except ValidationError:
        raise HTTPException(status_code=422,detail='Datos de entrada protegida inválidos',
            headers={'Cache-Control':'no-store'}) from None


def _wire(value):
    """NS/uint64 no atraviesan JSON como números inexactos de JavaScript."""
    if isinstance(value,dict):
        return {key:str(item) if key.endswith('_ns') and type(item) is int else _wire(item)
            for key,item in value.items()}
    if isinstance(value,(list,tuple)): return [_wire(item) for item in value]
    if isinstance(value,Decimal): return format(value,'f')
    return value


def local_bracket_panel(vault,sub,*,offset,now_ns):
    with vault.session() as db:
        stop=db.get(GrvtTestnetStop,sub)
        rows=list(db.scalars(select(GrvtTestnetBracketGroup).where(
            GrvtTestnetBracketGroup.sub_account_id==sub).order_by(
            GrvtTestnetBracketGroup.created_at.desc(),GrvtTestnetBracketGroup.parent_client_order_id.desc())
            .offset(offset).limit(51)))
        groups=[]; recovery=None
        for row in rows[:50]:
            item=_public(db,row); can_reconcile=False
            if item['reconciliation'] is None and item['financial_audit_count']>=2:
                try:
                    _receipt_payload(db,row,_verified(db,row),now_ns=now_ns); can_reconcile=True
                except GrvtTestnetBracketError: pass
            can_emergency=False
            if stop and stop.stopped and item['reconciliation'] is not None:
                from app.live_execution.grvt_testnet_bracket_emergency import _source,require_no_unreconciled_order
                try:
                    require_no_unreconciled_order(db,sub)
                    _source(db,row.parent_client_order_id,now_ns=now_ns,
                        client_order_id=row.parent_client_order_id,historical=False)
                    can_emergency=True
                except Exception: pass
            if item['emergency_attempts']:
                from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
                from app.live_execution.grvt_testnet_reconciliation import _calculate
                from app.portfolio.models import GrvtTestnetOrderIntent
                if recovery is None: recovery={entry.client_order_id:entry for entry in read_testnet_recovery(vault,sub).items}
                attempts=[]
                for attempt in item['emergency_attempts']:
                    status=recovery[attempt['client_order_id']]; eligible=False
                    if status.journal_status=='OBSERVED' and status.verified_audit_count>=2:
                        try:
                            with localcontext() as ctx:
                                ctx.prec=100
                                _calculate(db,db.get(GrvtTestnetOrderIntent,status.client_order_id),at_ns=now_ns)
                            eligible=True
                        except Exception: pass
                    attempts.append({**attempt,'audit_count':status.verified_audit_count,
                        'can_reconcile':eligible,'reconciliation':status.reconciliation,
                        'external_status':status.external_status,'traded_size':status.external_traded_size})
                item['emergency_attempts']=attempts
            can_review=(row.status in ('ACKNOWLEDGED','POST_CLAIMED')
                and item['reconciliation'] is None and not item['emergency_attempts']
                and db.get(GrvtTestnetBracketStrategy,row.parent_client_order_id) is not None)
            native_reason=None
            if item['last_observation'] is not None:
                current=summarise_bracket_state(item['last_observation']['orders'],observed_at_ns=now_ns,
                    exit_expiration_ns=item['legs'][1]['expiration_ns'])
                native_reason=native_recovery_reason(current)
            groups.append({**item,'can_reconcile':can_reconcile,'can_emergency':can_emergency,
                'can_review_strategy_exit':can_review,'native_recovery_reason':native_reason,
                'can_recover_native':bool(can_review and item['cancel_attempt'] is None and
                    (native_reason is not None or now_ns>=item['legs'][1]['expiration_ns']))})
        return _wire({'network':'TESTNET','mode':'MANUAL_PROTECTED_ENTRY','authorization':False,
            'stopped':bool(stop and stop.stopped),'groups':groups,'offset':offset,'has_more':len(rows)>50})


def create_grvt_bracket_router(access,*,authorized,authorized_passive,client_factory,
        lab_path=None,paper_path=None,public_post=None,clock_ns=time.time_ns,close_waiter=None,exit_stream_factory=None):
    router=APIRouter(prefix='/exchanges/grvt/testnet/brackets',tags=['grvt-testnet-protected-entry'])
    public_post=public_post or _public_post
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    closes=GrvtStrategyCloseService(access.vault,client_factory=client_factory,public_post=public_post,
        clock_ns=clock_ns,waiter=close_waiter,stream_factory=exit_stream_factory)
    access._grvt_closes=closes
    def stores():
        if lab_path is None or paper_path is None:
            raise HTTPException(status_code=503,detail='LAB y GRVT PAPER no están configurados')
        return PaperPerpStore(paper_path,allow_create=False),LabStore(lab_path,allow_create=False)
    from app.live_execution.grvt_bot_service import GrvtBotService
    from app.portfolio.grvt_bot_api import create_grvt_bot_router
    bots=GrvtBotService(access.vault,closes,client_factory=client_factory,public_post=public_post,clock_ns=clock_ns)
    access._grvt_bots=bots
    # Hermano de /brackets: se incluye en la raíz REAL por el creador principal.
    access._grvt_bot_router=create_grvt_bot_router(access,bots,authorized=authorized,
        authorized_passive=authorized_passive,stores=stores)
    def failed(message='Estado de entrada protegida no verificable; consulta el registro'):
        return HTTPException(status_code=409,detail=message,headers={'Cache-Control':'no-store'})

    def watch_record_state(parent,record):
        run=closes.state(parent)
        active=bool(run and run['active'] and run['watch'] and run['watch']['record_id']==record['id'])
        return {**record,'active':active,**({'status':run['status'],'unobserved_since_ns':None} if active else {})}

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(response: Response,sub_account_id: str=Query(min_length=1,max_length=30,pattern=r'^[0-9]+$'),
            offset: int=Query(default=0,ge=0,le=1_000_000),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        access.require(token,touch=False)
        generation=access._vault_generation
        try:
            with access.vault.guard_session_generation(generation):
                result=local_bracket_panel(access.vault,sub_account_id,offset=offset,now_ns=clock_ns())
                for group in result['groups']:
                    group['strategy_close']=_wire(closes.snapshot(group['parent_client_order_id']))
            access.require(token,touch=False)
            return result
        except HTTPException: raise
        except Exception: raise failed() from None

    @router.get('/{parent}/strategy-close',dependencies=[Depends(authorized_passive)])
    def strategy_close_status(parent: Identity,response: Response,
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        access.require(token,touch=False)
        try:
            result=_wire(closes.snapshot(parent)); access.require(token,touch=False)
            return result
        except HTTPException: raise
        except Exception: raise failed() from None

    @router.get('/{parent}/exit-watches',dependencies=[Depends(authorized_passive)])
    def exit_watches(parent: Identity,response: Response,offset: int=Query(default=0,ge=0,le=1_000_000),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_exit_watch_archive import exit_watch_history
        response.headers['Cache-Control']='no-store'
        with access._lock:
            access.require(token,touch=False)
            try:
                page=exit_watch_history(access.vault,parent,offset=offset)
                return _wire({'network':'TESTNET','authorization':False,**page,
                    'items':[watch_record_state(parent,item) for item in page['items']]})
            except Exception: raise failed('Historial de seguimiento no verificable') from None

    @router.get('/{parent}/exit-watches/{watch_id}',dependencies=[Depends(authorized_passive)])
    def exit_watch_events(parent: Identity,watch_id: str,response: Response,
            offset: int=Query(default=0,ge=0,le=1_000_000),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_exit_watch_archive import read_exit_watch
        response.headers['Cache-Control']='no-store'
        with access._lock:
            access.require(token,touch=False)
            try:
                record=read_exit_watch(access.vault,parent,watch_id,offset=offset)
                return _wire({'network':'TESTNET',**watch_record_state(parent,record)})
            except Exception: raise failed('Registro de seguimiento no verificable') from None

    @router.post('/{parent}/strategy-close/start',dependencies=[Depends(authorized)],status_code=202)
    def strategy_close_start(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'; data=_body(StrategyCloseStart,body)
        with access._lock:
            access.require(token); paper,lab=stores()
            generation=access._vault_generation
            ticket=closes.permission_ticket(parent)
        try:
            result=closes.start(paper,lab,parent,action_request_id=data.action_request_id,
                max_attempts=data.max_attempts,ttl_seconds=data.ttl_seconds,
                api_key=data.api_key.get_secret_value(),private_key=data.dedicated_private_key.get_secret_value(),
                expected_signer=data.expected_signer,expected_generation=generation,expected_ticket=ticket)
            access.require(token,touch=False)
            return _wire(result)
        except HTTPException: raise
        except Exception: raise failed('Cierre no autorizado o ya en curso; consulta su estado') from None

    @router.post('/{parent}/strategy-close/prepare',dependencies=[Depends(authorized)])
    def strategy_close_prepare(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_strategy_close import request_strategy_close
        response.headers['Cache-Control']='no-store'; data=_body(StrategyClosePrepare,body)
        with access._lock:
            access.require(token); paper,lab=stores()
            generation=access._vault_generation
            ticket=closes.permission_ticket(parent)
        def preparation_clock():
            closes.require_permission(parent,ticket)
            return clock_ns()
        try:
            with access.vault.guard_session_generation(generation):
                prepared=request_strategy_close(paper,lab,access.vault,parent,action_request_id=data.action_request_id,
                    max_attempts=data.max_attempts,deadline_ns=preparation_clock()+data.ttl_seconds*1_000_000_000,clock_ns=preparation_clock)
            access.require(token,touch=False)
            return _wire({'network':'TESTNET','authorization':False,'request':prepared,'run':closes.state(parent)})
        except HTTPException: raise
        except Exception: raise failed('No hay una señal de cierre verificable para preparar') from None

    @router.post('/{parent}/strategy-close/stop',dependencies=[Depends(authorized)])
    def strategy_close_stop(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'; _body(StrategyCloseStop,body)
        with access._lock:
            access.require(token)
            return _wire({'network':'TESTNET','run':closes.stop(parent)})

    @router.post('/{parent}/strategy-exit/start',dependencies=[Depends(authorized)],status_code=202)
    def strategy_exit_start(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'; data=_body(StrategyExitStart,body)
        with access._lock:
            access.require(token); generation=access._vault_generation
            ticket=tuple(data.permission_revision)
        try:
            result=closes.start_watch(parent,max_attempts=data.max_attempts,ttl_seconds=data.ttl_seconds,
                api_key=data.api_key.get_secret_value(),private_key=data.dedicated_private_key.get_secret_value(),
                expected_signer=data.expected_signer,expected_generation=generation,expected_ticket=ticket)
            access.require(token,touch=False)
            return _wire(result)
        except HTTPException: raise
        except Exception: raise failed('Seguimiento no autorizado o ya en curso; revisa protección y límites') from None

    @router.post('/{parent}/native-recovery/start',dependencies=[Depends(authorized)],status_code=202)
    def native_recovery_start(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'; data=_body(NativeRecoveryStart,body)
        with access._lock:
            access.require(token); generation=access._vault_generation
            ticket=tuple(data.permission_revision)
        try:
            result=closes.start_watch(parent,max_attempts=data.max_attempts,ttl_seconds=data.ttl_seconds,
                api_key=data.api_key.get_secret_value(),private_key=data.dedicated_private_key.get_secret_value(),
                expected_signer=data.expected_signer,expected_generation=generation,expected_ticket=ticket,recovery_only=True)
            access.require(token,touch=False)
            return _wire(result)
        except HTTPException: raise
        except Exception: raise failed('Recuperación no autorizada; revisa el grupo y su permiso anterior') from None

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'; data=_body(EntryPreview,body)
        with access._lock:
            access.require(token); paper,lab=stores()
            try:
                prepared=preview_bracket_entry(paper,lab,access.vault,None,
                    sub=data.sub_account_id,session_id=data.session_id,action_request_id=data.action_request_id,
                    **{name:Decimal(getattr(data,name)) for name in
                        ('size','limit_price','entry_cap','exit_cap','stop_loss','take_profit')},
                    ttl_minutes=data.ttl_minutes,max_spread_bps=data.max_spread_bps,
                    max_slippage_bps=data.max_slippage_bps,public_post=public_post,clock_ns=clock_ns,
                    client_factory=lambda:client_factory(data.api_key.get_secret_value()))
                return {'network':'TESTNET','can_submit':False,'preview':prepared,
                    'preview_mac':preview_mac(prepared,access._grvt_oco_preview_key,token)}
            except Exception: raise failed('No se pudo verificar la señal, cuenta o protección') from None

    @router.post('/send',dependencies=[Depends(authorized)])
    def send(response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
        response.headers['Cache-Control']='no-store'; data=_body(SignedReview,body)
        with access._lock:
            access.require(token); paper,lab=stores()
            try:
                verify_preview(data.preview,data.preview_mac,access._grvt_oco_preview_key,token,
                    schema=ENTRY_SCHEMA,now_ns=clock_ns())
                orders,entry_limits,exit_limits,instrument=sign_reviewed_entry(access.vault,data.preview,
                    private_key=data.dedicated_private_key.get_secret_value(),expected_signer=data.expected_signer,
                    now_ns=clock_ns())
                _,group=submit_bracket_signal_once(paper,lab,access.vault,
                    client_factory(data.api_key.get_secret_value()),orders,entry_limits,exit_limits,instrument,
                    session_id=data.preview['source']['session_id'],action_request_id=data.preview['source']['action_request_id'],
                    max_spread_bps=data.preview['max_spread_bps'],max_slippage_bps=data.preview['max_slippage_bps'],
                    public_post=public_post,clock_ns=clock_ns)
                return _wire({'network':'TESTNET','group':group,'execution_unverified':True,'mainnet_order_sent':False})
            except Exception:
                raise failed('Envío bloqueado o incierto; consulta los tres IDs antes de continuar') from None

    def group_action(parent,token,action):
        with access._lock:
            access.require(token)
            try:
                group=read_bracket(access.vault,parent); action(group)
                return _wire({'network':'TESTNET','group':read_bracket(access.vault,parent),'mainnet_order_sent':False})
            except Exception: raise failed() from None

    @router.post('/{parent}/observe',dependencies=[Depends(authorized)])
    def observe(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
        response.headers['Cache-Control']='no-store'; data=_body(Credentials,body)
        return group_action(parent,token,lambda group:observe_bracket_orders(access.vault,
            client_factory(data.api_key.get_secret_value()),parent,clock_ns=clock_ns))

    @router.get('/{parent}/market-samples',dependencies=[Depends(authorized_passive)])
    def archived_market_samples(parent: Identity,response: Response,
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        with access._lock:
            access.require(token,touch=False)
            try:
                with access.vault.session() as db:
                    db.connection().exec_driver_sql('BEGIN')
                    rows=list_market_samples(db,parent)
                access.require(token,touch=False)
                return _wire({'network':'TESTNET','samples':rows,'authorization':False})
            except (MarketProbeError,VaultError) as error:
                raise failed(str(error)) from None

    @router.get('/{parent}/market-samples/{identifier}',dependencies=[Depends(authorized_passive)])
    def archived_market_sample(parent: Identity,identifier: UUID,response: Response,
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        with access._lock:
            access.require(token,touch=False)
            try:
                with access.vault.session() as db:
                    db.connection().exec_driver_sql('BEGIN')
                    saved=find_market_sample(db,parent,str(identifier))
                if saved is None:
                    raise HTTPException(status_code=404,detail='Muestra TESTNET no encontrada',
                                        headers={'Cache-Control':'no-store'})
                access.require(token,touch=False)
                return _wire(saved[0])
            except (MarketProbeError,VaultError) as error:
                raise failed(str(error)) from None

    @router.post('/{parent}/market-sample',dependencies=[Depends(authorized)])
    def market_sample(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'; data=_body(MarketProbeRequest,body)
        if data.ioc_client_id is not None and data.ioc_order_id is not None:
            raise HTTPException(status_code=422,detail='Selecciona un identificador IOC',headers={'Cache-Control':'no-store'})
        lookup = (('client_order_id',data.ioc_client_id) if data.ioc_client_id is not None else
                  ('order_id',data.ioc_order_id) if data.ioc_order_id is not None else None)
        identifier=str(data.archive_id) if data.archive_id is not None else None
        with access._lock:
            access.require(token,touch=False)
            generation=access._vault_generation

        def scoped_clock():
            access.require(token,touch=False)
            return clock_ns()

        try:
            if identifier is not None:
                with access._lock, access.vault.guard_session_generation(generation), access.vault.session() as db:
                    scoped_clock()
                    db.connection().exec_driver_sql('BEGIN')
                    saved=find_market_sample(db,parent,identifier)
                    if saved is not None:
                        if saved[1] != lookup:
                            raise MarketProbeError('El identificador de captura ya se usó para otro IOC')
                        scoped_clock()
                        return _wire(saved[0])
            group=read_bracket(access.vault,parent)
            if group['status'] not in ('POST_CLAIMED','ACKNOWLEDGED'):
                raise ValueError('bracket was not posted')
            scoped_clock()
            sample=capture_market_probe(client_factory(data.api_key.get_secret_value()),group['sub_account_id'],parent,
                ioc_client_id=data.ioc_client_id,ioc_order_id=data.ioc_order_id,clock_ns=scoped_clock)
            match_market_probe_plan(sample,group)
            sample['planned_exit_sizes']={leg['role']:leg['size'] for leg in group['legs'][1:]}
            if identifier is not None:
                with access._lock, access.vault.guard_session_generation(generation), access.vault.session() as db:
                    scoped_clock()
                    sample=append_market_sample(db,parent,identifier,sample,lookup=lookup)
            scoped_clock()
            return _wire(sample)
        except HTTPException: raise
        except MarketProbeError as error:
            access.require(token,touch=False)
            raise failed(str(error)) from None
        except Exception:
            access.require(token,touch=False)
            raise failed('Muestra Testnet no conciliada. Repite con los mismos identificadores.') from None

    @router.post('/{parent}/market-comparison',dependencies=[Depends(authorized)])
    async def market_comparison(parent: Identity,request: Request,response: Response,
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        data=bytearray()
        async for chunk in request.stream():
            # The envelope quotes up to three files, including their whitespace.
            if len(data) + len(chunk) > 6 * MAX_SAMPLE_BYTES + 1024:
                raise HTTPException(status_code=413,detail='Las muestras superan el tamaño admitido',
                    headers={'Cache-Control':'no-store'})
            data.extend(chunk)

        def compare():
            access.require(token,touch=False)
            try:
                body=json.loads(data)
                if type(body) is not dict or any(type(value) is not str for value in body.values()):
                    raise ValueError('invalid comparison request')
                keys=set(body)
                archive_keys={'before_id','after_id'}
                if keys in (archive_keys,archive_keys|{'final_id'}):
                    with access._lock:
                        access.require(token,touch=False)
                        with access.vault.session() as db:
                            db.connection().exec_driver_sql('BEGIN')
                            texts={}
                            for key,value in body.items():
                                identifier=str(UUID(value))
                                if value != identifier:
                                    raise ValueError('invalid archive identifier')
                                saved=find_market_sample(db,parent,identifier)
                                if saved is None:
                                    raise MarketProbeError('Muestra TESTNET guardada no encontrada')
                                texts[key.removesuffix('_id')]=json.dumps(saved[0],separators=(',',':'))
                    body=texts
                elif keys not in ({'before','after'},{'before','after','final'}):
                    raise ValueError('invalid comparison request')
                group=read_bracket(access.vault,parent)
                if group['status'] not in ('POST_CLAIMED','ACKNOWLEDGED'):
                    raise ValueError('bracket was not posted')
                result = compare_market_probes(body['before'], body['after'], parent=parent, group=group,
                                               final_json=body.get('final'))
            except MarketProbeError as error:
                access.require(token,touch=False)
                raise failed(str(error)) from None
            except (ValueError,TypeError,RecursionError,GrvtTestnetBracketError,VaultError):
                access.require(token,touch=False)
                raise failed('Muestras o grupo Testnet no verificables') from None
            access.require(token,touch=False)
            return _wire(result)

        return await run_in_threadpool(compare)

    @router.post('/{parent}/strategy-exit/review',dependencies=[Depends(authorized)])
    def strategy_exit_review(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_bracket_exit_review import review_bracket_strategy_exit
        response.headers['Cache-Control']='no-store'; data=_body(Credentials,body)
        with access._lock:
            access.require(token); generation=access._vault_generation
        try:
            with access.vault.guard_session_generation(generation):
                result=review_bracket_strategy_exit(access.vault,client_factory(data.api_key.get_secret_value()),
                    parent,public_post=public_post,clock_ns=clock_ns)
            access.require(token,touch=False)
            return _wire(result)
        except HTTPException: raise
        except Exception: raise failed('Salidas no verificables; conserva la protección nativa') from None

    @router.post('/{parent}/audit',dependencies=[Depends(authorized)])
    def audit(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_bracket_finance import audit_bracket
        response.headers['Cache-Control']='no-store'; data=_body(Credentials,body)
        return group_action(parent,token,lambda group:audit_bracket(access.vault,
            client_factory(data.api_key.get_secret_value()),parent,clock_ns=clock_ns))

    @router.post('/{parent}/reconcile',dependencies=[Depends(authorized)])
    def reconcile(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_bracket_finance import reconcile_bracket
        response.headers['Cache-Control']='no-store'; _body(ReconcileRequest,body)
        return group_action(parent,token,lambda group:reconcile_bracket(access.vault,parent,now_ns=clock_ns()))

    @router.post('/{parent}/cancel',dependencies=[Depends(authorized)])
    def cancel(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
        from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
        from app.live_execution.grvt_testnet_oco import _stamp
        response.headers['Cache-Control']='no-store'; data=_body(CancelRequest,body)
        if data.acknowledge_unprotected_position is not True:
            raise HTTPException(status_code=422,detail='Confirma que retirar protección no cierra la posición')
        def action(group):
            stop_testnet_subaccount(access.vault,group['sub_account_id'],now=_stamp(clock_ns()))
            cancel_bracket_once(access.vault,client_factory(data.api_key.get_secret_value()),parent,
                acknowledge_unprotected_position=True,clock_ns=clock_ns)
        return group_action(parent,token,action)

    @router.post('/{parent}/abandon',dependencies=[Depends(authorized)])
    def abandon(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'; _body(AbandonRequest,body)
        return group_action(parent,token,lambda group:abandon_bracket(access.vault,parent,now_ns=clock_ns()))

    @router.post('/{parent}/emergency/preview',dependencies=[Depends(authorized)])
    def emergency_preview(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_bracket_preview import preview_bracket_emergency
        response.headers['Cache-Control']='no-store'; data=_body(Credentials,body)
        with access._lock:
            access.require(token)
            try:
                prepared=preview_bracket_emergency(access.vault,None,
                    parent,public_post=public_post,clock_ns=clock_ns,
                    client_factory=lambda:client_factory(data.api_key.get_secret_value()))
                return {'network':'TESTNET','can_submit':False,'preview':prepared,
                    'preview_mac':preview_mac(prepared,access._grvt_oco_preview_key,token)}
            except Exception: raise failed('No hay un residuo conciliado y detenido que pueda cerrarse') from None

    @router.post('/{parent}/emergency/send',dependencies=[Depends(authorized)])
    def emergency_send(parent: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_bracket_preview import sign_reviewed_emergency,EMERGENCY_SCHEMA
        from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
        response.headers['Cache-Control']='no-store'; data=_body(EmergencySend,body)
        with access._lock:
            access.require(token)
            try:
                verify_preview(data.preview,data.preview_mac,access._grvt_oco_preview_key,token,
                    schema=EMERGENCY_SCHEMA,now_ns=clock_ns())
                if data.preview['parent_client_order_id']!=parent: raise ValueError('other reviewed group')
                order=sign_reviewed_emergency(access.vault,data.preview,
                    private_key=data.dedicated_private_key.get_secret_value(),expected_signer=data.expected_signer,
                    now_ns=clock_ns())
                result=submit_bracket_emergency_once(access.vault,client_factory(data.api_key.get_secret_value()),
                    parent,order,public_post=public_post,clock_ns=clock_ns)
                return {'network':'TESTNET','client_order_id':result.client_order_id,
                    'journal_status':result.status,'execution_unverified':True,'mainnet_order_sent':False}
            except Exception: raise failed('Cierre bloqueado o incierto; consulta su ID antes de continuar') from None

    def emergency_action(parent,cid,token,action):
        from app.portfolio.models import GrvtTestnetBracketEmergency
        with access._lock:
            access.require(token)
            try:
                group=read_bracket(access.vault,parent)
                with access.vault.session() as db:
                    row=db.get(GrvtTestnetBracketEmergency,cid)
                    if row is None or row.parent_client_order_id!=parent: raise ValueError('foreign emergency')
                action(group['sub_account_id'])
                return {'network':'TESTNET','client_order_id':cid,'mainnet_order_sent':False}
            except Exception: raise failed('Cierre no verificable; conserva su ID y la parada') from None

    @router.post('/{parent}/emergencies/{cid}/audit',dependencies=[Depends(authorized)])
    def emergency_audit(parent: Identity,cid: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_submit import audit_testnet_intent
        response.headers['Cache-Control']='no-store'; data=_body(Credentials,body)
        return emergency_action(parent,cid,token,lambda sub:audit_testnet_intent(access.vault,
            client_factory(data.api_key.get_secret_value()),sub,cid,clock_ns=clock_ns))

    @router.post('/{parent}/emergencies/{cid}/reconcile',dependencies=[Depends(authorized)])
    def emergency_reconcile(parent: Identity,cid: Identity,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_reconciliation import reconcile_testnet_order
        from app.live_execution.grvt_testnet_oco import _stamp
        response.headers['Cache-Control']='no-store'; _body(EmergencyReconcile,body)
        return emergency_action(parent,cid,token,lambda sub:reconcile_testnet_order(access.vault,sub,cid,now=_stamp(clock_ns())))

    return router
