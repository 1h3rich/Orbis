"""Intentos IOC acotados a evidencia de reducción y a un permiso RAM nuevo."""

from collections.abc import Callable
from decimal import Decimal,localcontext
import json
import re
import secrets
import time

from eth_account import Account
from sqlalchemy import select

from app.integrations.grvt_readonly import _order
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,client_ids
from app.live_execution.grvt_mainnet_finance import stamp
from app.live_execution.grvt_mainnet_journal import encode,_draft,_instrument,signer_ref,_control,read_mainnet_bracket
from app.live_execution.grvt_mainnet_reduction_order import unsigned_reduction,sign_reduction,verify_reduction,reduction_price
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient,fetch_mainnet_instrument,TICKER_URL,_result,public_post as _public
from app.live_execution.grvt_mainnet_preflight import number,canonical
from app.live_execution.grvt_mainnet_remote_order import remote_contract,standalone_order_metadata
from app.live_execution.grvt_mainnet_order_history import lookup_recovery_orders
from app.portfolio.models import GrvtMainnetReduction,GrvtMainnetLeg,GrvtMainnetRecovery,GrvtMainnetBracket,GrvtMainnetObservation,GrvtMainnetReconciliation


_CLAIM_FIELDS={'schema','network','parent_client_order_id','request_digest','client_order_id','attempt','issued_at_ns',
    'expiration_ns','nonce','size','price','snapshot_refs','quote','wire_digest','unsigned_digest','signer_ref'}
_STATE_FIELDS={'role','client_order_id','order_id','status','size','traded_size','create_time_ns','update_time_ns','observed_at_ns'}


def ready_snapshots(events: list[dict],reductions: list[dict],*,now_ns: int,after_sequence:int=0) -> list[dict]:
    snapshots=[event for event in events if event['type']=='REDUCTION_SNAPSHOT' and event['sequence']>after_sequence]
    if len(snapshots)<2:raise GrvtMainnetError('Se requieren dos auditorías de reducción MAINNET')
    first,last=snapshots[-2:]
    if (last['occurred_at_ns']-first['occurred_at_ns']<5_000_000_000
            or not 0<=now_ns-last['occurred_at_ns']<=5_000_000_000
            or first['details']['result']!=last['details']['result']
            or any(first['details']['histories'][key]['rows']!=last['details']['histories'][key]['rows']
                for key in first['details']['histories'])
            or number(last['details']['result']['remaining_size'])<=0
            or any(item['state'] is None or item['state']['status'] not in ('FILLED','CANCELLED','REJECTED') for item in reductions)
            or reductions and first['sequence']<=max(item['material_state_event_sequence'] for item in reductions)):
        raise GrvtMainnetError('Auditorías de reducción MAINNET inestables, antiguas o pendientes')
    return [first,last]


def verified_reduction_claim(record,request: dict,request_digest: str,claim: dict,events: list[dict],prior: list[dict],*,at_ns: int) -> dict:
    from app.live_execution.grvt_mainnet_recovery_grants import effective_exit_draft
    value=json.loads(record.payload_json);draft=effective_exit_draft(claim,request);instrument=_instrument(claim['instrument'])
    if (set(value)!=_CLAIM_FIELDS or value['schema']!='orbis.grvt.mainnet.reduction.claim.v1' or value['network']!='MAINNET'
            or value['parent_client_order_id']!=claim['parent_client_order_id'] or record.parent_client_order_id!=claim['parent_client_order_id']
            or value['request_digest']!=request_digest or value['client_order_id']!=record.client_order_id
            or type(value['attempt']) is not int or value['attempt']!=len(prior)+1 or record.attempt!=value['attempt']
            or value['attempt']>request['max_attempts'] or encode(value)!=(record.payload_json,record.payload_digest)
            or not request['started_at_ns']<=stamp(value['issued_at_ns'])<=at_ns<stamp(value['expiration_ns'])<=request['deadline_ns']
            or value['signer_ref']!=claim['signer_ref'] or type(value['nonce']) is not int
            or value['nonce'] in claim['nonces'] or value['nonce'] in [item['claim']['nonce'] for item in prior]
            or any(not isinstance(value[key],str) or not re.fullmatch(r'[a-f0-9]{64}',value[key]) for key in ('wire_digest','unsigned_digest'))):
        raise ValueError('reduction claim scope')
    source=ready_snapshots(events,prior,now_ns=at_ns,after_sequence=request.get('grant_sequence',0))
    if value['snapshot_refs']!=[[event['sequence'],encode(event)[1]] for event in source]:raise ValueError('reduction snapshot link')
    size=number(value['size']);price=reduction_price(value['quote'],draft,instrument,at_ns=value['issued_at_ns'])
    with localcontext() as context:
        context.prec=100
        reference=number(value['quote']['ask' if draft.direction=='SHORT' else 'bid'])
        if (size!=number(source[-1]['details']['result']['remaining_size']) or value['price']!=canonical(price)
                or value['quote']['observed_at_ns']<source[-1]['occurred_at_ns']
                or size*max(price,reference)>draft.exit_cap):raise ValueError('reduction exceeds position or reviewed cap')
    template=unsigned_reduction(draft,instrument,cid=value['client_order_id'],size=size,price=price,nonce=value['nonce'],
        issued_at_ns=value['issued_at_ns'],expiration_ns=value['expiration_ns'])
    if encode(template)[1]!=value['unsigned_digest']:raise ValueError('reduction contract changed')
    return {'claim':value,'template':template,'state':None,'state_event_sequence':None,
        'material_state_event_sequence':None,'acknowledgement':None}


