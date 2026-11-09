"""Observación MAINNET de los tres contratos originales, sin firma persistida."""

from collections.abc import Callable
from decimal import Decimal
import json
import re
import time

from sqlalchemy import select

from app.integrations.grvt_readonly import _order
from app.live_execution.grvt_bracket_state import summarise_bracket_state
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,ROLES,unsigned_mainnet_bracket,verify_mainnet_bracket
from app.live_execution.grvt_mainnet_journal import (read_mainnet_bracket,encode,_draft,_instrument,_control,signer_ref)
from app.live_execution.grvt_mainnet_order_history import lookup_recovery_orders
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient
from app.live_execution.grvt_mainnet_remote_order import remote_contract
from app.live_execution.grvt_mainnet_preflight import canonical,number
from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetObservation


_FIELDS={'role','client_order_id','order_id','status','size','traded_size','create_time_ns','update_time_ns'}
_TERMINAL={'FILLED','CANCELLED','REJECTED'}


def _check_states(rows,templates,*,at: int,issued: int,previous: dict|None,ack: dict|None):
    if not isinstance(rows,list) or len(rows)!=3: raise ValueError('three mainnet states required')
    for index,(actual,template,role) in enumerate(zip(rows,templates,ROLES)):
        if (not isinstance(actual,dict) or set(actual)!=_FIELDS or actual['role']!=role
                or actual['client_order_id']!=template['metadata']['client_order_id']
                or actual['status'] not in ('PENDING','OPEN',*_TERMINAL)
                or not isinstance(actual['order_id'],str) or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',actual['order_id'])
                or any(not isinstance(actual[key],str) or not re.fullmatch(r'[0-9]{1,30}(?:\.[0-9]{1,18})?',actual[key]) for key in ('size','traded_size'))
                or Decimal(actual['size'])!=Decimal(template['legs'][0]['size'])
                or not Decimal(0)<=Decimal(actual['traded_size'])<=Decimal(actual['size'])
                or any(not isinstance(actual[key],str) or not re.fullmatch(r'[0-9]{1,20}',actual[key]) for key in ('create_time_ns','update_time_ns'))
                or not issued-1_000_000_000<=int(actual['create_time_ns'])<=int(actual['update_time_ns'])<=at+2_000_000_000
                or ack is not None and ack['orders'][index]['order_id']!=actual['order_id']):
            raise ValueError('mainnet state scope')
        if previous is not None:
            old=previous['orders'][index]
            if (old['order_id']!=actual['order_id'] or old['create_time_ns']!=actual['create_time_ns']
                    or int(old['update_time_ns'])>int(actual['update_time_ns'])
                    or Decimal(old['traded_size'])>Decimal(actual['traded_size'])
                    or old['status'] in _TERMINAL and old['status']!=actual['status']
                    or old['status']=='OPEN' and actual['status']=='PENDING'):
                raise ValueError('mainnet state regressed')
    if len({row['order_id'] for row in rows})!=3: raise ValueError('duplicated mainnet exchange identity')


def verified_mainnet_observations(db,row,claim,ack) -> list[dict]:
    draft=_draft(claim['draft']); instrument=_instrument(claim['instrument'])
    templates=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=row.parent_client_order_id,
        nonces=tuple(claim['nonces']),issued_at_ns=claim['issued_at_ns'])
    previous=None; digest=None; result=[]
    for record in db.scalars(select(GrvtMainnetObservation).where(
            GrvtMainnetObservation.parent_client_order_id==row.parent_client_order_id).order_by(GrvtMainnetObservation.sequence)):
        value=json.loads(record.payload_json); sequence=len(result)+1
        if (set(value)!={'schema','network','parent_client_order_id','claim_digest','sequence','previous_digest','state'}
                or value['schema']!='orbis.grvt.mainnet.bracket.observation.v1' or value['network']!='MAINNET'
                or value['parent_client_order_id']!=row.parent_client_order_id or value['claim_digest']!=row.payload_digest
                or type(value['sequence']) is not int or value['sequence']!=sequence or record.sequence!=sequence
                or value['previous_digest']!=digest or encode(value)!=(record.payload_json,record.payload_digest)):
            raise ValueError('mainnet observation chain')
        state=value['state']; at=state['observed_at_ns']
        if (type(at) is not int or not claim['claimed_at_ns']<=at<2**63
                or previous is not None and at<previous['observed_at_ns']): raise ValueError('mainnet observation time')
        _check_states(state['orders'],templates,at=at,issued=claim['issued_at_ns'],previous=previous,ack=ack)
        expected=summarise_bracket_state(state['orders'],observed_at_ns=at,exit_expiration_ns=int(templates[1]['signature']['expiration']))
        if encode(expected)!=encode(state): raise ValueError('mainnet observation summary changed')
        previous=state; digest=record.payload_digest; result.append(value)
    if len(result)!=row.observation_count or digest!=row.observation_head: raise ValueError('mainnet observation tail truncated')
    return result


