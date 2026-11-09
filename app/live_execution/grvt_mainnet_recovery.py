"""Recuperación explícita de un grupo MAINNET. El diario no es autorización.

La cancelación se confirma localmente antes del POST y nunca se reenvía.
El acuse no acredita posición plana: se siguen leyendo los tres contratos.
"""

from collections.abc import Callable
from decimal import Decimal
import json
import time

from sqlalchemy import select

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_finance import stamp
from app.live_execution.grvt_mainnet_journal import encode,read_mainnet_bracket
from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient
from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetRecovery,GrvtMainnetRecoveryEvent,GrvtMainnetReduction,GrvtMainnetLeg


_FIELDS={'schema','network','parent_client_order_id','claim_digest','started_at_ns','deadline_ns',
    'max_attempts','exit_cap','max_spread_bps','max_slippage_bps','authorization'}


def recovery_schema(db,*,required=False) -> bool:
    version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    if version<52:
        if required:raise GrvtMainnetError('Actualiza el esquema de recuperación MAINNET')
        return False
    names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'grvt_mainnet_recoveries','grvt_mainnet_recovery_events','grvt_mainnet_reductions'}<=names:
        raise GrvtMainnetError('Diario de recuperación MAINNET truncado')
    return True


def verified_recovery(db,group,claim,observations) -> dict|None:
    if not recovery_schema(db):return None
    record=db.get(GrvtMainnetRecovery,group.parent_client_order_id)
    if record is None:return None
    value=json.loads(record.payload_json)
    if (set(value)!=_FIELDS or value['schema']!='orbis.grvt.mainnet.recovery.v1' or value['network']!='MAINNET'
            or value['parent_client_order_id']!=group.parent_client_order_id or value['claim_digest']!=group.payload_digest
            or encode(value)!=(record.payload_json,record.payload_digest) or value['authorization'] is not False
            or type(value['max_attempts']) is not int or not 1<=value['max_attempts']<=3
            or not claim['claimed_at_ns']<=stamp(value['started_at_ns'])
            or not 30_000_000_000<=stamp(value['deadline_ns'])-value['started_at_ns']<=300_000_000_000
            or any(value[key]!=claim['draft'][key] for key in ('exit_cap','max_spread_bps','max_slippage_bps'))):
        raise ValueError('recovery terms changed')
    initial=value;events=[];previous=None;cancelled=False;acknowledged=False;last_at=value['started_at_ns'];reductions=[]
    for row in db.scalars(select(GrvtMainnetRecoveryEvent).where(
            GrvtMainnetRecoveryEvent.parent_client_order_id==group.parent_client_order_id).order_by(GrvtMainnetRecoveryEvent.sequence)):
        event=json.loads(row.payload_json)
        if (set(event)!={'schema','parent_client_order_id','request_digest','sequence','previous_digest','type','occurred_at_ns','details'}
                or event['schema']!='orbis.grvt.mainnet.recovery.event.v1'
                or event['parent_client_order_id']!=group.parent_client_order_id or event['request_digest']!=record.payload_digest
                or type(event['sequence']) is not int or event['sequence']!=len(events)+1 or row.sequence!=event['sequence']
                or event['previous_digest']!=previous or encode(event)!=(row.payload_json,row.payload_digest)
                or not last_at<=stamp(event['occurred_at_ns'])):
            raise ValueError('recovery event chain')
        details=event['details']
        if event['type']=='RECOVERY_GRANT':
            from app.live_execution.grvt_mainnet_recovery_grants import verified_grant
            if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<55:raise ValueError('unsupported recovery grant')
            if details.get('schema')=='orbis.grvt.mainnet.recovery.grant.v2' and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<57:raise ValueError('unsupported exit cap review')
            value=verified_grant(value,details,sequence=event['sequence'],at_ns=event['occurred_at_ns'],reductions=reductions)
            cancelled=False;acknowledged=False
        elif event['type']=='CANCEL_CLAIM':
            if (cancelled or set(details)!={'observation_sequence','observation_digest'}
                    or not event['occurred_at_ns']<value['deadline_ns'] or type(details['observation_sequence']) is not int
                    or not 1<=details['observation_sequence']<=len(observations)):
                raise ValueError('cancel claim source')
            source=observations[details['observation_sequence']-1]
            if (encode(source)[1]!=details['observation_digest'] or source['state']['observed_at_ns']>event['occurred_at_ns']
                    or not any(order['status'] in ('OPEN','PENDING') for order in source['state']['orders'])):
                raise ValueError('cancel source changed')
            cancelled=True
        elif event['type']=='CANCEL_ACK':
            if not cancelled or acknowledged or details!={'ack':True}:raise ValueError('cancel acknowledgement source')
            acknowledged=True
        elif event['type']=='REDUCTION_SNAPSHOT':
            if (set(details)!={'observation_sequence','observation_digest','cutoff_ns','observed_at_ns','histories','account','result','reductions'}
                    or type(details['observation_sequence']) is not int or not 1<=details['observation_sequence']<=len(observations)):
                raise ValueError('reduction snapshot scope')
            source=observations[details['observation_sequence']-1]
            references=[[item['claim']['client_order_id'],item['state_event_sequence'],
                encode(events[item['state_event_sequence']-1])[1]] for item in reductions if item['state'] is not None]
            if len(references)!=len(reductions) or details['reductions']!=references:raise ValueError('unobserved reduction in snapshot')
            from app.live_execution.grvt_mainnet_finance import calculate_reduction_account
            expected=calculate_reduction_account(claim,source,details['histories'],details['account'],
                cutoff_ns=details['cutoff_ns'],observed_at_ns=details['observed_at_ns'],
                extra_orders=[item['state'] for item in reductions],extra_templates=[item['template'] for item in reductions])
            if (encode(source)[1]!=details['observation_digest'] or encode(expected)!=encode(details['result'])
                    or event['occurred_at_ns']!=details['observed_at_ns']):raise ValueError('reduction snapshot changed')
        elif event['type']=='REDUCTION_CLAIM':
            from app.live_execution.grvt_mainnet_reduction import verified_reduction_claim
            if set(details)!={'client_order_id','claim_digest'} or db.get(GrvtMainnetLeg,details['client_order_id']) is not None:
                raise ValueError('reduction reservation identity')
            record_order=db.get(GrvtMainnetReduction,details['client_order_id'])
            if record_order is None or record_order.payload_digest!=details['claim_digest']:
                raise ValueError('reduction claim truncated')
            reductions.append(verified_reduction_claim(record_order,value,encode(value)[1],claim,events,reductions,
                at_ns=event['occurred_at_ns']))
        elif event['type']=='REDUCTION_ACK':
            if set(details)!={'client_order_id','order_id'} or not isinstance(details['order_id'],str):
                raise ValueError('reduction acknowledgement')
            item=next(item for item in reductions if item['claim']['client_order_id']==details['client_order_id'])
            if item['acknowledgement'] is not None:raise ValueError('duplicate reduction acknowledgement')
            import re
            if not re.fullmatch(r'[A-Za-z0-9x]{1,80}',details['order_id']):raise ValueError('reduction exchange identity')
            item['acknowledgement']=details
        elif event['type']=='REDUCTION_STATE':
            from app.live_execution.grvt_mainnet_reduction import validate_reduction_state
            item=next(item for item in reductions if item['claim']['client_order_id']==details['client_order_id'])
            validate_reduction_state(details,item,at_ns=event['occurred_at_ns'])
            source_orders=observations[-1]['state']['orders'] if observations else []
            other_ids={order['order_id'] for order in source_orders}|{
                other['state']['order_id'] for other in reductions if other is not item and other['state'] is not None}
            if details['state']['order_id'] in other_ids:raise ValueError('reused exchange reduction identity')
            old=item['state'];new=details['state']
            if old is None or {key:value for key,value in old.items() if key!='observed_at_ns'}!={
                    key:value for key,value in new.items() if key!='observed_at_ns'}:
                item['material_state_event_sequence']=event['sequence']
            item['state']=details['state'];item['state_event_sequence']=event['sequence']
        else:raise ValueError('unknown recovery event')
        last_at=event['occurred_at_ns'];previous=row.payload_digest;events.append(event)
    if type(record.event_count) is not int or record.event_count!=len(events) or record.head_digest!=previous:
        raise ValueError('recovery tail truncated')
    reserved=list(db.scalars(select(GrvtMainnetReduction.client_order_id).where(GrvtMainnetReduction.parent_client_order_id==group.parent_client_order_id)))
    if set(reserved)!={item['claim']['client_order_id'] for item in reductions}:raise ValueError('reduction claim events truncated')
    terminal=bool(observations) and all(order['status'] in ('FILLED','CANCELLED','REJECTED') for order in observations[-1]['state']['orders'])
    return {'network':'MAINNET','authorization':False,'request':value,'initial_request':initial,'events':events,
        'cancel_claimed':cancelled,'cancel_acknowledged':acknowledged,'native_orders_terminal':terminal,'reductions':reductions}