def validate_reduction_state(details: dict,reduction: dict,*,at_ns: int) -> None:
    state=details['state'];claim=reduction['claim'];old=reduction['state']
    if (set(details)!={'client_order_id','wire_digest','state'} or details['client_order_id']!=claim['client_order_id']
            or details['wire_digest']!=claim['wire_digest'] or set(state)!=_STATE_FIELDS
            or state['role']!='REDUCTION' or state['client_order_id']!=claim['client_order_id']
            or not isinstance(state['order_id'],str) or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',state['order_id'])
            or state['status'] not in ('PENDING','OPEN','FILLED','CANCELLED','REJECTED')
            or number(state['size'])!=number(claim['size']) or not 0<=number(state['traded_size'])<=number(claim['size'])
            or state['status']=='FILLED' and number(state['traded_size'])!=number(claim['size'])
            or state['status']=='REJECTED' and number(state['traded_size'])!=0
            or type(state['observed_at_ns']) is not int or state['observed_at_ns']!=at_ns
            or any(not isinstance(state[key],str) or not re.fullmatch(r'[0-9]{1,20}',state[key]) for key in ('create_time_ns','update_time_ns'))
            or not claim['issued_at_ns']-1_000_000_000<=int(state['create_time_ns'])<=int(state['update_time_ns'])<=at_ns+2_000_000_000
            or reduction['acknowledgement'] is not None and reduction['acknowledgement']['order_id']!=state['order_id']):
        raise ValueError('reduction state changed')
    if old is not None and (old['order_id']!=state['order_id'] or old['create_time_ns']!=state['create_time_ns']
            or int(old['update_time_ns'])>int(state['update_time_ns']) or old['observed_at_ns']>at_ns
            or number(old['traded_size'])>number(state['traded_size'])
            or old['status'] in ('FILLED','CANCELLED','REJECTED') and old['status']!=state['status']
            or old['status']=='OPEN' and state['status']=='PENDING'):
        raise ValueError('reduction state regressed')


def snapshot_reductions(view: dict,event: dict) -> tuple[list[dict],list[dict]]:
    by_id={item['claim']['client_order_id']:item for item in view['reductions']}
    templates=[];states=[]
    for cid,sequence,digest in event['details']['reductions']:
        source=view['events'][sequence-1]
        if source['type']!='REDUCTION_STATE' or encode(source)[1]!=digest or source['details']['client_order_id']!=cid:
            raise ValueError('historical reduction source')
        templates.append(by_id[cid]['template']);states.append(source['details']['state'])
    return templates,states


def _checked(vault,generation,permission,clock):
    last=stamp(clock());start=last
    def checked():
        nonlocal last
        permission();at=stamp(clock())
        if vault.session_generation!=generation or not last<=at<=start+120_000_000_000:
            raise GrvtMainnetError('Permiso de reducción MAINNET revocado o vencido')
        last=at;return at
    checked();return checked


