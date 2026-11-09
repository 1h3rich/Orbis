"""Panel manual GRVT OCO TESTNET, con el mismo acceso local de la bóveda."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, StrictStr, ValidationError
from sqlalchemy import select

from app.live_execution.grvt_testnet_oco import (
    GrvtTestnetOcoError, _parent, _public, _verified, require_no_pending_oco,
)
from app.live_execution.grvt_testnet_oco_finance import _receipt_payload, verified_oco_reconciliation
from app.live_execution.grvt_testnet_oco_continuity import oco_receipts
from app.live_execution.grvt_testnet_reconciliation import _ns, _positions
from app.portfolio.models import GrvtTestnetOrderIntent, GrvtTestnetOcoGroup, GrvtTestnetStop
from app.portfolio.vault import VaultError
from app.live_execution.grvt_testnet_oco_preview import preview_protection, preview_mac, MAX_PROTECTION_NOTIONAL
from app.live_execution.grvt_testnet_risk import _public_post


Amount = Annotated[StrictStr,Field(pattern=r'^[0-9]{1,18}(\.[0-9]{1,18})?$',max_length=37)]


class OcoCredentials(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    api_key: SecretStr = Field(min_length=1,max_length=512)


class OcoPreviewRequest(OcoCredentials):
    sub_account_id: Annotated[StrictStr,Field(pattern=r'^[0-9]{1,30}$')]
    parent_client_order_id: Annotated[StrictStr,Field(pattern=r'^[0-9]{19,20}$')]
    max_order_notional: Amount
    stop_loss: Amount
    take_profit: Amount
    max_slippage_bps: Annotated[StrictInt,Field(ge=0,le=100)] = 25
    ttl_minutes: Annotated[StrictInt,Field(ge=1,le=1440)] = 60


class OcoSendRequest(OcoCredentials):
    preview: dict
    preview_mac: Annotated[StrictStr,Field(pattern=r'^[a-f0-9]{64}$')]
    dedicated_private_key: SecretStr = Field(min_length=64,max_length=66)
    expected_signer: Annotated[StrictStr,Field(pattern=r'^0x[0-9a-fA-F]{40}$')]
    confirmation: Literal['FIRMAR_OCO_GRVT_TESTNET']


class OcoReconcileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    confirmation: Literal['CONCILIAR_OCO_GRVT_TESTNET']


class OcoCancelRequest(OcoCredentials):
    confirmation: Literal['RETIRAR_PROTECCIONES_OCO_TESTNET']
    acknowledge_unprotected_position: StrictBool


class OcoAbandonRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    confirmation: Literal['DESCARTAR_OCO_GRVT_TESTNET']


def _body(model, value):
    # No dejar que el error 422 automático de FastAPI repita claves del cuerpo.
    try:
        return model.model_validate(value)
    except ValidationError:
        raise HTTPException(status_code=422,detail='Datos OCO inválidos',headers={'Cache-Control':'no-store'}) from None


def local_oco_panel(vault, sub_account_id: str, *, offset: int = 0, now_ns: int) -> dict:
    with vault.session() as db:
        stop = db.get(GrvtTestnetStop,sub_account_id)
        stopped = stop is not None and stop.stopped
        pending = False
        try:
            require_no_pending_oco(db,sub_account_id)
        except GrvtTestnetOcoError:
            # Se verifica por separado cada grupo al construir la página.
            # Una falta de conciliación no se convierte en autorización.
            pending = True
        rows = list(db.scalars(select(GrvtTestnetOcoGroup).where(
            GrvtTestnetOcoGroup.sub_account_id == sub_account_id).order_by(
                GrvtTestnetOcoGroup.created_at.desc(),GrvtTestnetOcoGroup.id.desc()).offset(offset).limit(51)))
        groups = []
        for row in rows[:50]:
            item = _public(db,row)
            can_reconcile = False
            if item['reconciliation'] is None and item['financial_audit_count'] >= 2:
                try:
                    _receipt_payload(db,row,_verified(db,row),at_ns=now_ns)
                    can_reconcile = True
                except GrvtTestnetOcoError:
                    pass
            groups.append({**item,'can_reconcile':can_reconcile})
        latest = db.scalar(select(GrvtTestnetOrderIntent).where(
            GrvtTestnetOrderIntent.sub_account_id == sub_account_id,
            GrvtTestnetOrderIntent.status == 'RECONCILED').order_by(
                GrvtTestnetOrderIntent.updated_at.desc(),GrvtTestnetOrderIntent.client_order_id.desc()).limit(1))
        sources = []
        if latest is not None:
            try:
                source,_ = _parent(db,latest.client_order_id,latest=True)
                for receipt,group in oco_receipts(db,sub_account_id):
                    if int(receipt.reconciled_at_ns) > _ns(latest.updated_at):
                        after = verified_oco_reconciliation(db,group,_verified(db,group))
                        if _positions(after['result']['positions']) != _positions(source['result']['positions']):
                            raise GrvtTestnetOcoError('La posición de origen cambió')
                instrument,size = source['result']['positions'][0]
                sources = [{'parent_client_order_id':latest.client_order_id,'instrument':instrument,
                    'side':'SELL' if not size.startswith('-') else 'BUY',
                    'size':size.lstrip('-'),'suggested_cap':format(min(latest.max_order_notional,MAX_PROTECTION_NOTIONAL),'f'),
                    'eligible':not stopped and not pending}]
            except GrvtTestnetOcoError:
                pass
        return {'network':'TESTNET','authorization':False,'stopped':stopped,
            'sources':sources,'groups':groups,'offset':offset,'has_more':len(rows)>50}


def create_grvt_oco_router(access, *, authorized, authorized_passive, client_factory,
                           public_post=None, clock_ns=time.time_ns) -> APIRouter:
    router = APIRouter(prefix='/exchanges/grvt/testnet/oco',tags=['grvt-testnet-oco'])
    public_post = public_post or _public_post

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(response: Response,
                sub_account_id: str = Query(min_length=1,max_length=30,pattern=r'^[0-9]+$'),
                offset: int = Query(default=0,ge=0,le=1_000_000),
                token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'
        with access._lock:
            access.require(token,touch=False)
            try:
                return local_oco_panel(access.vault,sub_account_id,offset=offset,now_ns=clock_ns())
            except (GrvtTestnetOcoError,VaultError,ValueError):
                raise HTTPException(status_code=409,detail='Registro OCO no verificable',headers={'Cache-Control':'no-store'}) from None

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(response: Response, body: object = Body(),
                token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'
        data = _body(OcoPreviewRequest,body)
        with access._lock:
            access.require(token)
            try:
                from app.live_execution.grvt_testnet_oco_preview import protection_source
                if not 0 < Decimal(data.max_order_notional) <= MAX_PROTECTION_NOTIONAL:
                    raise GrvtTestnetOcoError('Tope TESTNET inválido')
                protection_source(access.vault,data.sub_account_id,data.parent_client_order_id)
                client = client_factory(data.api_key.get_secret_value())
                prepared = preview_protection(access.vault,client,sub=data.sub_account_id,
                    parent_id=data.parent_client_order_id,cap=Decimal(data.max_order_notional),
                    stop_loss=Decimal(data.stop_loss),take_profit=Decimal(data.take_profit),
                    slippage_bps=data.max_slippage_bps,ttl_minutes=data.ttl_minutes,
                    public_post=public_post,clock_ns=clock_ns)
                return {'network':'TESTNET','can_submit':False,'preview':prepared,
                    'preview_mac':preview_mac(prepared,access._grvt_oco_preview_key,token)}
            except Exception:
                raise HTTPException(status_code=409,detail='No se pudo verificar la preparación OCO',
                    headers={'Cache-Control':'no-store'}) from None

    @router.post('/send',dependencies=[Depends(authorized)])
    def send(response: Response, body: object = Body(),
             token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'
        data = _body(OcoSendRequest,body)
        with access._lock:
            access.require(token)
            try:
                from app.live_execution.grvt_testnet_oco_preview import verify_preview,sign_reviewed_protection
                from app.live_execution.grvt_testnet_oco import prepare_oco
                from app.live_execution.grvt_testnet_oco_submit import submit_oco_once
                verify_preview(data.preview,data.preview_mac,access._grvt_oco_preview_key,token,now_ns=clock_ns())
                orders,limits,instrument = sign_reviewed_protection(access.vault,data.preview,
                    private_key=data.dedicated_private_key.get_secret_value(),
                    expected_signer=data.expected_signer,now_ns=clock_ns())
                prepare_oco(access.vault,data.preview['group_id'],data.preview['parent_client_order_id'],
                    orders,limits,instrument,now_ns=clock_ns())
                group = submit_oco_once(access.vault,client_factory(data.api_key.get_secret_value()),
                    data.preview['group_id'],orders,limits,public_post=public_post,clock_ns=clock_ns)
                return {'network':'TESTNET','group':group}
            except Exception:
                raise HTTPException(status_code=409,
                    detail='Envío OCO no confirmado. Consulta el registro; no reenvíes la preparación.',
                    headers={'Cache-Control':'no-store'}) from None

    def group_action(group_id,token,action):
        from app.live_execution.grvt_testnet_oco import read_oco
        with access._lock:
            access.require(token)
            try:
                group = read_oco(access.vault,group_id)
                action(group)
                return {'network':'TESTNET','group':read_oco(access.vault,group_id)}
            except Exception:
                raise HTTPException(status_code=409,detail='Estado OCO no verificable; conserva el bloqueo',
                    headers={'Cache-Control':'no-store'}) from None

    @router.post('/{group_id}/observe',dependencies=[Depends(authorized)])
    def observe(group_id: str, response: Response, body: object = Body(),
                token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
        response.headers['Cache-Control'] = 'no-store'
        data = _body(OcoCredentials,body)
        def action(group):
            if group['status'] not in ('POST_CLAIMED','ACKNOWLEDGED'):
                raise GrvtTestnetOcoError('No hay envío que consultar')
            observe_oco_orders(access.vault,client_factory(data.api_key.get_secret_value()),group_id,clock_ns=clock_ns)
        return group_action(group_id,token,action)

    @router.post('/{group_id}/audit',dependencies=[Depends(authorized)])
    def audit(group_id: str, response: Response, body: object = Body(),
              token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_oco_finance import audit_oco
        response.headers['Cache-Control'] = 'no-store'
        data = _body(OcoCredentials,body)
        def action(group):
            if group['status'] not in ('POST_CLAIMED','ACKNOWLEDGED') or group['reconciliation'] is not None:
                raise GrvtTestnetOcoError('No hay envío pendiente de auditoría')
            audit_oco(access.vault,client_factory(data.api_key.get_secret_value()),group_id,clock_ns=clock_ns)
        return group_action(group_id,token,action)

    @router.post('/{group_id}/reconcile',dependencies=[Depends(authorized)])
    def reconcile(group_id: str, response: Response, body: object = Body(),
                  token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_oco_finance import reconcile_oco
        response.headers['Cache-Control'] = 'no-store'
        _body(OcoReconcileRequest,body)
        return group_action(group_id,token,lambda group:reconcile_oco(access.vault,group_id,now_ns=clock_ns()))

    @router.post('/{group_id}/cancel',dependencies=[Depends(authorized)])
    def cancel(group_id: str, response: Response, body: object = Body(),
               token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
        from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
        from app.live_execution.grvt_testnet_oco import _stamp
        response.headers['Cache-Control'] = 'no-store'
        data = _body(OcoCancelRequest,body)
        if data.acknowledge_unprotected_position is not True:
            raise HTTPException(status_code=422,detail='Confirma la retirada de ambas protecciones sin cerrar posición',
                headers={'Cache-Control':'no-store'})
        def action(group):
            if (group['status'] not in ('POST_CLAIMED','ACKNOWLEDGED')
                    or group['cancel_attempt'] is not None or group['reconciliation'] is not None):
                raise GrvtTestnetOcoError('No hay cancelación OCO nueva disponible')
            stop_testnet_subaccount(access.vault,group['sub_account_id'],now=_stamp(clock_ns()))
            cancel_oco_once(access.vault,client_factory(data.api_key.get_secret_value()),group_id,
                acknowledge_unprotected_position=True,clock_ns=clock_ns)
        return group_action(group_id,token,action)

    @router.post('/{group_id}/abandon',dependencies=[Depends(authorized)])
    def abandon(group_id: str, response: Response, body: object = Body(),
                token: str | None = Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.grvt_testnet_oco import abandon_oco
        response.headers['Cache-Control'] = 'no-store'
        _body(OcoAbandonRequest,body)
        return group_action(group_id,token,lambda group:abandon_oco(access.vault,group_id,now_ns=clock_ns()))

    return router