def read_mainnet_recovery(vault,parent: str) -> dict:
    from app.live_execution.grvt_mainnet_observation import verified_mainnet_observations
    with vault.session() as db:
        from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
        group=db.get(GrvtMainnetBracket,parent);saved=verified_mainnet_bracket(db,group)
        observed=verified_mainnet_observations(db,group,saved['claim'],saved['acknowledgement'])
        result=verified_recovery(db,group,saved['claim'],observed)
        if result is None:raise GrvtMainnetError('No hay una recuperación MAINNET solicitada')
        return result


def _event(db,record,kind: str,details: dict,at: int) -> dict:
    value={'schema':'orbis.grvt.mainnet.recovery.event.v1','parent_client_order_id':record.parent_client_order_id,
        'request_digest':record.payload_digest,'sequence':record.event_count+1,'previous_digest':record.head_digest,
        'type':kind,'occurred_at_ns':stamp(at),'details':details}
    text,digest=encode(value)
    db.add(GrvtMainnetRecoveryEvent(parent_client_order_id=record.parent_client_order_id,
        sequence=value['sequence'],payload_json=text,payload_digest=digest))
    record.event_count+=1;record.head_digest=digest
    return value


def begin_mainnet_recovery(vault,parent: str,*,ttl_seconds: int,max_attempts: int,confirmation: str,now_ns: int) -> dict:
    if (confirmation!='RECUPERAR_POSICION_GRVT_MAINNET' or type(ttl_seconds) is not int or not 30<=ttl_seconds<=300
            or type(max_attempts) is not int or not 1<=max_attempts<=3):
        raise GrvtMainnetError('Límites o confirmación de recuperación MAINNET inválidos')
    stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');recovery_schema(db,required=True)
            from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
            group=db.get(GrvtMainnetBracket,parent);saved=verified_mainnet_bracket(db,group)
            if saved['reconciliation'] is not None or db.get(GrvtMainnetRecovery,parent) is not None:
                raise ValueError('request already present or group settled')
            request={'schema':'orbis.grvt.mainnet.recovery.v1','network':'MAINNET','parent_client_order_id':parent,
                'claim_digest':group.payload_digest,'started_at_ns':now_ns,'deadline_ns':now_ns+ttl_seconds*1_000_000_000,
                'max_attempts':max_attempts,**{key:saved['claim']['draft'][key]
                    for key in ('exit_cap','max_spread_bps','max_slippage_bps')},'authorization':False}
            text,digest=encode(request)
            db.add(GrvtMainnetRecovery(parent_client_order_id=parent,payload_json=text,payload_digest=digest,event_count=0,head_digest=None))
            db.flush();verified_mainnet_bracket(db,group);db.commit()
        except Exception:
            db.rollback();raise GrvtMainnetError('No se pudo reservar una recuperación MAINNET nueva') from None
    return read_mainnet_recovery(vault,parent)


