"""Sealed cancellation mandates and single-POST grants. No keys or network IO."""
from dataclasses import fields
from datetime import datetime,timedelta
import hashlib,hmac
from uuid import UUID
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_cancellation import CancellationState,CancellationPlan,plan_cancellation,verify_cancellation_signature,GAS
from app.live_execution.evm_transaction import word
from app.portfolio import dex_gas_return as original,dex_native_resolution as native
from app.portfolio.dex_deployments import _identity,_hex_ref
from app.portfolio.dex_operator_journal import _history
from app.portfolio.models import RealDexGasCancellationIntent
from app.portfolio.wallet_funding import _encoded,_decoded,_time,_uint
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA='orbis.dex.gas.cancellation.v1'
TRANSITIONS={'PREPARED':{'RESERVED','CANCELLED','SETTLED'},'RESERVED':{'SIGNED','ABANDONED_UNSENT','SETTLED'},
    'SIGNED':{'BROADCAST_ATTEMPTED','ABANDONED_UNSENT','SETTLED'},'BROADCAST_ATTEMPTED':{'SETTLED'}}
STATES=set(TRANSITIONS)|{'CANCELLED','ABANDONED_UNSENT','SETTLED'}


def _schema(db):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<69:raise DexExecutorError('Actualiza la bóveda antes de cancelar devoluciones')


def _seal(db,purpose,body):return hmac.new(_salt(db),('orbis.dex.gas.cancellation.v1:'+purpose+':').encode()+_encoded(body)[0].encode(),hashlib.sha256).hexdigest()


def _sealed_state(db,digest,state):
    body={key:value for key,value in state.items() if key!='seal'}
    return {**body,'seal':_seal(db,'state:'+digest,body)}


def _anchor(snapshot):
    return {field.name:(getattr(snapshot,field.name).isoformat() if isinstance(getattr(snapshot,field.name),datetime)
        else str(getattr(snapshot,field.name)) if type(getattr(snapshot,field.name)) is int else getattr(snapshot,field.name))
        for field in fields(CancellationState) if field.name not in {'owner','operator'}}


def _read_anchor(value,at):
    expected={field.name for field in fields(CancellationState)}-{'owner','operator'}
    if set(value)!=expected:raise ValueError('cancel anchor')
    data=dict(value)
    for key in ('block_number','latest_number','balance','latest_balance','nonce','latest_nonce','live_nonce','pending_nonce'):
        data[key]=_uint(value[key],key,maximum=2**64-1 if 'nonce' in key else 2**256-1,positive=False)
    for key in ('block_at','latest_at','observed_at'):data[key]=_time(datetime.fromisoformat(value[key]))
    result=CancellationState('0x'+'11'*20,'0x'+'22'*20,**data);result.require_fresh(at)
    if _anchor(result)!=value:raise ValueError('canonical cancel anchor')
    return result


def _source_pending(state):
    if state['status']=='BROADCAST_ATTEMPTED':return state
    if state['status'] not in {'FINALIZED','REVERTED'}:raise ValueError('original did not have a POST grant')
    return {**state,'status':'BROADCAST_ATTEMPTED','receipt':None,'history':state['history'][:-1]}


