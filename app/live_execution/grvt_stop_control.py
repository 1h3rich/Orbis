"""Procedencia de STOP TESTNET: una parada manual/incidencia queda enclavada.

El booleano existente sigue bloqueando los transportes. El diario añade
revisión y procedencia verificables para autorizar un rearme independiente.
Las paradas anteriores sin diario se consideran LEGACY_STOP, nunca automáticas.
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_oco import _encode, _stamp
from app.live_execution.grvt_testnet_reconciliation import _ns
from app.portfolio.models import (GrvtTestnetStop, GrvtTestnetStopControl, GrvtTestnetStopEvent,
    GrvtTestnetBracketGroup, GrvtTestnetBracketReconciliation, GrvtTestnetBracketEmergency, GrvtTestnetReconciliation)
from app.portfolio.vault import RealVault


StopReason = Literal['MANUAL', 'AUTO_EXIT', 'PARTIAL_EXIT', 'NATIVE_RECOVERY', 'REVIEW']
_REASONS = {'NONE', 'LEGACY_STOP', 'MANUAL', 'AUTO_EXIT', 'PARTIAL_EXIT', 'NATIVE_RECOVERY', 'REVIEW'}
_GROUP_REASONS = {'AUTO_EXIT','PARTIAL_EXIT','NATIVE_RECOVERY'}


def _check_state(state: dict) -> None:
    if set(state) != {'stopped','reason','parent_client_order_id','at_ns'} or type(state['stopped']) is not bool:
        raise ValueError('stop state fields')
    _stamp(state['at_ns'])
    if state['reason'] not in _REASONS or state['stopped'] != (state['reason'] != 'NONE'):
        raise ValueError('stop state reason')
    parent = state['parent_client_order_id']
    if (parent is not None and (not isinstance(parent,str) or not re.fullmatch(r'[0-9]{19,20}',parent))
            or (state['reason'] in _GROUP_REASONS) != (parent is not None)):
        raise ValueError('stop parent')


def _derive(before: dict, request: dict, at: int) -> dict:
    _check_state(before); _stamp(at)
    if at < before['at_ns'] or set(request) != {'reason','parent_client_order_id','release_receipt'}:
        raise ValueError('stop request')
    reason = request['reason']; parent = request['parent_client_order_id']
    if reason == 'AUTO_REARM':
        if (not before['stopped'] or before['reason'] != 'AUTO_EXIT'
                or before['parent_client_order_id'] != parent or not isinstance(request['release_receipt'],dict)):
            raise ValueError('cannot rearm a manual or unrelated stop')
        return {'stopped':False,'reason':'NONE','parent_client_order_id':None,'at_ns':at}
    if reason not in ('MANUAL',*_GROUP_REASONS,'REVIEW') or request['release_receipt'] is not None:
        raise ValueError('stop cause')
    if reason not in _GROUP_REASONS: parent = None
    if reason in _GROUP_REASONS and before['stopped']:
        if before['reason'] not in _GROUP_REASONS: reason, parent = before['reason'], before['parent_client_order_id']
        elif before['parent_client_order_id'] != parent: reason, parent = 'REVIEW', None
        elif before['reason']=='AUTO_EXIT': reason='AUTO_EXIT'
    value = {'stopped':True, 'reason':reason, 'parent_client_order_id':parent, 'at_ns':at}
    _check_state(value); return value


def flat_stop_receipt(db: Session, parent: str, at: int) -> dict:
    """Referencia congelada; la conciliación completa se verifica antes de liberar."""
    last = db.scalars(select(GrvtTestnetBracketEmergency).where(
        GrvtTestnetBracketEmergency.parent_client_order_id == parent)).all()
    if last:
        rows = [db.get(GrvtTestnetReconciliation,item.client_order_id) for item in last]
        if any(row is None for row in rows): raise ValueError('unreconciled reduction')
        row = max(rows,key=lambda item:(_ns(item.reconciled_at),item.client_order_id))
        receipt = {'kind':'IOC','id':row.client_order_id,'digest':row.payload_digest}
    else:
        row = db.get(GrvtTestnetBracketReconciliation,parent)
        if row is None: raise ValueError('missing flat receipt')
        receipt = {'kind':'BRACKET','id':parent,'digest':row.payload_digest}
    verify_flat_stop_receipt(db,parent,receipt,at); return receipt


def verify_flat_stop_receipt(db: Session, parent: str, proof: dict, at: int) -> None:
    if set(proof) != {'kind','id','digest'}: raise ValueError('flat stop proof fields')
    if proof['kind'] == 'BRACKET' and proof['id'] == parent:
        row = db.get(GrvtTestnetBracketReconciliation,parent)
        timestamp = int(row.reconciled_at_ns) if row else None
    elif proof['kind'] == 'IOC':
        link = db.get(GrvtTestnetBracketEmergency,proof['id'])
        if link is None or link.parent_client_order_id != parent: raise ValueError('foreign reduction receipt')
        row = db.get(GrvtTestnetReconciliation,proof['id'])
        timestamp = _ns(row.reconciled_at) if row else None
    else: raise ValueError('unknown flat stop proof')
    if (row is None or row.payload_digest != proof['digest'] or _ns(_stamp(timestamp)) > at
            or _encode(json.loads(row.payload_json)) != (row.payload_json,row.payload_digest)
            or json.loads(row.payload_json)['result']['positions']):
        raise ValueError('changed or non-flat stop receipt')


def verified_testnet_stop(db: Session, sub: str) -> dict:
    stop = db.get(GrvtTestnetStop,sub)
    names = db.connection().exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('grvt_testnet_stop_controls','grvt_testnet_stop_events')").scalar_one()
    if names != 2:
        if names or db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() >= 47:
            raise ValueError('stop provenance schema incomplete')
        control = None
    else: control = db.get(GrvtTestnetStopControl,sub)
    if control is None:
        return {'stopped':bool(stop and stop.stopped), 'reason':'LEGACY_STOP' if stop and stop.stopped else 'NONE',
            'parent_client_order_id':None, 'at_ns':_ns(stop.updated_at) if stop else None, 'revision':0}
    previous = None; before = None; count = 0
    for row in db.scalars(select(GrvtTestnetStopEvent).where(GrvtTestnetStopEvent.sub_account_id == sub).order_by(
            GrvtTestnetStopEvent.revision)):
        count += 1; value = json.loads(row.payload_json)
        if (set(value) != {'schema','sub_account_id','revision','previous_digest','initial','request','at_ns','state'}
                or value['schema'] != 'orbis.grvt.testnet.stop.event.v1' or value['sub_account_id'] != sub
                or type(value['revision']) is not int or row.revision != count or value['revision'] != count
                or value['previous_digest'] != previous or _encode(value) != (row.payload_json,row.payload_digest)):
            raise ValueError('stop journal changed')
        if before is None:
            before = value['initial']; _check_state(before)
            if before['reason'] not in ('NONE','LEGACY_STOP'): raise ValueError('initial stop cause')
        elif value['initial'] is not None: raise ValueError('stop journal restarted')
        if _derive(before,value['request'],value['at_ns']) != value['state']:
            raise ValueError('stop derivation changed')
        if value['request']['reason'] == 'AUTO_REARM':
            verify_flat_stop_receipt(db,value['request']['parent_client_order_id'],value['request']['release_receipt'],value['at_ns'])
        before = value['state']; previous = row.payload_digest
    if (count < 1 or count != control.revision or previous != control.head_digest or stop is None
            or stop.stopped is not before['stopped'] or _ns(stop.updated_at) != before['at_ns']):
        raise ValueError('stop projection or chain truncated')
    return {**before,'revision':count}


def _record_request(db: Session, sub: str, *, now: datetime, reason: str,
        parent_client_order_id: str | None = None, release_receipt: dict | None = None) -> dict:
    """Dentro de la transacción del llamador; nunca confirma parcial por sí solo."""
    if not isinstance(sub,str) or not re.fullmatch(r'[0-9]{1,30}',sub): raise ValueError('stop subaccount')
    before = verified_testnet_stop(db,sub)
    if reason in (*_GROUP_REASONS,'AUTO_REARM'):
        group = db.get(GrvtTestnetBracketGroup,parent_client_order_id)
        if group is None or group.sub_account_id != sub: raise ValueError('automatic stop without own group')
    elif parent_client_order_id is not None: raise ValueError('unexpected stop parent')
    at = max(_ns(now),before['at_ns'] or 0)
    initial = {key:before[key] for key in ('stopped','reason','parent_client_order_id','at_ns')}
    if initial['at_ns'] is None: initial['at_ns'] = at
    request = {'reason':reason,'parent_client_order_id':parent_client_order_id,'release_receipt':release_receipt}
    state = _derive(initial,request,at)
    control = db.get(GrvtTestnetStopControl,sub)
    stop = db.get(GrvtTestnetStop,sub)
    if stop is None:
        stop = GrvtTestnetStop(sub_account_id=sub,stopped=True,updated_at=_stamp(at)); db.add(stop); db.flush()
    if control is None:
        control = GrvtTestnetStopControl(sub_account_id=sub,revision=0,head_digest=None); db.add(control); db.flush()
    payload = {'schema':'orbis.grvt.testnet.stop.event.v1','sub_account_id':sub,'revision':control.revision+1,
        'previous_digest':control.head_digest,'initial':initial if control.revision == 0 else None,
        'request':request,'at_ns':at,'state':state}
    encoded, digest = _encode(payload)
    db.add(GrvtTestnetStopEvent(sub_account_id=sub,revision=payload['revision'],payload_json=encoded,payload_digest=digest))
    control.revision += 1; control.head_digest = digest; stop.stopped = state['stopped']; stop.updated_at = _stamp(at)
    db.flush(); return {**state,'revision':control.revision}


def record_testnet_stop(db: Session, sub: str, *, now: datetime, reason: StopReason = 'MANUAL',
        parent_client_order_id: str | None = None) -> dict:
    if reason not in ('MANUAL',*_GROUP_REASONS,'REVIEW'): raise ValueError('invalid stop reason')
    return _record_request(db,sub,now=now,reason=reason,parent_client_order_id=parent_client_order_id)


def release_automatic_stop(db: Session, sub: str, parent: str, *, expected_revision: int, now_ns: int) -> dict:
    """Sólo la parada de ese cierre, con revisión exacta y recibo plano real."""
    from app.live_execution.grvt_exit_watch_archive import _require_flat_receipt
    before = verified_testnet_stop(db,sub)
    if (before['revision'] != expected_revision or before['reason'] != 'AUTO_EXIT'
            or before['parent_client_order_id'] != parent): raise ValueError('stop changed before rearm')
    _require_flat_receipt(db,parent,now_ns)
    return _record_request(db,sub,now=_stamp(now_ns),reason='AUTO_REARM',parent_client_order_id=parent,
        release_receipt=flat_stop_receipt(db,parent,now_ns))


def stop_was_active(db: Session, sub: str, at_ns: int) -> bool:
    """Evidencia histórica; nunca habilita una cancelación o reducción actual."""
    state = verified_testnet_stop(db,sub)
    if state['revision'] == 0: return state['stopped'] and state['at_ns'] >= at_ns
    for row in db.scalars(select(GrvtTestnetStopEvent).where(GrvtTestnetStopEvent.sub_account_id == sub)):
        value = json.loads(row.payload_json)
        if value['state']['stopped'] and value['state']['at_ns'] == at_ns: return True
        initial = value['initial']
        if initial and initial['stopped'] and initial['at_ns'] == at_ns: return True
    return False


def read_testnet_stop(vault: RealVault, sub: str) -> dict:
    try:
        with vault.session() as db: return verified_testnet_stop(db,sub)
    except Exception:
        raise GrvtTestnetBracketError('Procedencia de parada TESTNET no verificable') from None


def verify_all_testnet_stop_controls(db: Session) -> None:
    for row in db.scalars(select(GrvtTestnetStopControl)): verified_testnet_stop(db,row.sub_account_id)
