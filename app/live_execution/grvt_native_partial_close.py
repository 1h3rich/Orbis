"""Un parcial nativo solicita cancelar y conciliar, nunca acredita el residuo.

El contrato observado es el disparador. Los fills, costes, saldo y cantidad
operable siguen dependiendo de los dos audits terminales del coordinador.
No se inventa una señal PAPER ni se evalúa una cotización para justificarla.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import time
from typing import Literal,TypedDict,cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError,native_partial_role
from app.live_execution.grvt_testnet_bracket_journal import _verified,read_bracket
from app.live_execution.grvt_testnet_bracket_observation import latest_bracket_observation,observe_bracket_orders
from app.live_execution.grvt_testnet_oco import _encode,_stamp
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.live_execution.grvt_native_exit import NativeGroupExit,NativePartialExit,classify_native_exit
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetBracketObservation,GrvtTestnetStrategyClose
from app.portfolio.vault import RealVault


SCHEMA='orbis.grvt.testnet.native.partial.close.v1'


class NativePartialCloseRequest(TypedDict):
    schema: Literal['orbis.grvt.testnet.native.partial.close.v1']
    parent_client_order_id: str
    plan_digest: str
    evidence: dict
    created_at_ns: int
    deadline_ns: int
    max_attempts: int


def _close_id(parent: str,evidence: dict) -> str:
    digest=_encode({'domain':SCHEMA,'parent':parent,'evidence':evidence})[1]
    return str(2**63|(int(digest[:16],16)&(2**63-1)))


def verified_native_partial_close(db: Session,row: GrvtTestnetStrategyClose,payload: dict) -> NativePartialCloseRequest:
    try:
        if (set(payload)!={'schema','parent_client_order_id','plan_digest','evidence','created_at_ns','deadline_ns','max_attempts'}
                or payload['schema']!=SCHEMA or payload['parent_client_order_id']!=row.parent_client_order_id
                or _encode(payload)!=(row.payload_json,row.payload_digest)):
            raise ValueError('partial request shape')
        parent=row.parent_client_order_id
        group=db.get(GrvtTestnetBracketGroup,parent); plan=_verified(db,group)
        latest_bracket_observation(db,group,plan)
        proof=payload['evidence']
        if (not isinstance(proof,dict) or set(proof)!={'scope','native_observation_sequence','native_observation_digest','reason'}
                or proof['scope']!='ORDER_STATE_ONLY' or type(proof['native_observation_sequence']) is not int
                or proof['native_observation_sequence']<1): raise ValueError('partial evidence shape')
        native=db.scalar(select(GrvtTestnetBracketObservation).where(
            GrvtTestnetBracketObservation.parent_client_order_id==parent,
            GrvtTestnetBracketObservation.sequence==proof['native_observation_sequence']))
        if native is None or native.payload_digest!=proof['native_observation_digest']:
            raise ValueError('missing partial observation')
        state=json.loads(native.payload_json)['state']; role=native_partial_role(state)
        reason={'STOP_LOSS':'NATIVE_PARTIAL_STOP','TAKE_PROFIT':'NATIVE_PARTIAL_TARGET'}.get(role)
        for key in ('created_at_ns','deadline_ns'): _stamp(payload[key])
        if (reason is None or proof['reason']!=reason or payload['plan_digest']!=group.payload_digest
                or row.close_client_order_id!=_close_id(parent,proof)
                or not 0<=payload['created_at_ns']-state['observed_at_ns']<=5_000_000_000
                or not 0<payload['deadline_ns']-payload['created_at_ns']<=300_000_000_000
                or type(payload['max_attempts']) is not int or not 1<=payload['max_attempts']<=3):
            raise ValueError('partial request scope')
        return cast(NativePartialCloseRequest,payload)
    except Exception:
        raise GrvtTestnetBracketError('Solicitud de cierre por parcial nativo no verificable') from None


def request_native_partial_close(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,*,
        max_attempts: int,deadline_ns: int,clock_ns: Callable[[],int]=time.time_ns
        ) -> NativePartialCloseRequest | NativeGroupExit:
    from app.live_execution.grvt_testnet_strategy_close import _exclusive,read_strategy_close
    from app.live_execution.grvt_stop_control import record_testnet_stop
    at=clock_ns(); _stamp(at)
    if (type(max_attempts) is not int or not 1<=max_attempts<=3 or type(deadline_ns) is not int
            or not 0<deadline_ns-at<=300_000_000_000):
        raise GrvtTestnetBracketError('Límites de cierre inválidos')
    with _exclusive(vault):
        existing=read_strategy_close(vault,parent)
        if existing is not None:
            if existing['schema']!=SCHEMA: raise GrvtTestnetBracketError('El grupo ya tiene otro origen de cierre')
            return existing
        group=read_bracket(vault,parent)
        if group['reconciliation'] is not None or group['emergency_attempts'] or group['cancel_attempt'] is not None:
            raise GrvtTestnetBracketError('El grupo requiere revisar su recuperación anterior')
        state=observe_bracket_orders(vault,client,parent,clock_ns=clock_ns)
        outcome=classify_native_exit(parent,state)
        if isinstance(outcome,NativeGroupExit): return outcome
        if not isinstance(outcome,NativePartialExit): raise GrvtTestnetBracketError('No hay parcial nativo verificado')
        with vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                if db.get(GrvtTestnetStrategyClose,parent) is not None: raise ValueError('concurrent close')
                stored=db.get(GrvtTestnetBracketGroup,parent); plan=_verified(db,stored)
                latest=latest_bracket_observation(db,stored,plan)
                if latest!=state: raise ValueError('partial observation changed')
                native=db.scalar(select(GrvtTestnetBracketObservation).where(
                    GrvtTestnetBracketObservation.parent_client_order_id==parent).order_by(
                        GrvtTestnetBracketObservation.sequence.desc()).limit(1))
                proof={'scope':'ORDER_STATE_ONLY','native_observation_sequence':native.sequence,
                    'native_observation_digest':native.payload_digest,
                    'reason':'NATIVE_PARTIAL_STOP' if outcome.role=='STOP_LOSS' else 'NATIVE_PARTIAL_TARGET'}
                payload: NativePartialCloseRequest={'schema':SCHEMA,'parent_client_order_id':parent,
                    'plan_digest':stored.payload_digest,'evidence':proof,'created_at_ns':clock_ns(),
                    'deadline_ns':deadline_ns,'max_attempts':max_attempts}
                encoded,digest=_encode(payload)
                row=GrvtTestnetStrategyClose(parent_client_order_id=parent,close_client_order_id=_close_id(parent,proof),
                    payload_json=encoded,payload_digest=digest)
                db.add(row); db.flush(); verified_native_partial_close(db,row,payload)
                record_testnet_stop(db,stored.sub_account_id,now=_stamp(payload['created_at_ns']),
                    reason='AUTO_EXIT',parent_client_order_id=parent)
                db.commit(); return payload
            except Exception:
                db.rollback()
                raise GrvtTestnetBracketError('No se pudo registrar el cierre por parcial nativo') from None
