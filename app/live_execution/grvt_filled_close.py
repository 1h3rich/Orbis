"""Cierre acotado desde evidencia reproducible de fills/mercado TESTNET.

El registro v2 comparte los transportes de cancelación, auditoría y reducción
del coordinador v1. No genera operaciones PAPER ni autoridad de nuevas entradas.
"""

from dataclasses import asdict
from decimal import Decimal
import json
import time
from typing import Callable,Literal,TypedDict,cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.lab.engine import INTERVALS,validate_candles
from app.live_execution.grvt_bracket_strategy_snapshot import verified_bracket_strategy
from app.live_execution.grvt_filled_strategy_exit import GrvtExitMark,evaluate_filled_strategy_exit
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_bracket_observation import latest_bracket_observation
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_oco import _encode,_stamp
from app.live_execution.grvt_testnet_oco_preflight import _safe
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,GrvtTestnetOrderObservation
from app.live_execution.grvt_testnet_quote import _decimal
from app.portfolio.models import (
    GrvtTestnetBracketGroup,GrvtTestnetBracketObservation,GrvtTestnetBracketStrategy,
    GrvtTestnetStrategyClose,GrvtTestnetStop,
)
from app.portfolio.vault import RealVault


class FilledCloseRequest(TypedDict):
    schema: Literal['orbis.grvt.testnet.strategy.close.v2']
    parent_client_order_id: str
    plan_digest: str
    evidence: dict
    created_at_ns: int
    deadline_ns: int
    max_attempts: int


class _ArchivedFills:
    """Reproduce el parser de fills sobre las páginas locales archivadas."""
    def __init__(self,rows: list[dict]): self.rows=rows
    def read_fill_history(self,sub: str,start: str,cursor: str,**kwargs) -> dict:
        offset=int(cursor or 0); end=offset+500
        return {'result':self.rows[offset:end],'next':str(end) if end<len(self.rows) else ''}


def filled_close_id(parent: str,evidence: dict) -> str:
    digest=_encode({'domain':'orbis.grvt.filled.close.v1','parent':parent,'evidence':evidence})[1]
    return str(2**63|(int(digest[:16],16)&(2**63-1)))


def verify_filled_exit_evidence(db: Session,parent: str,evidence: dict) -> dict:
    """Reproduce definición, fill audit y decisión en su timestamp histórico."""
    try:
        if (not isinstance(evidence,dict) or set(evidence)!={'schema','parent_client_order_id','strategy_digest',
                'native_observation_sequence','native_observation_digest','entry_order','fills','market',
                'decision','evaluated_at_ns'} or evidence['schema']!='orbis.grvt.testnet.filled.exit.evidence.v1'
                or evidence['parent_client_order_id']!=parent): raise ValueError('evidence shape')
        group=db.get(GrvtTestnetBracketGroup,parent); plan=_verified(db,group)
        snapshot=verified_bracket_strategy(db,db.get(GrvtTestnetBracketStrategy,parent))
        latest_bracket_observation(db,group,plan)
        sequence=evidence['native_observation_sequence']
        if type(sequence) is not int or sequence<1: raise ValueError('native sequence')
        native=db.scalar(select(GrvtTestnetBracketObservation).where(
            GrvtTestnetBracketObservation.parent_client_order_id==parent,
            GrvtTestnetBracketObservation.sequence==sequence))
        if (native is None or native.payload_digest!=evidence['native_observation_digest']
                or _encode(snapshot)[1]!=evidence['strategy_digest']): raise ValueError('changed source')
        state=json.loads(native.payload_json)['state']; orders=state['orders']
        order=GrvtTestnetOrderObservation(**evidence['entry_order'])
        if (order.sub_account_id!=plan['sub_account_id'] or order.instrument!=plan['instrument']
                or order.side!=plan['legs'][0]['side'] or order.limit_price!=plan['legs'][0]['limit_price']
                or order.execution_unverified is not True
                or any(getattr(order,key)!=orders[0][key] for key in
                    ('order_id','client_order_id','status','size','traded_size','create_time_ns','update_time_ns'))
                or any(Decimal(leg['traded_size'])!=0 for leg in orders[1:])):
            raise ValueError('incompatible native entry')
        at=evidence['evaluated_at_ns']; _stamp(at)
        if state['observed_at_ns']>at: raise ValueError('future native observation')
        rows=evidence['fills']
        if not isinstance(rows,list) or not 1<=len(rows)<=10000: raise ValueError('fill coverage')
        audit=audit_testnet_fills(_ArchivedFills(rows),order,now_ns=at)
        if audit.other_fills: raise ValueError('other fill activity')
        market=evidence['market']
        if set(market)!={'history','history_digest','mark'}: raise ValueError('market shape')
        history=market['history']; step=INTERVALS[snapshot['timeframe']]
        mark=GrvtExitMark(**{**market['mark'],'price':_decimal(market['mark']['price'])})
        if (set(history)!={'source','symbol','timeframe','start_t','end_t','candles'}
                or history['source']!='GRVT_TESTNET_TRADE_AND_MARK'
                or history['symbol']!=plan['instrument'].replace('_USDT_Perp','/USDT:PERP')
                or history['timeframe']!=snapshot['timeframe']
                or history['start_t']!=snapshot['history_start_t']
                or type(history['start_t']) is not int or type(history['end_t']) is not int
                or history['end_t']!=mark.event_time_ns//1_000_000_000//step*step
                or _encode(history)[1]!=market['history_digest']): raise ValueError('history scope')
        bars,gaps=validate_candles(history['candles'],snapshot['timeframe'],now=history['end_t'])
        if gaps or bars[0].t!=history['start_t'] or bars[-1].t+step!=history['end_t']:
            raise ValueError('history coverage')
        decision=evaluate_filled_strategy_exit(snapshot['definition'],timeframe=snapshot['timeframe'],
            engine_version=snapshot['engine_version'],order=order,audit=audit,candles=bars,mark=mark,now_ns=at)
        if _safe(asdict(decision))!=evidence['decision']: raise ValueError('changed decision')
        return evidence
    except Exception:
        raise GrvtTestnetBracketError('Evidencia de salida sobre fills no reproducible') from None