def submit_recovery_reduction_once(vault,parent: str,*,client_factory:Callable[[],GrvtMainnetClient],
        private_key:bytes,expected_signer:str,require_permission:Callable[[],None],
        public_post:Callable=_public,clock_ns:Callable[[],int]=time.time_ns) -> dict:
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery,_event
    generation=vault.session_generation
    if generation is None:raise GrvtMainnetError('Bóveda bloqueada')
    checked=_checked(vault,generation,require_permission,clock_ns)
    view=read_mainnet_recovery(vault,parent);saved=read_mainnet_bracket(vault,parent);request=view['request']
    source=ready_snapshots(view['events'],view['reductions'],now_ns=checked(),after_sequence=request.get('grant_sequence',0))
    if saved['reconciliation'] is not None or len(view['reductions'])>=request['max_attempts']:
        raise GrvtMainnetError('Sin permiso para otro intento de reducción MAINNET')
    from app.live_execution.grvt_mainnet_recovery_grants import effective_exit_draft
    draft=effective_exit_draft(saved['claim'],request);instrument=_instrument(saved['claim']['instrument'])
    with vault.session() as db:salt=_control(db,draft.sub_account_id).identity_salt
    try:
        if (type(private_key) is not bytes or len(private_key)!=32 or not isinstance(expected_signer,str)
                or signer_ref(salt,expected_signer)!=saved['claim']['signer_ref']
                or Account.from_key(private_key).address.lower()!=expected_signer.lower()):raise ValueError('dedicated signer')
        if fetch_mainnet_instrument(draft.instrument,http_post=public_post)!=instrument:raise ValueError('instrument changed')
        checked();raw=_result(public_post(TICKER_URL,{'instrument':draft.instrument}))['result'];at=checked()
        if not isinstance(raw.get('event_time'),str) or not re.fullmatch(r'[0-9]{1,20}',raw['event_time']):raise ValueError('quote timestamp')
        quote={'source':TICKER_URL,'instrument':raw['instrument'],'event_time_ns':int(raw['event_time']),'observed_at_ns':at,
            **{name:canonical(number(raw[field])) for name,field in (('bid','best_bid_price'),('ask','best_ask_price'),
                ('bid_size','best_bid_size'),('ask_size','best_ask_size'))}}
        price=reduction_price(quote,draft,instrument,at_ns=checked())
        client=client_factory();checked()
        if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=draft.sub_account_id
                or client.identity_ref(salt)!=saved['claim']['account_ref']):raise ValueError('reduction account')
        issued=checked();expiration=min(issued+20_000_000_000,request['deadline_ns'])
        if expiration-issued<5_000_000_000:raise ValueError('permission expiring')
        nonce=secrets.randbits(32)
        used=saved['claim']['nonces']+[item['claim']['nonce'] for item in view['reductions']]
        while nonce in used:nonce=secrets.randbits(32)
        cid=str(2**63|secrets.randbits(63))
        from app.live_execution.grvt_perp_limit_journal import limit_client_id_reserved
        with vault.session() as db:
            while (db.get(GrvtMainnetLeg,cid) is not None or db.get(GrvtMainnetReduction,cid) is not None
                   or limit_client_id_reserved(db,cid)):
                cid=str(2**63|secrets.randbits(63))
        template=unsigned_reduction(draft,instrument,cid=cid,size=number(source[-1]['details']['result']['remaining_size']),
            price=price,nonce=nonce,issued_at_ns=issued,expiration_ns=expiration)
        wire=sign_reduction(template,instrument,private_key=private_key,expected_signer=expected_signer)
        payload={'schema':'orbis.grvt.mainnet.reduction.claim.v1','network':'MAINNET','parent_client_order_id':parent,
            'request_digest':encode(request)[1],'client_order_id':cid,'attempt':len(view['reductions'])+1,
            'issued_at_ns':issued,'expiration_ns':expiration,'nonce':nonce,'size':template['legs'][0]['size'],
            'price':template['legs'][0]['limit_price'],'snapshot_refs':[[event['sequence'],encode(event)[1]] for event in source],
            'quote':quote,'wire_digest':encode(wire)[1],'unsigned_digest':encode(template)[1],'signer_ref':saved['claim']['signer_ref']}
        with vault.guard_session_generation(generation),vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                current=read_mainnet_recovery(vault,parent)
                if current!=view or db.get(GrvtMainnetLeg,cid) is not None or limit_client_id_reserved(db,cid):
                    raise ValueError('reduction source changed')
                current_group=read_mainnet_bracket(vault,parent)
                source_order=db.get(GrvtMainnetObservation,(parent,source[-1]['details']['observation_sequence']))
                if (current_group['reconciliation'] is not None or source_order is None
                        or current_group['last_observation']['state']['orders']!=json.loads(source_order.payload_json)['state']['orders']):
                    raise ValueError('native state changed after reduction audit')
                text,digest=encode(payload)
                record=GrvtMainnetReduction(client_order_id=cid,parent_client_order_id=parent,attempt=payload['attempt'],payload_json=text,payload_digest=digest)
                verified_reduction_claim(record,request,encode(request)[1],saved['claim'],view['events'],view['reductions'],at_ns=checked())
                db.add(record);header=db.get(GrvtMainnetRecovery,parent)
                _event(db,header,'REDUCTION_CLAIM',{'client_order_id':cid,'claim_digest':digest},checked())
                db.flush()
                from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
                verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,parent));db.commit()
            except Exception:db.rollback();raise
        try:
            sent_at=checked();ready_snapshots(view['events'],view['reductions'],now_ns=sent_at,after_sequence=request.get('grant_sequence',0))
            reduction_price(quote,draft,instrument,at_ns=sent_at)
            if checked()>=expiration:raise ValueError('reduction expired before send')
            ack=client.submit_reduction_once(wire,template,instrument,require_permission=checked,clock_ns=checked)
        except Exception:return read_mainnet_recovery(vault,parent)
        with vault.guard_session_generation(generation),vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                header=db.get(GrvtMainnetRecovery,parent)
                received=clock_ns();received=received if type(received) is int and sent_at<=received<2**63 else sent_at
                _event(db,header,'REDUCTION_ACK',{'client_order_id':cid,'order_id':ack['order_id']},received)
                db.flush()
                from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
                verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,parent));db.commit()
            except Exception:db.rollback();raise
        return read_mainnet_recovery(vault,parent)
    except Exception:
        raise GrvtMainnetError('Reducción MAINNET no confirmada o fuera de límites; consulta los IDs') from None
    finally:del private_key


