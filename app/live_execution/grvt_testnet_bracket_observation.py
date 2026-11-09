"""Recupera tres contratos desde el venue; guarda sólo estados y huellas."""

from decimal import Decimal
import json
import re
import time
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from app.integrations.grvt_readonly import _order
from app.live_execution.grvt_testnet_bracket import (
    GrvtTestnetBracketError,validate_testnet_bracket,canonical_bracket_order,summarise_bracket_state,
    verify_testnet_bracket,native_partial_role,native_recovery_reason,
)
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_oco import _encode,_stamp,_number
from app.live_execution.grvt_testnet_oco_observation import _ns,_TERMINAL
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits,GrvtTestnetOrderClient,_ORDER_FIELDS
from app.live_execution.grvt_testnet_protection import GrvtTestnetTrigger
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetBracketClaim,GrvtTestnetBracketObservation,GrvtTestnetStop
from app.portfolio.vault import RealVault


_FIELDS={'role','client_order_id','order_id','status','size','traded_size','create_time_ns','update_time_ns'}


def _check_states(plan,states,at,previous):
    if not isinstance(states,list) or len(states)!=3: raise ValueError('three states required')
    for index,(actual,expected) in enumerate(zip(states,plan['legs'])):
        if (not isinstance(actual,dict) or set(actual)!=_FIELDS
                or any(actual[key]!=expected[key] for key in ('role','client_order_id'))
                or _number(actual['size'])!=_number(expected['size'])
                or not isinstance(actual['order_id'],str) or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',actual['order_id'])
                or actual['status'] not in {'PENDING','OPEN',*_TERMINAL}
                or not isinstance(actual['traded_size'],str) or not re.fullmatch(r'[0-9]{1,30}(?:\.[0-9]{1,18})?',actual['traded_size'])):
            raise ValueError('invalid state')
        created,updated=_ns(actual['create_time_ns']),_ns(actual['update_time_ns'])
        if (Decimal(actual['traded_size'])>Decimal(actual['size']) or created<plan['created_at_ns']-1_000_000_000
                or not created<=updated<=at+2_000_000_000):
            raise ValueError('invalid observation quantities or time')
        if previous is not None:
            before=previous['orders'][index]
            if (before['order_id']!=actual['order_id'] or before['create_time_ns']!=actual['create_time_ns']
                    or _ns(before['update_time_ns'])>updated or Decimal(before['traded_size'])>Decimal(actual['traded_size'])
                    or before['status'] in _TERMINAL and before['status']!=actual['status']
                    or before['status']=='OPEN' and actual['status']=='PENDING'):
                raise ValueError('state regressed')
    if len({row['order_id'] for row in states})!=3: raise ValueError('duplicate exchange ID')


def latest_bracket_observation(db,group,plan):
    try:
        rows=list(db.scalars(select(GrvtTestnetBracketObservation).where(
            GrvtTestnetBracketObservation.parent_client_order_id==group.parent_client_order_id
        ).order_by(GrvtTestnetBracketObservation.sequence)))
        claim=db.get(GrvtTestnetBracketClaim,group.parent_client_order_id)
        if claim is None:
            if rows: raise ValueError('observations before claim')
            return None
        if len(rows)!=claim.observation_count: raise ValueError('observation chain truncated')
        previous,digest=None,None
        for seq,row in enumerate(rows,1):
            value=json.loads(row.payload_json)
            if (row.sequence!=seq or _encode(value)!=(row.payload_json,row.payload_digest)
                    or set(value)!={'schema','parent_client_order_id','plan_digest','sequence','previous_digest','state'}
                    or value['schema']!='orbis.grvt.testnet.bracket.observation.v1'
                    or value['parent_client_order_id']!=group.parent_client_order_id
                    or value['plan_digest']!=group.payload_digest or type(value['sequence']) is not int
                    or value['sequence']!=seq or value['previous_digest']!=digest):
                raise ValueError('invalid observation chain')
            state=value['state']; at=state['observed_at_ns']; _stamp(at)
            if (at<int(claim.claimed_at_ns) or previous is not None and at<previous['observed_at_ns']):
                raise ValueError('observation clock regressed')
            _check_states(plan,state['orders'],at,previous)
            if state!=summarise_bracket_state(state['orders'],observed_at_ns=at,exit_expiration_ns=plan['legs'][1]['expiration_ns']):
                raise ValueError('misleading stored summary')
            if claim.ack_json is not None and any(actual['order_id']!=expected['order_id'] for actual,expected in
                    zip(state['orders'],json.loads(claim.ack_json)['orders'])):
                raise ValueError('changed acknowledged exchange ID')
            previous,digest=state,row.payload_digest
        if digest!=claim.observation_head_digest: raise ValueError('changed observation head')
        return previous
    except Exception:
        raise GrvtTestnetBracketError('Cadena de observaciones OTOCO no verificable') from None