def verified_filled_close(db: Session,row: GrvtTestnetStrategyClose,payload: dict) -> FilledCloseRequest:
    try:
        if (set(payload)!={'schema','parent_client_order_id','plan_digest','evidence','created_at_ns','deadline_ns','max_attempts'}
                or payload['schema']!='orbis.grvt.testnet.strategy.close.v2'
                or payload['parent_client_order_id']!=row.parent_client_order_id
                or _encode(payload)!=(row.payload_json,row.payload_digest)):
            raise ValueError('request shape')
        group=db.get(GrvtTestnetBracketGroup,row.parent_client_order_id)
        proof=verify_filled_exit_evidence(db,row.parent_client_order_id,payload['evidence'])
        for key in ('created_at_ns','deadline_ns'): _stamp(payload[key])
        if (payload['plan_digest']!=group.payload_digest or proof['decision']['action']!='CLOSE'
                or row.close_client_order_id!=filled_close_id(row.parent_client_order_id,proof)
                or not 0<=payload['created_at_ns']-proof['evaluated_at_ns']<=5_000_000_000
                or payload['created_at_ns']-proof['market']['mark']['event_time_ns']>5_000_000_000
                or not 0<payload['deadline_ns']-payload['created_at_ns']<=300_000_000_000
                or type(payload['max_attempts']) is not int or not 1<=payload['max_attempts']<=3):
            raise ValueError('request scope')
        return cast(FilledCloseRequest,payload)
    except Exception:
        raise GrvtTestnetBracketError('Solicitud de cierre sobre fills no verificable') from None


def request_filled_strategy_close(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,*,
        max_attempts: int,deadline_ns: int,public_post: Callable[[str,dict],httpx.Response],
        clock_ns: Callable[[],int]=time.time_ns,prepared_evidence: dict|None=None) -> FilledCloseRequest:
    from app.live_execution.grvt_testnet_strategy_close import _exclusive,read_strategy_close
    from app.live_execution.grvt_bracket_exit_review import collect_bracket_exit_evidence
    now=clock_ns(); _stamp(now)
    if (type(max_attempts) is not int or not 1<=max_attempts<=3 or type(deadline_ns) is not int
            or not 0<deadline_ns-now<=300_000_000_000): raise GrvtTestnetBracketError('Límites de cierre inválidos')
    with _exclusive(vault):
        existing=read_strategy_close(vault,parent)
        if existing is not None:
            if existing['schema']!='orbis.grvt.testnet.strategy.close.v2':
                raise GrvtTestnetBracketError('El grupo ya tiene otro origen de cierre')
            return existing
    if prepared_evidence is None:
        _,evidence=collect_bracket_exit_evidence(vault,client,parent,public_post=public_post,clock_ns=clock_ns)
    else: evidence=prepared_evidence
    with _exclusive(vault),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            if db.get(GrvtTestnetStrategyClose,parent) is not None: raise ValueError('close concurrently registered')
            group=db.get(GrvtTestnetBracketGroup,parent); plan=_verified(db,group)
            latest=latest_bracket_observation(db,group,plan)
            origin=db.scalar(select(GrvtTestnetBracketObservation).where(
                GrvtTestnetBracketObservation.parent_client_order_id==parent,
                GrvtTestnetBracketObservation.sequence==evidence['native_observation_sequence']))
            if latest['orders']!=json.loads(origin.payload_json)['state']['orders']:
                raise ValueError('native group changed after decision')
            at=clock_ns()
            payload: FilledCloseRequest={'schema':'orbis.grvt.testnet.strategy.close.v2',
                'parent_client_order_id':parent,'plan_digest':group.payload_digest,'evidence':evidence,
                'created_at_ns':at,'deadline_ns':deadline_ns,'max_attempts':max_attempts}
            encoded,digest=_encode(payload)
            row=GrvtTestnetStrategyClose(parent_client_order_id=parent,close_client_order_id=filled_close_id(parent,evidence),
                payload_json=encoded,payload_digest=digest)
            db.add(row); db.flush(); verified_filled_close(db,row,payload)
            from app.live_execution.grvt_stop_control import record_testnet_stop
            record_testnet_stop(db,group.sub_account_id,now=_stamp(at),reason='AUTO_EXIT',parent_client_order_id=parent)
            db.commit(); return payload
        except Exception:
            db.rollback()
            raise GrvtTestnetBracketError('No hay una salida sobre fills autorizable') from None