def observe_mainnet_bracket(vault,parent: str,*,client_factory: Callable[[],GrvtMainnetClient],
        require_permission: Callable[[],None],clock_ns: Callable[[],int]=time.time_ns) -> dict:
    generation=vault.session_generation
    if generation is None: raise GrvtMainnetError('Bóveda bloqueada')
    last=clock_ns(); start=last
    def checked():
        nonlocal last
        require_permission(); at=clock_ns()
        if (vault.session_generation!=generation or type(at) is not int or type(last) is not int
                or not 0<last<=at<=start+120_000_000_000 or at>=2**63):
            raise GrvtMainnetError('Consulta MAINNET revocada, vencida o con reloj regresado')
        last=at; return at
    checked(); saved=read_mainnet_bracket(vault,parent); claim=saved['claim']
    if saved.get('reconciliation') is not None:
        raise GrvtMainnetError('Grupo MAINNET conciliado; consulta su recibo archivado')
    draft=_draft(claim['draft']); instrument=_instrument(claim['instrument'])
    with vault.session() as db:
        salt=_control(db,draft.sub_account_id).identity_salt
    try:
        client=client_factory(); checked()
        if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=draft.sub_account_id
                or client.identity_ref(salt)!=claim['account_ref']): raise ValueError('account scope')
        templates=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=parent,
            nonces=tuple(claim['nonces']),issued_at_ns=claim['issued_at_ns'])
        began=checked()
        windows={template['metadata']['client_order_id']:
            (claim['issued_at_ns']-1_000_000_000,int(template['signature']['expiration'])) for template in templates}
        received=lookup_recovery_orders(client,draft.sub_account_id,draft.instrument,windows=windows,checked=checked)
        raw_rows=[received[cid] for cid in windows]
        wire=[remote_contract(raw,template) for raw,template in zip(raw_rows,templates)]
        recovered=verify_mainnet_bracket(wire,draft,instrument,issued_at_ns=claim['issued_at_ns'],now_ns=claim['issued_at_ns'])
        if ([encode(order)[1] for order in wire]!=claim['wire_digests'] or signer_ref(salt,recovered)!=claim['signer_ref']):
            raise ValueError('original mainnet contract changed')
        states=[]
        for role,raw in zip(ROLES,raw_rows):
            observed=_order(raw,draft.sub_account_id,open_only=False,now_ns=checked()); leg=observed['legs'][0]
            states.append({'role':role,'client_order_id':observed['client_order_id'],'order_id':observed['order_id'],
                'status':observed['status'],'size':canonical(number(leg['size'])),'traded_size':canonical(number(leg['traded_size'])),
                'create_time_ns':observed['create_time_ns'],'update_time_ns':observed['update_time_ns']})
        at=checked()
        if at-began>5_000_000_000: raise ValueError('mainnet state collection took too long')
        with vault.guard_session_generation(generation),vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                row=db.get(GrvtMainnetBracket,parent)
                from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
                current=verified_mainnet_bracket(db,row)
                if row.payload_digest!=encode(claim)[1]: raise ValueError('changed mainnet source')
                previous=current['last_observation']['state'] if current['last_observation'] else None
                _check_states(states,templates,at=at,issued=claim['issued_at_ns'],previous=previous,ack=current['acknowledgement'])
                state=summarise_bracket_state(states,observed_at_ns=at,exit_expiration_ns=int(templates[1]['signature']['expiration']))
                payload={'schema':'orbis.grvt.mainnet.bracket.observation.v1','network':'MAINNET','parent_client_order_id':parent,
                    'claim_digest':row.payload_digest,'sequence':row.observation_count+1,'previous_digest':row.observation_head,'state':state}
                text,digest=encode(payload)
                db.add(GrvtMainnetObservation(parent_client_order_id=parent,sequence=payload['sequence'],payload_json=text,payload_digest=digest))
                row.observation_count+=1; row.observation_head=digest
                if row.status=='POST_CLAIMED': row.status='OBSERVED'
                if state['needs_review']:
                    control=_control(db,draft.sub_account_id); was_stopped=control.stopped
                    control.stopped=True; control.revision+=1
                    from app.live_execution.grvt_mainnet_stop_origin import record_stop
                    record_stop(db,control,'OBSERVATION',parent=parent,was_stopped=was_stopped)
                checked(); db.flush(); verified_mainnet_bracket(db,row); db.commit(); return payload
            except Exception:
                db.rollback(); raise
    except Exception:
        raise GrvtMainnetError('No se verificaron los tres contratos MAINNET; conserva el bloqueo y no reenvíes') from None