def _base(db,identifier):
    _schema(db);row=db.get(RealDexGasCancellationIntent,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Cancelación de devolución no encontrada')
    try:
        terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
        keys={'schema','id','account_id','return_id','draft_id','wallet_digest','operator_digest','source_plan_digest','source_pending_digest',
            'nonce','original_gas_price','base_gas_price','gas_price','bump_percent','max_gas_wei','anchor','stopped','previous_posts','expires_at','seal'}
        parent,source,pstate=original.return_record(db,row.return_id)
        pending=_source_pending(pstate)
        if (set(terms)!=keys or terms['schema']!=SCHEMA or terms['id']!=row.id or terms['return_id']!=row.return_id
                or terms['account_id']!=row.account_id or row.account_id!=parent.account_id or terms['wallet_digest']!=row.wallet_digest
                or row.wallet_digest!=source['wallet_digest'] or terms['operator_digest']!=row.operator_digest or row.operator_digest!=source['operator_digest']
                or str(UUID(terms['draft_id']))!=terms['draft_id'] or terms['source_plan_digest']!=parent.payload_digest
                or terms['source_pending_digest']!=_encoded(pending)[1] or parent.created_at>row.created_at
                or datetime.fromisoformat(pending['history'][-1]['at'])>row.created_at
                or terms['nonce']!=source['nonce'] or terms['original_gas_price']!=source['gas_price']
                or terms['expires_at']!=(row.created_at+timedelta(seconds=120)).isoformat()
                or terms['stopped']!=original._stopped(db,row.operator_digest,row.created_at)
                or not hmac.compare_digest(terms['seal'],_seal(db,'plan',{key:value for key,value in terms.items() if key!='seal'}))):raise ValueError('original/mandate')
        snapshot=_read_anchor(terms['anchor'],row.created_at)
        plan=plan_cancellation(snapshot,original_nonce=int(terms['nonce']),original_gas_price=_uint(terms['base_gas_price'],'Precio base',maximum=10**12),
            gas_price=_uint(terms['gas_price'],'Precio',maximum=10**12),bump_percent=terms['bump_percent'],
            max_gas_wei=_uint(terms['max_gas_wei'],'Coste máximo',maximum=10**17),now=row.created_at)
        if plan.original_gas_price<int(source['gas_price']):raise ValueError('unreviewed original fee')
        refs=terms['previous_posts']
        if (not isinstance(refs,list) or any(set(ref)!={'id','payload_digest','broadcast_digest'} or str(UUID(ref['id']))!=ref['id']
                or not _hex_ref(ref['payload_digest']) or not _hex_ref(ref['broadcast_digest']) for ref in refs)
                or len({ref['id'] for ref in refs})!=len(refs) or refs!=sorted(refs,key=lambda ref:ref['id'])):raise ValueError('predecessors')
        if set(state)!={'status','history','tx_ref','settlement','seal'} or row.status not in STATES:raise ValueError('cancel state')
        statuses=_history(row,state,TRANSITIONS,'PREPARED')
        if (not hmac.compare_digest(state['seal'],_sealed_state(db,row.payload_digest,state)['seal'])
                or ('SIGNED' in statuses)!=(state['tx_ref'] is not None) or state['tx_ref'] is not None and not _hex_ref(state['tx_ref'])
                or (row.status=='SETTLED')!=(state['settlement'] is not None)
                or 'BROADCAST_ATTEMPTED' in statuses and datetime.fromisoformat(state['history'][statuses.index('BROADCAST_ATTEMPTED')]['at'])>=datetime.fromisoformat(terms['expires_at'])):raise ValueError('grant/history')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):raise DexExecutorError('El diario de cancelación BNB no es coherente') from None
    return row,terms,state


def _broadcast_digest(db,row,terms,state):
    statuses=[event['status'] for event in state['history']]
    if 'BROADCAST_ATTEMPTED' not in statuses:return None
    before={**state,'status':'BROADCAST_ATTEMPTED','settlement':None,'history':state['history'][:statuses.index('BROADCAST_ATTEMPTED')+1]}
    return _encoded(_sealed_state(db,row.payload_digest,before))[1]


def _posts(db,return_id):
    result=[];prices=[]
    for identifier in db.scalars(select(RealDexGasCancellationIntent.id).where(RealDexGasCancellationIntent.return_id==return_id)):
        row,terms,state=_base(db,identifier);digest=_broadcast_digest(db,row,terms,state)
        if digest is not None:
            result.append({'id':row.id,'payload_digest':row.payload_digest,'broadcast_digest':digest});prices.append(int(terms['gas_price']))
    return sorted(result,key=lambda ref:ref['id']),prices