def audit_recovery_position(vault,parent: str,*,client_factory:Callable[[],GrvtMainnetClient],
        require_permission:Callable[[],None],clock_ns:Callable[[],int]=time.time_ns) -> dict:
    from app.live_execution.grvt_mainnet_finance import _histories,calculate_reduction_account
    from app.live_execution.grvt_mainnet_preflight import _state
    from app.live_execution.grvt_mainnet_journal import _control,verified_mainnet_bracket
    generation=vault.session_generation
    if generation is None:raise GrvtMainnetError('Bóveda bloqueada')
    last=stamp(clock_ns());began=last
    def checked():
        nonlocal last
        require_permission();at=stamp(clock_ns())
        if vault.session_generation!=generation or not last<=at<=began+120_000_000_000:
            raise GrvtMainnetError('Auditoría de reducción MAINNET revocada o vencida')
        last=at;return at
    checked();request=read_mainnet_recovery(vault,parent);saved=read_mainnet_bracket(vault,parent)
    if saved['reconciliation'] is not None:raise GrvtMainnetError('Grupo MAINNET ya conciliado')
    observed=observe_mainnet_bracket(vault,parent,client_factory=client_factory,require_permission=checked,clock_ns=checked)
    from app.live_execution.grvt_mainnet_reduction import observe_recovery_reductions
    view=observe_recovery_reductions(vault,parent,client_factory=client_factory,require_permission=checked,clock_ns=checked)
    try:
        with vault.session() as db:salt=_control(db,saved['sub_account_id']).identity_salt
        client=client_factory();checked()
        if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET'
                or client.sub_account_id!=saved['sub_account_id'] or client.identity_ref(salt)!=saved['claim']['account_ref']):
            raise ValueError('reduction audit account')
        cutoff=checked();start=saved['claim']['preflight']['states'][-1]['observed_at_ns']
        histories=_histories(client,saved['sub_account_id'],start,cutoff,checked)
        account=_state(client,saved['sub_account_id'],checked,allow_position=saved['claim']['draft']['instrument']);at=checked()
        if any(item['state'] is None for item in view['reductions']):raise ValueError('reduction pending observation')
        result=calculate_reduction_account(saved['claim'],observed,histories,account,cutoff_ns=cutoff,observed_at_ns=at,
            extra_orders=[item['state'] for item in view['reductions']],extra_templates=[item['template'] for item in view['reductions']])
        details={'observation_sequence':observed['sequence'],'observation_digest':encode(observed)[1],
            'cutoff_ns':cutoff,'observed_at_ns':at,'histories':histories,'account':account,'result':result,
            'reductions':[[item['claim']['client_order_id'],item['state_event_sequence'],
                encode(view['events'][item['state_event_sequence']-1])[1]] for item in view['reductions']]}
        with vault.guard_session_generation(generation),vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                group=db.get(GrvtMainnetBracket,parent);current=verified_mainnet_bracket(db,group)
                if current['last_observation']!=observed or current['reconciliation'] is not None:
                    raise ValueError('reduction source changed')
                record=db.get(GrvtMainnetRecovery,parent)
                if current['recovery']['request']!=request['request']:raise ValueError('recovery terms changed')
                event=_event(db,record,'REDUCTION_SNAPSHOT',details,at)
                checked();db.flush();verified_mainnet_bracket(db,group);db.commit();return event
            except Exception:db.rollback();raise
    except Exception:
        raise GrvtMainnetError('Posición MAINNET no verificable para reducción; conserva la reserva') from None


