"""Cancelación única de grupo OTOCO TESTNET; no ejecuta un cierre de posición."""

import json
import time
from typing import Callable

from sqlalchemy import select

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError,BRACKET_FIELDS
from app.live_execution.grvt_testnet_bracket_journal import _verified,read_bracket
from app.live_execution.grvt_testnet_bracket_observation import latest_bracket_observation,observe_bracket_orders
from app.live_execution.grvt_testnet_oco import _encode,_stamp
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.live_execution.grvt_testnet_reconciliation import _ns
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetBracketClaim,GrvtTestnetBracketCancel,GrvtTestnetBracketObservation,GrvtTestnetStop
from app.portfolio.vault import RealVault


def verified_bracket_cancel(db,group,plan) -> dict | None:
    row=db.get(GrvtTestnetBracketCancel,group.parent_client_order_id)
    if row is None: return None
    try:
        p=json.loads(row.payload_json)
        if (_encode(p)!=(row.payload_json,row.payload_digest)
                or set(p)!={'schema','parent_client_order_id','plan_digest','observation_sequence','observation_digest',
                    'target_client_order_id','claimed_at_ns','stop_at_ns'}
                or p['schema']!='orbis.grvt.testnet.bracket.cancel.v1'
                or p['parent_client_order_id']!=group.parent_client_order_id or p['plan_digest']!=group.payload_digest
                or group.status not in ('POST_CLAIMED','ACKNOWLEDGED')
                or db.get(GrvtTestnetBracketClaim,group.parent_client_order_id) is None
                or any(type(p[key]) is not int for key in ('observation_sequence','claimed_at_ns','stop_at_ns'))):
            raise ValueError('invalid cancel contract')
        _stamp(p['claimed_at_ns']); _stamp(p['stop_at_ns'])
        latest_bracket_observation(db,group,plan)
        record=db.scalar(select(GrvtTestnetBracketObservation).where(
            GrvtTestnetBracketObservation.parent_client_order_id==group.parent_client_order_id,
            GrvtTestnetBracketObservation.sequence==p['observation_sequence']))
        if record is None or record.payload_digest!=p['observation_digest']:
            raise ValueError('missing cancellation observation')
        observed=json.loads(record.payload_json)['state']
        targets=[leg['client_order_id'] for leg in observed['orders'] if leg['status'] in ('OPEN','PENDING')]
        stop=db.get(GrvtTestnetStop,group.sub_account_id)
        from app.live_execution.grvt_stop_control import stop_was_active
        if (p['target_client_order_id'] not in targets or not 0<=p['claimed_at_ns']-observed['observed_at_ns']<=5_000_000_000
                or stop is None or not stop_was_active(db,group.sub_account_id,p['stop_at_ns']) or not p['stop_at_ns']<=p['claimed_at_ns']
                or _ns(stop.updated_at)<p['stop_at_ns']):
            raise ValueError('unverified cancellation scope')
        status,received='CLAIMED',None
        if row.ack_json is not None or row.ack_digest is not None:
            ack=json.loads(row.ack_json)
            if (_encode(ack)!=(row.ack_json,row.ack_digest)
                    or set(ack)!={'cancel_digest','received_at_ns','acknowledgement_received'}
                    or ack['cancel_digest']!=row.payload_digest or ack['acknowledgement_received'] is not True
                    or type(ack['received_at_ns']) is not int or ack['received_at_ns']<p['claimed_at_ns']):
                raise ValueError('unverified cancellation acknowledgement')
            _stamp(ack['received_at_ns']); status,received='ACKNOWLEDGED',ack['received_at_ns']
        return {**p,'status':status,'acknowledgement_received_at_ns':received,
            'financially_reconciled':False,'can_submit':False,'position_closed':False}
    except Exception:
        raise GrvtTestnetBracketError('Cancelación OTOCO no verificable') from None


def cancel_bracket_once(vault: RealVault,client: GrvtTestnetOrderClient,parent_client_order_id: str,*,
                         acknowledge_unprotected_position: bool,clock_ns: Callable[[],int]=time.time_ns) -> dict:
    if acknowledge_unprotected_position is not True:
        raise GrvtTestnetBracketError('Confirma que cancelar retira protecciones y no cierra la posición')
    plan=read_bracket(vault,parent_client_order_id)
    if plan['cancel_attempt'] is not None:
        raise GrvtTestnetBracketError('Cancelación OTOCO ya consumida; consulta los tres IDs')
    with vault.session() as db:
        stop=db.get(GrvtTestnetStop,plan['sub_account_id'])
        if stop is None or not stop.stopped:
            raise GrvtTestnetBracketError('Detén la subcuenta antes de cancelar OTOCO')
    observed=observe_bracket_orders(vault,client,parent_client_order_id,clock_ns=clock_ns)
    targets=[row['client_order_id'] for row in observed['orders'] if row['status'] in ('OPEN','PENDING')]
    if not targets: return read_bracket(vault,parent_client_order_id)
    claimed=clock_ns(); _stamp(claimed)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group=db.get(GrvtTestnetBracketGroup,parent_client_order_id); p=_verified(db,group)
            claim=db.get(GrvtTestnetBracketClaim,parent_client_order_id)
            stop=db.get(GrvtTestnetStop,group.sub_account_id)
            current=latest_bracket_observation(db,group,p)
            if (group.payload_digest!=_encode({key:plan[key] for key in BRACKET_FIELDS})[1]
                    or current!=observed or stop is None or not stop.stopped
                    or db.get(GrvtTestnetBracketCancel,parent_client_order_id) is not None):
                raise GrvtTestnetBracketError('Estado OTOCO cambiado antes de cancelar')
            payload={'schema':'orbis.grvt.testnet.bracket.cancel.v1','parent_client_order_id':parent_client_order_id,
                'plan_digest':group.payload_digest,'observation_sequence':claim.observation_count,
                'observation_digest':claim.observation_head_digest,'target_client_order_id':targets[0],
                'claimed_at_ns':claimed,'stop_at_ns':_ns(stop.updated_at)}
            encoded,digest=_encode(payload)
            db.add(GrvtTestnetBracketCancel(parent_client_order_id=parent_client_order_id,payload_json=encoded,payload_digest=digest))
            db.flush(); verified_bracket_cancel(db,group,p); db.commit()
        except Exception:
            db.rollback(); raise
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group=db.get(GrvtTestnetBracketGroup,parent_client_order_id); p=_verified(db,group)
            attempt=verified_bracket_cancel(db,group,p)
            claim=db.get(GrvtTestnetBracketClaim,parent_client_order_id)
            now=clock_ns(); _stamp(now)
            stop=db.get(GrvtTestnetStop,group.sub_account_id)
            if (attempt['status']!='CLAIMED' or not 0<=now-claimed<=5_000_000_000
                    or stop is None or not stop.stopped
                    or claim.observation_head_digest!=attempt['observation_digest']):
                raise GrvtTestnetBracketError('Cancelación OTOCO sin revisión final; consulta los tres IDs')
            client.cancel_bracket_by_client_id(group.sub_account_id,attempt['target_client_order_id'])
            received=clock_ns()
            if type(received) is not int or received<now: received=now
            row=db.get(GrvtTestnetBracketCancel,parent_client_order_id)
            row.ack_json,row.ack_digest=_encode({'cancel_digest':row.payload_digest,'received_at_ns':received,'acknowledgement_received':True})
            db.flush(); verified_bracket_cancel(db,group,p); db.commit()
        except Exception:
            db.rollback(); raise
    observe_bracket_orders(vault,client,parent_client_order_id,clock_ns=clock_ns)
    return read_bracket(vault,parent_client_order_id)