def _settlement(db,row,terms,state):
    value=state['settlement']
    if value is None:return None
    if set(value)!= {'kind','source_digest','resolution_id','winner_id'}:raise ValueError('settlement shape')
    if value['kind']=='ORIGINAL':
        parent,_,pstate=original.return_record(db,row.return_id)
        if parent.status not in {'FINALIZED','REVERTED'} or parent.state_digest!=value['source_digest'] or parent.updated_at>row.updated_at or value['resolution_id'] is not None or value['winner_id'] is not None:
            raise ValueError('original settlement')
        return {'winner':'ORIGINAL','winner_id':None,'resolution_id':None,'receipt':pstate['receipt']}
    if value['kind']!='NATIVE':raise ValueError('settlement kind')
    resolved=native.resolution_record(db,value['resolution_id'])
    if resolved is None:raise ValueError('missing resolution')
    record,payload=resolved;proof=payload['proof']
    if (payload['source_kind']!='GAS_RETURN' or payload['source_id']!=row.return_id or record.payload_digest!=value['source_digest']
            or record.created_at>row.updated_at):raise ValueError('native settlement')
    winner=None
    for candidate in db.scalars(select(RealDexGasCancellationIntent.id).where(RealDexGasCancellationIntent.return_id==row.return_id)):
        other,review,grant=_base(db,candidate)
        if _broadcast_digest(db,other,review,grant) is not None and grant['tx_ref']==proof['tx_ref']:
            if (proof['outcome']!='CANCELLED' or proof['receipt']['transaction_type']!=0 or proof['receipt']['gas_limit']!=str(GAS)
                    or proof['receipt']['fee_cap']!=review['gas_price'] or proof['receipt']['gas_fee_wei']!=str(GAS*int(review['gas_price']))):raise ValueError('unreviewed cancellation receipt')
            winner=other.id
    if winner!=value['winner_id']:raise ValueError('winner link')
    return {'winner':'CANCELLATION' if winner else 'EXTERNAL','winner_id':winner,'resolution_id':record.id,'receipt':proof['receipt']}


def cancellation_record(db,identifier):
    row,terms,state=_base(db,identifier)
    try:
        prices=[int(terms['original_gas_price'])]
        for ref in terms['previous_posts']:
            previous,review,grant=_base(db,ref['id'])
            posted=next((datetime.fromisoformat(event['at']) for event in grant['history'] if event['status']=='BROADCAST_ATTEMPTED'),None)
            if (previous.id==row.id or previous.return_id!=row.return_id or previous.created_at>row.created_at
                    or posted is None or posted>row.created_at
                    or previous.payload_digest!=ref['payload_digest'] or _broadcast_digest(db,previous,review,grant)!=ref['broadcast_digest']):raise ValueError('previous POST')
            prices.append(int(review['gas_price']))
        if int(terms['base_gas_price'])!=max(prices):raise ValueError('replacement price base')
        _settlement(db,row,terms,state)
    except (ValueError,KeyError,TypeError,AttributeError):raise DexExecutorError('La cancelación perdió su revisión o recibo ganador') from None
    return row,terms,state


def _public(db,row,terms,state,now):
    settled=_settlement(db,row,terms,state)
    return {'id':row.id,'return_id':row.return_id,'account_id':row.account_id,'status':row.status,'plan_digest':row.payload_digest,
        'nonce':terms['nonce'],'base_gas_price':int(terms['base_gas_price']),'gas_price':int(terms['gas_price']),'bump_percent':terms['bump_percent'],
        'gas_wei':str(GAS*int(terms['gas_price'])),'max_gas_wei':terms['max_gas_wei'],'previous_posts':len(terms['previous_posts']),
        'post_attempted':any(event['status']=='BROADCAST_ATTEMPTED' for event in state['history']),
        'expires_at':terms['expires_at'],'expired':_time(now)>=datetime.fromisoformat(terms['expires_at']),
        'winner':settled['winner'] if settled else None,'winner_id':settled['winner_id'] if settled else None,
        'resolution_id':settled['resolution_id'] if settled else None,'receipt':settled['receipt'] if settled else None,'automatic_execution':False}


def view_cancellation(db,identifier,*,now):return _public(db,*cancellation_record(db,identifier),now)


def _owned(db,identifier,account,wallet=None):
    row,terms,state=cancellation_record(db,identifier)
    if row.account_id!=str(UUID(account)) or wallet is not None and row.wallet_digest!=_identity(db,'wallet',canonical_wallet_address(wallet)):
        raise DexExecutorError('La cancelación pertenece a otra cuenta o wallet')
    return row,terms,state


def _open(db,return_id):
    parent,source,state=original.return_record(db,return_id)
    if parent.status!='BROADCAST_ATTEMPTED' or native.status(db,'GAS_RETURN',return_id) is not None:
        raise DexExecutorError('El origen ya se resolvió; no emitas una cancelación')
    for identifier in db.scalars(select(RealDexGasCancellationIntent.id).where(RealDexGasCancellationIntent.return_id==return_id,
            RealDexGasCancellationIntent.status.in_(('RESERVED','SIGNED')))):
        cancellation_record(db,identifier)
        raise DexExecutorError('Descarta primero la cancelación reservada que no se ha emitido')
    return parent,source,state