def cancel_mainnet_bracket_once(vault,parent: str,*,client_factory:Callable[[],GrvtMainnetClient],
        require_permission:Callable[[],None],clock_ns:Callable[[],int]=time.time_ns) -> dict:
    generation=vault.session_generation
    if generation is None:raise GrvtMainnetError('Bóveda bloqueada')
    last=stamp(clock_ns());started=last
    def checked():
        nonlocal last
        require_permission();at=stamp(clock_ns())
        if vault.session_generation!=generation or not last<=at<=started+120_000_000_000:
            raise GrvtMainnetError('Cancelación MAINNET revocada o vencida')
        last=at;return at
    checked();request=read_mainnet_recovery(vault,parent)
    if not request['cancel_claimed'] and checked()>=request['request']['deadline_ns']:
        raise GrvtMainnetError('Permiso de cancelación MAINNET vencido')
    saved=read_mainnet_bracket(vault,parent)
    if saved['reconciliation'] is not None:return request
    observed=observe_mainnet_bracket(vault,parent,client_factory=client_factory,require_permission=checked,clock_ns=checked)
    request=read_mainnet_recovery(vault,parent)
    if request['cancel_claimed'] or request['native_orders_terminal']:return request
    from app.live_execution.grvt_mainnet_journal import _control
    with vault.session() as db:salt=_control(db,saved['sub_account_id']).identity_salt
    client=client_factory();checked()
    if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET'
            or client.sub_account_id!=saved['sub_account_id'] or client.identity_ref(salt)!=saved['claim']['account_ref']):
        raise GrvtMainnetError('Sesión de cancelación fuera del ámbito MAINNET')
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            current=read_mainnet_recovery(vault,parent)
            if current['cancel_claimed'] or current['request']!=request['request'] or checked()>=request['request']['deadline_ns']:
                raise ValueError('cancel source changed')
            record=db.get(GrvtMainnetRecovery,parent)
            _event(db,record,'CANCEL_CLAIM',{'observation_sequence':observed['sequence'],
                'observation_digest':encode(observed)[1]},checked())
            db.flush();db.commit()
        except Exception:
            db.rollback();raise GrvtMainnetError('No se pudo reservar la cancelación MAINNET') from None
    try:
        checked();client.cancel_bracket_once(saved['sub_account_id'],parent,require_permission=checked)
    except Exception:return read_mainnet_recovery(vault,parent)
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            record=db.get(GrvtMainnetRecovery,parent)
            at=clock_ns();at=at if type(at) is int and last<=at<2**63 else last
            _event(db,record,'CANCEL_ACK',{'ack':True},at);db.flush();db.commit()
        except Exception:
            db.rollback();raise GrvtMainnetError('Cancelación emitida sin acuse archivado; consulta los IDs') from None
    return read_mainnet_recovery(vault,parent)
