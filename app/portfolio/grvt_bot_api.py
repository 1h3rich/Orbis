"""API privada para ciclos GRVT acotados y rearme expreso, sólo TESTNET."""

from collections.abc import Callable
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, StrictStr, model_validator

from app.live_execution.grvt_bot_source import bot_source
from app.live_execution.grvt_bot_entry_plan import GrvtBotConfigurationError
from app.live_execution.grvt_bot_executions import BotResearchExportError, export_bot_research, read_bot_executions
from app.portfolio.grvt_bracket_api import Credentials, Amount, _body, _wire
from app.portfolio.vault import RealVault


ArchiveResult = TypeVar("ArchiveResult")


class BotStart(Credentials):
    session_id: Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    sub_account_id: Annotated[StrictStr,Field(pattern=r'^[0-9]{1,30}$')]
    dedicated_private_key: SecretStr = Field(min_length=64,max_length=66)
    expected_signer: Annotated[StrictStr,Field(pattern=r'^0x[0-9a-fA-F]{40}$')]
    entry_cap: Amount
    exit_cap: Amount
    wait_seconds: Annotated[StrictInt,Field(ge=30,le=86400)] = 900
    exit_seconds: Annotated[StrictInt,Field(ge=30,le=86280)] = 900
    max_attempts: Annotated[StrictInt,Field(ge=1,le=3)] = 2
    max_spread_bps: Annotated[StrictInt,Field(ge=0,le=100)] = 25
    max_slippage_bps: Annotated[StrictInt,Field(ge=0,le=100)] = 25
    max_entries: Annotated[StrictInt,Field(ge=1,le=20)] = 1
    total_entry_cap: Amount | None = None
    loss_cap: Amount | None = None
    permission_revision: Annotated[list[Annotated[StrictInt,Field(ge=0,le=9007199254740991)]],Field(min_length=2,max_length=2)]
    confirmation: Literal['ACTIVAR_UN_CICLO_AUTOMATICO_GRVT_TESTNET']

    @model_validator(mode='after')
    def explicit_aggregate_limits(self):
        if self.max_entries > 1 and (self.total_entry_cap is None or self.loss_cap is None):
            raise ValueError('Los ciclos requieren presupuesto y límite de pérdida neta')
        if self.max_entries == 1 and (self.total_entry_cap is not None or self.loss_cap is not None):
            raise ValueError('Los límites agregados corresponden a varios ciclos')
        return self


class BotStop(BaseModel):
    # Parar sólo requiere la sesión local, no una clave del exchange.
    model_config = ConfigDict(extra='forbid',strict=True)
    confirmation: Literal['DETENER_CICLO_AUTOMATICO_GRVT_TESTNET']


class BotSubStop(BotStop):
    sub_account_id: Annotated[StrictStr,Field(pattern=r'^[0-9]{1,30}$')]