def _profile(db,parent,source,wallet,drafts,draft_id,snapshot,now):
    address=drafts.require_verified(identifier=draft_id,account_id=parent.account_id,wallet_address=wallet,now=now)
    if (source['wallet_digest']!=_identity(db,'wallet',wallet) or source['operator_digest']!=_identity(db,'wallet',address)
            or type(snapshot) is not CancellationState or snapshot.owner!=wallet or snapshot.operator!=address):
        raise DexExecutorError('La copia, operadora o wallet de cancelación cambió')
    snapshot.require_fresh(now)
    return original._stopped(db,source['operator_digest'],now)


def prepare_cancellation(db,*,identifier,return_id,account_id,wallet_address,draft_id,drafts,snapshot,gas_price,bump_percent,max_gas_wei,now):
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');_schema(db);now=_time(now)
        identifier,return_id,account_id,draft_id=map(lambda value:str(UUID(value)),(identifier,return_id,account_id,draft_id));wallet=canonical_wallet_address(wallet_address)
        if db.get(RealDexGasCancellationIntent,identifier) is not None:
            row,terms,state=_owned(db,identifier,account_id,wallet)
            if (row.return_id!=return_id or terms['draft_id']!=draft_id or terms['gas_price']!=str(gas_price)
                    or terms['bump_percent']!=bump_percent or terms['max_gas_wei']!=str(max_gas_wei)):raise DexExecutorError('La revisión de cancelación ya tiene otros datos')
            return {**_public(db,row,terms,state,now),'replayed':True}
        parent,source,pstate=_open(db,return_id)
        if parent.account_id!=account_id:raise DexExecutorError('La devolución pertenece a otra cuenta')
        stopped=_profile(db,parent,source,wallet,drafts,draft_id,snapshot,now);refs,prices=_posts(db,return_id)
        base=max([int(source['gas_price']),*prices])
        plan=plan_cancellation(snapshot,original_nonce=int(source['nonce']),original_gas_price=base,gas_price=gas_price,bump_percent=bump_percent,max_gas_wei=max_gas_wei,now=now)
        body={'schema':SCHEMA,'id':identifier,'account_id':account_id,'return_id':return_id,'draft_id':draft_id,
            'wallet_digest':source['wallet_digest'],'operator_digest':source['operator_digest'],'source_plan_digest':parent.payload_digest,
            'source_pending_digest':parent.state_digest,'nonce':source['nonce'],'original_gas_price':source['gas_price'],'base_gas_price':str(base),
            'gas_price':str(plan.gas_price),'bump_percent':bump_percent,'max_gas_wei':str(max_gas_wei),'anchor':_anchor(snapshot),
            'stopped':stopped,'previous_posts':refs,'expires_at':(now+timedelta(seconds=120)).isoformat()}
        raw,digest=_encoded({**body,'seal':_seal(db,'plan',body)})
        state=_sealed_state(db,digest,{'status':'PREPARED','history':[{'status':'PREPARED','at':now.isoformat()}],'tx_ref':None,'settlement':None})
        sraw,sdigest=_encoded(state)
        db.add(RealDexGasCancellationIntent(id=identifier,return_id=return_id,account_id=account_id,wallet_digest=source['wallet_digest'],operator_digest=source['operator_digest'],
            payload_json=raw,payload_digest=digest,state_json=sraw,state_digest=sdigest,status='PREPARED',created_at=now,updated_at=now));db.flush()
        result=view_cancellation(db,identifier,now=now);db.commit();return result
    except Exception:db.rollback();raise


def _compatible(terms,plan,now):
    if type(plan) is not CancellationPlan:raise DexExecutorError('Revisa la cancelación exacta')
    plan.validate(now)
    if (terms['nonce']!=str(plan.original_nonce) or terms['base_gas_price']!=str(plan.original_gas_price)
            or terms['gas_price']!=str(plan.gas_price) or terms['bump_percent']!=plan.bump_percent or terms['max_gas_wei']!=str(plan.max_gas_wei)
            or plan.snapshot.block_number<int(terms['anchor']['block_number'])
            or plan.snapshot.block_number==int(terms['anchor']['block_number']) and plan.snapshot.block_hash!=terms['anchor']['block_hash']):
        raise DexExecutorError('El nonce, precio o revisión de cancelación cambió')


