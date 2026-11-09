"""Diario cifrado del observador TESTNET, sin credenciales ni autoridad.

Cada append cuesta una lectura de cola y una transacción local. La lectura
histórica y el backup verifican la cadena completa y sus resúmenes derivados.
Una ejecución sin evento terminal se presenta como interrumpida; no se reanuda.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import time
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.grvt_bracket_strategy_snapshot import verified_bracket_strategy
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_oco import _encode, _stamp
from app.live_execution.grvt_testnet_quote import _decimal
from app.portfolio.models import (GrvtTestnetBracketGroup, GrvtTestnetBracketStrategy,
    GrvtTestnetExitWatch, GrvtTestnetExitWatchEvent)
from app.portfolio.vault import RealVault


EventKind = Literal['STARTED', 'OBSERVATION', 'STATUS', 'STOP_REQUESTED', 'TERMINAL']
TERMINAL = {'COMPLETE', 'EXPIRED', 'ATTEMPT_LIMIT', 'REVIEW_UNSENT', 'STOPPED', 'NEEDS_REVIEW', 'GAP'}
_PROGRESS = {'CANCELLING', 'WAITING_AUDIT', 'GROUP_RECONCILED', 'AUDITING_GROUP',
    'AUDITING_CLOSE', 'CLOSE_RECONCILED', 'CLOSE_SENT', 'RECOVERING'}
_CAUSES = {'NONE', 'USER_STOP', 'VAULT_LOCKED', 'VIEW_PROTECTED', 'SHUTDOWN', 'REVOKED'}
_REASONS = {'STOP', 'TRAILING_STOP', 'TARGET', 'EXIT_RULE', 'TIME_LIMIT', 'POSITION_OPEN'}


def _header(db: Session, row: GrvtTestnetExitWatch) -> dict:
    value = json.loads(row.payload_json)
    recovery=value.get('schema')=='orbis.grvt.testnet.exit.watch.v2'
    if (set(value) != {'schema','id','parent_client_order_id','plan_digest','strategy_digest',
            'started_at_ns','deadline_ns','max_attempts'}|({'mode'} if recovery else set())
            or value['schema'] not in ('orbis.grvt.testnet.exit.watch.v1','orbis.grvt.testnet.exit.watch.v2')
            or recovery and value['mode']!='RECOVERY'
            or str(UUID(value['id'])) != row.id or value['id'] != row.id
            or value['parent_client_order_id'] != row.parent_client_order_id
            or str(value['started_at_ns']) != row.started_at_ns
            or _encode(value) != (row.payload_json,row.payload_digest)):
        raise ValueError('invalid watch identity')
    for key in ('started_at_ns','deadline_ns'): _stamp(value[key])
    if (not 0 < value['deadline_ns']-value['started_at_ns'] <= 86400_000_000_000
            or recovery and value['deadline_ns']-value['started_at_ns']>300_000_000_000
            or type(value['max_attempts']) is not int or not 1 <= value['max_attempts'] <= 3):
        raise ValueError('invalid watch limits')
    group = db.get(GrvtTestnetBracketGroup,row.parent_client_order_id)
    _verified(db,group)
    snapshot = verified_bracket_strategy(db,db.get(GrvtTestnetBracketStrategy,row.parent_client_order_id))
    if value['plan_digest'] != group.payload_digest or value['strategy_digest'] != _encode(snapshot)[1]:
        raise ValueError('changed watch source')
    return value


def _state(before: dict | None, kind: str, data: dict, at: int, header: dict) -> dict:
    _stamp(at)
    if at < header['started_at_ns'] or before and (at < before['last_event_at_ns'] or before['terminal']):
        raise ValueError('watch event after terminal or regressed clock')
    if not isinstance(data,dict): raise ValueError('watch event data')
    state = before.copy() if before else {'status':'STARTING','terminal':False,'stop_requested':False,
        'stop_cause':'NONE','observations':0,'last_market_event_ns':None,'last_reason':None}
    if kind == 'STARTED':
        if before is not None or data: raise ValueError('duplicate watch start')
    elif before is None:
        raise ValueError('watch event before start')
    elif kind == 'OBSERVATION':
        if (set(data) != {'event_time_ns','mark_price','action','reason'} or state['stop_requested']
                or header.get('mode')=='RECOVERY'):
            raise ValueError('invalid watch observation')
        event = data['event_time_ns']; _stamp(event)
        if (not 0 <= at-event <= 5_000_000_000 or event > header['deadline_ns']
                or state['last_market_event_ns'] is not None and event <= state['last_market_event_ns']
                or data['reason'] not in _REASONS
                or data['action'] != ('HOLD' if data['reason'] == 'POSITION_OPEN' else 'CLOSE')
                or not isinstance(data['mark_price'],str) or _decimal(data['mark_price']) <= 0):
            raise ValueError('incompatible watch observation')
        state.update(status='WATCHING',observations=state['observations']+1,
            last_market_event_ns=event,last_reason=data['reason'])
    elif kind == 'STATUS':
        if set(data) != {'status'} or data['status'] not in _PROGRESS:
            raise ValueError('invalid watch progress')
        if not state['stop_requested']: state['status'] = data['status']
    elif kind == 'STOP_REQUESTED':
        if set(data) != {'cause'} or data['cause'] not in _CAUSES-{'NONE'}:
            raise ValueError('invalid watch revocation')
        state.update(status='STOP_REQUESTED',stop_requested=True,stop_cause=data['cause'])
    elif kind == 'TERMINAL':
        if set(data) != {'status','cause'} or data['status'] not in TERMINAL or data['cause'] not in _CAUSES:
            raise ValueError('invalid watch end')
        state.update(status=data['status'],terminal=True)
        if data['cause'] != 'NONE': state.update(stop_requested=True,stop_cause=data['cause'])
    else:
        raise ValueError('unknown watch event')
    state['last_event_at_ns'] = at
    return state


def _event(row: GrvtTestnetExitWatchEvent, header: dict, before: dict | None, previous: str | None) -> dict:
    value = json.loads(row.payload_json)
    if (set(value) != {'schema','watch_id','sequence','previous_digest','at_ns','kind','data','state'}
            or value['schema'] != 'orbis.grvt.testnet.exit.watch.event.v1'
            or value['watch_id'] != header['id'] or type(value['sequence']) is not int
            or value['sequence'] != row.sequence or value['previous_digest'] != previous
            or _encode(value) != (row.payload_json,row.payload_digest)
            or _encode(_state(before,value['kind'],value['data'],value['at_ns'],header)) != _encode(value['state'])):
        raise ValueError('invalid watch chain')
    return value


def _require_flat_receipt(db: Session, parent: str, at: int) -> None:
    from app.live_execution.grvt_testnet_bracket_finance import verified_bracket_reconciliation
    from app.live_execution.grvt_testnet_bracket_emergency import read_emergency_attempts
    from app.live_execution.grvt_testnet_reconciliation import verified_reconciliation, _ns
    from app.portfolio.models import GrvtTestnetOrderIntent, GrvtTestnetReconciliation
    group = db.get(GrvtTestnetBracketGroup,parent)
    attempts = read_emergency_attempts(db,parent)
    if attempts:
        last = attempts[-1]
        if last['status'] != 'RECONCILED': raise ValueError('last reduction not reconciled')
        receipt = verified_reconciliation(db,db.get(GrvtTestnetOrderIntent,last['client_order_id']))
        receipt_at = _ns(db.get(GrvtTestnetReconciliation,last['client_order_id']).reconciled_at)
    else:
        receipt = verified_bracket_reconciliation(db,group,_verified(db,group))
        receipt_at = receipt['reconciled_at_ns'] if receipt else None
    if receipt is None or receipt['result']['positions'] or receipt_at > at:
        raise ValueError('complete watch without flat reconciled position')


def _append(db: Session, row: GrvtTestnetExitWatch, header: dict, kind: EventKind, data: dict, at: int) -> None:
    before = None
    if row.event_count:
        last = db.get(GrvtTestnetExitWatchEvent,(row.id,row.event_count))
        if last is None or last.payload_digest != row.head_digest:
            raise ValueError('watch tail missing')
        value = json.loads(last.payload_json)
        if _encode(value) != (last.payload_json,last.payload_digest): raise ValueError('changed watch tail')
        before = value['state']
        # STOP y finalización repetidos no añaden otro evento ni amplían límites.
        if before['terminal']:
            if kind == 'STOP_REQUESTED' or kind == 'TERMINAL' and data['status'] == before['status']: return
        if kind == 'STOP_REQUESTED' and before['stop_requested']: return
    sequence = row.event_count+1
    payload = {'schema':'orbis.grvt.testnet.exit.watch.event.v1','watch_id':row.id,'sequence':sequence,
        'previous_digest':row.head_digest,'at_ns':at,'kind':kind,'data':data,
        'state':_state(before,kind,data,at,header)}
    if kind == 'TERMINAL' and data['status'] == 'COMPLETE':
        _require_flat_receipt(db,row.parent_client_order_id,at)
    encoded,digest = _encode(payload)
    db.add(GrvtTestnetExitWatchEvent(watch_id=row.id,sequence=sequence,payload_json=encoded,payload_digest=digest))
    row.event_count = sequence; row.head_digest = digest


def begin_exit_watch(vault: RealVault, parent: str, *, max_attempts: int, deadline_ns: int,
        clock_ns: Callable[[],int] = time.time_ns,mode: Literal['WATCH','RECOVERY']='WATCH') -> str:
    try:
        if mode not in ('WATCH','RECOVERY'): raise ValueError('invalid watch mode')
        at = clock_ns(); _stamp(at)
        with vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group = db.get(GrvtTestnetBracketGroup,parent); _verified(db,group)
            strategy = verified_bracket_strategy(db,db.get(GrvtTestnetBracketStrategy,parent))
            run = str(uuid4())
            payload = {'schema':'orbis.grvt.testnet.exit.watch.v1','id':run,'parent_client_order_id':parent,
                'plan_digest':group.payload_digest,'strategy_digest':_encode(strategy)[1],
                'started_at_ns':at,'deadline_ns':deadline_ns,'max_attempts':max_attempts}
            if mode=='RECOVERY': payload.update(schema='orbis.grvt.testnet.exit.watch.v2',mode='RECOVERY')
            encoded,digest = _encode(payload)
            row = GrvtTestnetExitWatch(id=run,parent_client_order_id=parent,started_at_ns=str(at),
                payload_json=encoded,payload_digest=digest,event_count=0,head_digest=None)
            db.add(row); db.flush(); _header(db,row)
            _append(db,row,payload,'STARTED',{},at); db.commit()
            return run
    except Exception:
        raise GrvtTestnetBracketError('No se pudo registrar el inicio del seguimiento') from None


def append_exit_watch(vault: RealVault, watch_id: str, kind: EventKind, data: dict, *,
        clock_ns: Callable[[],int] = time.time_ns) -> None:
    try:
        if kind == 'STARTED': raise ValueError('start is atomic with header')
        with vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtTestnetExitWatch,watch_id)
            if row is None: raise ValueError('watch missing')
            header = _header(db,row)
            _append(db,row,header,kind,data,clock_ns()); db.commit()
    except Exception:
        raise GrvtTestnetBracketError('Diario de seguimiento no disponible; revisar posición') from None


def _read(db: Session, row: GrvtTestnetExitWatch, *, offset: int = 0, limit: int = 0) -> dict:
    header = _header(db,row); before = None; digest = None; count = 0; events = []
    query = select(GrvtTestnetExitWatchEvent).where(GrvtTestnetExitWatchEvent.watch_id == row.id).order_by(
        GrvtTestnetExitWatchEvent.sequence).execution_options(yield_per=500)
    for event in db.scalars(query):
        count += 1
        if event.sequence != count: raise ValueError('watch chain truncated')
        value = _event(event,header,before,digest)
        if value['kind'] == 'TERMINAL' and value['data']['status'] == 'COMPLETE':
            _require_flat_receipt(db,row.parent_client_order_id,value['at_ns'])
        before = value['state']; digest = event.payload_digest
        if offset < count <= offset+limit: events.append(value)
    if count != row.event_count or count < 1 or digest != row.head_digest:
        raise ValueError('watch head changed')
    return {**header,**before,'recorded_status':before['status'],
        'status':before['status'] if before['terminal'] else 'INTERRUPTED',
        'authorization':False,'event_count':count,
        'unobserved_since_ns':None if before['terminal'] else before['last_market_event_ns'] or header['started_at_ns'],
        'events':events,'offset':offset,'has_more':offset+limit < count if limit else False}


def exit_watch_history(vault: RealVault, parent: str, *, offset: int = 0, limit: int = 20) -> dict:
    try:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('invalid watch page')
        with vault.session() as db:
            rows = list(db.scalars(select(GrvtTestnetExitWatch).where(
                GrvtTestnetExitWatch.parent_client_order_id == parent).order_by(
                GrvtTestnetExitWatch.started_at_ns.desc(),GrvtTestnetExitWatch.id.desc()).offset(offset).limit(limit+1)))
            return {'items':[_read(db,row) for row in rows[:limit]],'offset':offset,'has_more':len(rows)>limit}
    except Exception:
        raise GrvtTestnetBracketError('Historial de seguimiento no verificable') from None


def read_exit_watch(vault: RealVault, parent: str, watch_id: str, *, offset: int = 0, limit: int = 100) -> dict:
    try:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid watch events page')
        with vault.session() as db:
            row = db.get(GrvtTestnetExitWatch,watch_id)
            if row is None or row.parent_client_order_id != parent: raise ValueError('foreign watch')
            return _read(db,row,offset=offset,limit=limit)
    except Exception:
        raise GrvtTestnetBracketError('Registro de seguimiento no verificable') from None


def verify_all_exit_watches(db: Session) -> None:
    for row in db.scalars(select(GrvtTestnetExitWatch)): _read(db,row)