class BotRearm(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    permission_revision: Annotated[list[Annotated[StrictInt,Field(ge=0,le=9007199254740991)]],Field(min_length=2,max_length=2)]
    stop_revision: Annotated[StrictInt,Field(ge=1,le=9007199254740991)]
    confirmation: Literal['REARMAR_GRVT_TESTNET_TRAS_CIERRE']


class BotResearchExport(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sub_account_id: Annotated[StrictStr, Field(pattern=r'^[0-9]{1,30}$')]
    confirmation: Literal['EXPORTAR_JSONL_TESTNET']


def create_grvt_bot_router(access, service, *, authorized, authorized_passive, stores):
    router = APIRouter(prefix='/exchanges/grvt/testnet/bots',tags=['grvt-testnet-bot'])
    def failed():
        return HTTPException(status_code=409,detail='Ciclo no autorizado o pendiente de revisión; consulta el registro',
            headers={'Cache-Control':'no-store'})

    @router.get('/sources',dependencies=[Depends(authorized_passive)])
    def sources(response: Response, token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'; access.require(token,touch=False)
        try:
            paper, lab = stores()
            with paper.connect() as db:
                ids = [row[0] for row in db.execute('''SELECT id FROM paper_signal_sessions
                    WHERE status='ACTIVE' AND execution_mode='AUTO_PAPER' ORDER BY started_at_ns DESC,id LIMIT 100''')]
            items = []
            for session in ids:
                try: source = bot_source(paper,lab,session)
                except Exception: continue
                items.append({key:source[key] for key in ('session_id','symbol','timeframe','version_id')}|
                    {'direction':source['definition']['direction']})
            access.require(token,touch=False)
            return {'network':'TESTNET','authorization':False,'items':items}
        except HTTPException: raise
        except Exception: raise failed() from None

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(response: Response, sub_account_id: str=Query(min_length=1,max_length=30,pattern=r'^[0-9]+$'),
            offset: int=Query(default=0,ge=0,le=1_000_000),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'; access.require(token,touch=False)
        try:
            result = service.history(sub_account_id,offset=offset,expected_generation=access._vault_generation)
            access.require(token,touch=False); return _wire(result)
        except HTTPException: raise
        except Exception: raise failed() from None

    @router.post('/start',dependencies=[Depends(authorized)],status_code=202)
    def start(response: Response, body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from decimal import Decimal
        response.headers['Cache-Control'] = 'no-store'; data = _body(BotStart,body)
        with access._lock:
            access.require(token); generation = access._vault_generation; paper,lab = stores()
        try:
            result = service.start(paper,lab,data.session_id,sub_account_id=data.sub_account_id,
                entry_cap=Decimal(data.entry_cap),exit_cap=Decimal(data.exit_cap),wait_seconds=data.wait_seconds,
                exit_seconds=data.exit_seconds,max_attempts=data.max_attempts,max_spread_bps=data.max_spread_bps,
                max_slippage_bps=data.max_slippage_bps,api_key=data.api_key.get_secret_value(),
                private_key=data.dedicated_private_key.get_secret_value(),expected_signer=data.expected_signer,
                expected_generation=generation,expected_ticket=tuple(data.permission_revision),max_entries=data.max_entries,
                total_entry_cap=Decimal(data.total_entry_cap) if data.total_entry_cap is not None else None,
                loss_cap=Decimal(data.loss_cap) if data.loss_cap is not None else None)
            access.require(token,touch=False); return _wire(result)
        except HTTPException: raise
        except GrvtBotConfigurationError as exc:
            raise HTTPException(status_code=409,detail=str(exc),headers={'Cache-Control':'no-store'}) from None
        except Exception: raise failed() from None

    @router.post('/stop',dependencies=[Depends(authorized)])
    def stop_subaccount(response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'; data = _body(BotSubStop,body); access.require(token)
        service.stop_subaccount(data.sub_account_id)
        return {'network':'TESTNET','control_revision':list(service.permission_ticket(data.sub_account_id)),
            'authorization':False,'stop_requested':True}

    def read_archive(run_id: UUID, sub_account_id: str, token: str | None,
                     reader: Callable[[RealVault, str, str], ArchiveResult | None]) -> ArchiveResult:
        access.require(token, touch=False)
        try:
            with access.vault.try_guard_session_generation(access._vault_generation) as available:
                if not available:
                    raise HTTPException(status_code=503, detail='Archivo TESTNET en uso. Inténtalo de nuevo.',
                                        headers={'Cache-Control':'no-store'})
                result = reader(access.vault, str(run_id), sub_account_id)
        except HTTPException:
            raise
        except BotResearchExportError as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={'Cache-Control':'no-store'}) from None
        except Exception:
            raise failed() from None
        access.require(token, touch=False)
        if result is None:
            raise HTTPException(status_code=404, detail='Ciclo TESTNET no encontrado',
                                headers={'Cache-Control':'no-store'})
        return result

    @router.get('/{run_id}/executions', dependencies=[Depends(authorized_passive)])
    def executions(run_id: UUID, response: Response,
            sub_account_id: str = Query(min_length=1, max_length=30, pattern=r'^[0-9]+$'),
            token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        return _wire(read_archive(run_id, sub_account_id, token, read_bot_executions))

    @router.post('/{run_id}/research-export', dependencies=[Depends(authorized_passive)])
    def research_export(run_id: UUID, response: Response, body: BotResearchExport,
            token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        result = read_archive(run_id, body.sub_account_id, token, export_bot_research)
        if not result['record_count']:
            raise HTTPException(status_code=409, detail='El episodio todavía no tiene señales seleccionadas',
                                headers={'Cache-Control':'no-store'})
        return result

    @router.get('/{run_id}',dependencies=[Depends(authorized_passive)])
    def status(run_id: str,response: Response,
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'; access.require(token,touch=False)
        try:
            result = service.snapshot(run_id); access.require(token,touch=False); return _wire(result)
        except HTTPException: raise
        except Exception: raise failed() from None

    @router.post('/{run_id}/stop',dependencies=[Depends(authorized)])
    def stop(run_id: str,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'; _body(BotStop,body); access.require(token)
        try: return _wire(service.stop(run_id))
        except Exception: raise failed() from None

    @router.post('/{run_id}/rearm',dependencies=[Depends(authorized)])
    def rearm(run_id: str,response: Response,body: object=Body(),
            token: str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'; data = _body(BotRearm,body)
        with access._lock:
            access.require(token); generation = access._vault_generation
        try:
            result = service.rearm_finished(run_id,expected_ticket=tuple(data.permission_revision),
                expected_stop_revision=data.stop_revision,expected_generation=generation)
            access.require(token,touch=False); return _wire(result)
        except HTTPException: raise
        except Exception: raise failed() from None
    return router