def observe_recovery_reductions(vault,parent: str,*,client_factory:Callable[[],GrvtMainnetClient],
        require_permission:Callable[[],None],clock_ns:Callable[[],int]=time.time_ns) -> dict:
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery,_event
    generation=vault.session_generation
    if generation is None:raise GrvtMainnetError('Bóveda bloqueada')
    checked=_checked(vault,generation,require_permission,clock_ns)
    view=read_mainnet_recovery(vault,parent);saved=read_mainnet_bracket(vault,parent)
    if saved['reconciliation'] is not None:raise GrvtMainnetError('Grupo MAINNET ya conciliado')
    if not view['reductions']:return view
    with vault.session() as db:salt=_control(db,saved['sub_account_id']).identity_salt
    try:
        client=client_factory();checked()
        if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=saved['sub_account_id']
                or client.identity_ref(salt)!=saved['claim']['account_ref']):raise ValueError('reduction observer account')
        details=[];instrument=_instrument(saved['claim']['instrument'])
        for item in view['reductions']:
            claim=item['claim'];cid=claim['client_order_id']
            raw=lookup_recovery_orders(client,saved['sub_account_id'],instrument.name,checked=checked,
                windows={cid:(claim['issued_at_ns']-1_000_000_000,claim['expiration_ns'])})[cid]
            at=checked()
            if not standalone_order_metadata(raw.get('metadata')):raise ValueError('reduction metadata changed')
            template=item['template'];wire=remote_contract(raw,template)
            signer=verify_reduction(wire,template,instrument)
            if encode(wire)[1]!=claim['wire_digest'] or signer_ref(salt,signer)!=claim['signer_ref']:raise ValueError('reduction wire changed')
            order=_order(raw,saved['sub_account_id'],open_only=False,now_ns=at);leg=order['legs'][0]
            state={'role':'REDUCTION','client_order_id':order['client_order_id'],'order_id':order['order_id'],
                'status':order['status'],'size':canonical(number(leg['size'])),'traded_size':canonical(number(leg['traded_size'])),
                'create_time_ns':order['create_time_ns'],'update_time_ns':order['update_time_ns'],'observed_at_ns':at}
            value={'client_order_id':claim['client_order_id'],'wire_digest':claim['wire_digest'],'state':state}
            validate_reduction_state(value,item,at_ns=at);details.append(value)
        with vault.guard_session_generation(generation),vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                if read_mainnet_recovery(vault,parent)!=view:raise ValueError('reduction observer source changed')
                if db.get(GrvtMainnetReconciliation,parent) is not None:raise ValueError('reduction group already settled')
                header=db.get(GrvtMainnetRecovery,parent)
                for value in details:_event(db,header,'REDUCTION_STATE',value,value['state']['observed_at_ns'])
                checked();db.flush()
                from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
                verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,parent));db.commit()
            except Exception:db.rollback();raise
        return read_mainnet_recovery(vault,parent)
    except Exception:
        raise GrvtMainnetError('Ejecución de reducción MAINNET no verificable; no reenvíes') from None