def _advance(db,row,terms,state,status,now):
    now=_time(now)
    if now<row.updated_at or status not in TRANSITIONS.get(row.status,set()):raise DexExecutorError('La cancelación cambió de estado')
    state['status']=row.status=status;row.updated_at=now;state['history'].append({'status':status,'at':now.isoformat()})
    state=_sealed_state(db,row.payload_digest,state);row.state_json,row.state_digest=_encoded(state);db.flush()
    result=view_cancellation(db,row.id,now=now);db.commit();return result


def reserve_cancellation(db,identifier,*,account_id,wallet_address,draft_id,drafts,plan,plan_digest,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now);wallet=canonical_wallet_address(wallet_address)
    row,terms,state=_owned(db,identifier,account_id,wallet)
    if row.payload_digest!=plan_digest:raise DexExecutorError('La revisión de cancelación cambió')
    if row.status!='PREPARED':return {**_public(db,row,terms,state,now),'reserved':False}
    parent,source,_=_open(db,row.return_id);_compatible(terms,plan,now)
    stopped=_profile(db,parent,source,wallet,drafts,draft_id,plan.snapshot,now)
    if (terms['draft_id']!=draft_id or stopped!=terms['stopped'] or _posts(db,row.return_id)[0]!=terms['previous_posts']
            or now>=datetime.fromisoformat(terms['expires_at'])):raise DexExecutorError('La revisión caducó o hay otro intento; revisa de nuevo')
    return {**_advance(db,row,terms,state,'RESERVED',now),'reserved':True}


def record_signature(db,identifier,*,account_id,plan,raw,tx_hash,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now);row,terms,state=_owned(db,identifier,account_id)
    if row.status!='RESERVED':raise DexExecutorError('La cancelación no tiene reserva de firma')
    _compatible(terms,plan,now);verify_cancellation_signature(plan,raw,tx_hash)
    if terms['operator_digest']!=_identity(db,'wallet',plan.snapshot.operator) or terms['wallet_digest']!=_identity(db,'wallet',plan.snapshot.owner):raise DexExecutorError('Firma de otra operadora')
    state['tx_ref']=_identity(db,'tx',tx_hash)
    return {**_advance(db,row,terms,state,'SIGNED',now),'signature_recorded':True}


def mark_broadcast(db,identifier,*,account_id,plan_digest,tx_hash,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now);row,terms,state=_owned(db,identifier,account_id)
    parent,source,_=original.return_record(db,row.return_id)
    if (row.status!='SIGNED' or row.payload_digest!=plan_digest or state['tx_ref']!=_identity(db,'tx',word(tx_hash))
            or parent.status!='BROADCAST_ATTEMPTED' or native.status(db,'GAS_RETURN',row.return_id) is not None
            or now>=datetime.fromisoformat(terms['expires_at']) or terms['stopped']!=original._stopped(db,row.operator_digest,now)
            or _posts(db,row.return_id)[0]!=terms['previous_posts']):raise DexExecutorError('La cancelación ya se concedió, caducó o cambió; no se repite')
    return {**_advance(db,row,terms,state,'BROADCAST_ATTEMPTED',now),'broadcast_granted':True}