def observe_bracket_orders(vault: RealVault,client: GrvtTestnetOrderClient,parent_client_order_id: str,*,
                           clock_ns: Callable[[],int]=time.time_ns) -> dict:
    with vault.session() as db:
        group=db.get(GrvtTestnetBracketGroup,parent_client_order_id)
        if group is None or group.status not in ('POST_CLAIMED','ACKNOWLEDGED'):
            raise GrvtTestnetBracketError('Entrada OTOCO sin concesión para consultar')
        plan=_verified(db,group); plan_digest=group.payload_digest
        latest_bracket_observation(db,group,plan)
    try:
        terms=plan['instrument_terms']
        instrument=GrvtTestnetInstrument(plan['instrument'],int(terms['asset_id']),terms['base_decimals'],
            _number(terms['tick_size']),_number(terms['min_size']),_number(terms['min_notional']))
        entry_limits=GrvtTestnetLimits(plan['sub_account_id'],plan['instrument'],_number(plan['entry_cap']))
        exit_limits=GrvtTestnetLimits(plan['sub_account_id'],plan['instrument'],_number(plan['exit_cap']))
        last=clock_ns(); _stamp(last)
        raw_rows,contracts,times=[],[],[]
        for leg in plan['legs']:
            raw=client._lookup_order_payload(plan['sub_account_id'],leg['client_order_id'])
            candidate={key:raw[key] for key in _ORDER_FIELDS}
            keys=('client_order_id',) if leg['role']=='ENTRY' else ('client_order_id','trigger')
            if leg['role']=='ENTRY' and raw['metadata'].get('trigger') is not None:
                raise ValueError('entry now conditional')
            candidate['metadata']={key:raw['metadata'][key] for key in keys}
            at=clock_ns(); _stamp(at)
            if at<last: raise ValueError('clock regressed')
            last=at; times.append(at); raw_rows.append(raw); contracts.append(candidate)
        # Tipos antes de canonicalizar; todas las firmas contra términos guardados.
        validate_testnet_bracket(contracts,entry_limits,exit_limits,now_ns=plan['created_at_ns'])
        contracts=[canonical_bracket_order(order) for order in contracts]
        if any(_encode(order)[1]!=leg['wire_digest'] for order,leg in zip(contracts,plan['legs'])):
            raise ValueError('original contract changed')
        triggers=tuple(GrvtTestnetTrigger(leg['role'],_number(leg['trigger_price'])) for leg in plan['legs'][1:])
        verify_testnet_bracket(contracts,entry_limits,exit_limits,instrument,expected_triggers=triggers,now_ns=plan['created_at_ns'])
        states=[]
        for raw,contract,leg,at in zip(raw_rows,contracts,plan['legs'],times):
            normalised={**raw,**contract,'metadata':{**raw['metadata'],**contract['metadata']}}
            observed=_order(normalised,plan['sub_account_id'],open_only=False,now_ns=at)
            actual=observed['legs'][0]
            states.append({'role':leg['role'],'client_order_id':observed['client_order_id'],'order_id':observed['order_id'],
                'status':observed['status'],'size':actual['size'],'traded_size':actual['traded_size'],
                'create_time_ns':observed['create_time_ns'],'update_time_ns':observed['update_time_ns']})
        at=clock_ns(); _stamp(at)
        if at<last: raise ValueError('clock regressed')
    except Exception:
        raise GrvtTestnetBracketError('No se verificaron los tres IDs; conserva el bloqueo y no reenvíes') from None
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group=db.get(GrvtTestnetBracketGroup,parent_client_order_id); p=_verified(db,group)
            if group.payload_digest!=plan_digest: raise ValueError('changed plan')
            claim=db.get(GrvtTestnetBracketClaim,parent_client_order_id)
            previous=latest_bracket_observation(db,group,p)
            if at<int(claim.claimed_at_ns) or previous is not None and at<previous['observed_at_ns']:
                raise ValueError('clock before last observation')
            _check_states(p,states,at,previous)
            state=summarise_bracket_state(states,observed_at_ns=at,exit_expiration_ns=p['legs'][1]['expiration_ns'])
            payload={'schema':'orbis.grvt.testnet.bracket.observation.v1','parent_client_order_id':parent_client_order_id,
                'plan_digest':plan_digest,'sequence':claim.observation_count+1,
                'previous_digest':claim.observation_head_digest,'state':state}
            encoded,digest=_encode(payload)
            db.add(GrvtTestnetBracketObservation(id=str(uuid4()),parent_client_order_id=parent_client_order_id,
                sequence=payload['sequence'],payload_json=encoded,payload_digest=digest))
            claim.observation_count+=1; claim.observation_head_digest=digest
            if state['needs_review']:
                from app.live_execution.grvt_stop_control import record_testnet_stop
                controlled=False
                partial=native_partial_role(state) is not None
                recovery=native_recovery_reason(state) is not None
                if (state['review_reasons']==['RESIDUAL_AFTER_TERMINAL_EXITS'] and state['entry_fully_filled']
                        and all(item['status']=='CANCELLED' and Decimal(item['traded_size'])==0 for item in state['orders'][1:])):
                    from app.portfolio.models import GrvtTestnetStrategyClose
                    from app.live_execution.grvt_testnet_strategy_close import _request
                    from app.live_execution.grvt_testnet_bracket_cancel import verified_bracket_cancel
                    request=db.get(GrvtTestnetStrategyClose,parent_client_order_id)
                    if request is not None:
                        close=_request(db,request)
                        cancellation=verified_bracket_cancel(db,group,p)
                        controlled=bool(cancellation and close['created_at_ns']<=cancellation['claimed_at_ns']<=at)
                record_testnet_stop(db,group.sub_account_id,now=_stamp(at),
                    reason='AUTO_EXIT' if controlled else 'PARTIAL_EXIT' if partial else 'NATIVE_RECOVERY' if recovery else 'REVIEW',
                    parent_client_order_id=parent_client_order_id if controlled or partial or recovery else None)
            db.flush(); result=latest_bracket_observation(db,group,p); db.commit(); return result
        except Exception:
            db.rollback()
            raise GrvtTestnetBracketError('Estados incompatibles con la cadena OTOCO guardada') from None