def cancel_review(db,identifier,*,account_id,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=_owned(db,identifier,account_id)
    if row.status=='CANCELLED':return _public(db,row,terms,state,now)
    if row.status!='PREPARED':raise DexExecutorError('Sólo se retira una revisión sin reserva')
    return _advance(db,row,terms,state,'CANCELLED',now)


def abandon_unsent(db,identifier,*,account_id,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=_owned(db,identifier,account_id)
    if row.status=='ABANDONED_UNSENT':return _public(db,row,terms,state,now)
    if row.status not in {'RESERVED','SIGNED'}:raise DexExecutorError('Un envío concedido requiere su recibo; no se descarta')
    return _advance(db,row,terms,state,'ABANDONED_UNSENT',now)


def _known(db,row):
    parent,_,_=original.return_record(db,row.return_id)
    if parent.status in {'FINALIZED','REVERTED'}:return {'kind':'ORIGINAL','source_digest':parent.state_digest,'resolution_id':None,'winner_id':None}
    resolved=native.resolution_record(db,native.identifier('GAS_RETURN',row.return_id))
    if resolved is None:return None
    record,payload=resolved;winner=None
    for identifier in db.scalars(select(RealDexGasCancellationIntent.id).where(RealDexGasCancellationIntent.return_id==row.return_id)):
        candidate,terms,state=_base(db,identifier)
        if _broadcast_digest(db,candidate,terms,state) is not None and state['tx_ref']==payload['proof']['tx_ref']:winner=identifier
    return {'kind':'NATIVE','source_digest':record.payload_digest,'resolution_id':record.id,'winner_id':winner}


def _settle(db,row,terms,state,now):
    if row.status=='SETTLED':return _public(db,row,terms,state,now)
    known=_known(db,row)
    if known is None:raise DexExecutorError('Todavía no hay un recibo ganador verificado')
    state['settlement']=known;return _advance(db,row,terms,state,'SETTLED',now)


def settle_known(db,identifier,*,account_id,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=_owned(db,identifier,account_id)
    return _settle(db,row,terms,state,now)


def require_observation_hash(db,row,tx_hash):
    reference=_identity(db,'tx',word(tx_hash))
    for candidate in db.scalars(select(RealDexGasCancellationIntent.id).where(RealDexGasCancellationIntent.return_id==row.return_id)):
        action,review,grant=_base(db,candidate)
        if _broadcast_digest(db,action,review,grant) is not None and grant['tx_ref']==reference:return
    _,_,source=original.return_record(db,row.return_id)
    if original._identity(db,'tx',word(tx_hash))==source['tx_ref']:return
    raise DexExecutorError('Revisa ese reemplazo externo desde el historial de devoluciones')


def observe_cancellation(db,identifier,*,account_id,wallet_address,evidence,now):
    try:
        now=_time(now);row,terms,state=_owned(db,identifier,account_id,wallet_address)
        if row.status=='SETTLED':return _public(db,row,terms,state,now)
        if _known(db,row) is not None:
            db.commit();return settle_known(db,identifier,account_id=account_id,now=now)
        reference=_identity(db,'tx',word(evidence['tx_hash']));allowed=False
        for candidate in db.scalars(select(RealDexGasCancellationIntent.id).where(RealDexGasCancellationIntent.return_id==row.return_id)):
            action,review,grant=_base(db,candidate)
            if _broadcast_digest(db,action,review,grant) is not None and grant['tx_ref']==reference:allowed=True
        parent,source,pstate=original.return_record(db,row.return_id)
        if allowed:
            review=native.preview_resolution(db,'GAS_RETURN',row.return_id,account_id=account_id,wallet_address=wallet_address,evidence=evidence,accept_changes=True,now=now)
            if review['outcome']!='CANCELLED':raise DexExecutorError('El recibo no corresponde a una cancelación revisada')
            native.confirm_resolution(db,'GAS_RETURN',row.return_id,account_id=account_id,wallet_address=wallet_address,evidence=evidence,accept_changes=True,
                preview_digest=review['preview_digest'],source_state_digest=review['source_state_digest'],now=now,commit=False)
        elif original._identity(db,'tx',word(evidence['tx_hash']))==pstate['tx_ref']:
            plain={key:value for key,value in evidence.items() if key in {'chain_id','tx_hash','transaction','receipt','block','finalized'}}
            original.check_return(db,row.return_id,account_id=account_id,evidence=plain,now=now,commit=False)
        else:raise DexExecutorError('Revisa ese reemplazo externo desde el historial de devoluciones')
        row,terms,state=_owned(db,identifier,account_id,wallet_address)
        return _settle(db,row,terms,state,now)
    except Exception:db.rollback();raise


def verify_all_cancellations(db,*,version):
    if version<69:
        names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'real_dex_gas_cancellation_intents' in names and db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_gas_cancellation_intents').scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite cancelaciones BNB')
        return
    busy=set();hashes=set()
    for identifier in db.scalars(select(RealDexGasCancellationIntent.id)):
        row,terms,state=cancellation_record(db,identifier)
        if row.status in {'RESERVED','SIGNED'}:
            if row.return_id in busy:raise DexExecutorError('Dos reservas de cancelación para el mismo origen')
            busy.add(row.return_id)
        if _broadcast_digest(db,row,terms,state) is not None:
            if state['tx_ref'] in hashes:raise DexExecutorError('Dos concesiones del mismo envío de cancelación')
            hashes.add(state['tx_ref'])
