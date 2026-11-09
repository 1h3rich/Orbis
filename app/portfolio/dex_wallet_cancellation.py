"""Sealed owner-wallet mandates; no RPC, wallet invocation, key or raw bytes."""
from dataclasses import fields
from datetime import datetime,timedelta
import hashlib,hmac
from uuid import UUID
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError,_integer
from app.live_execution.dex_wallet_cancellation import WalletCancellationState,WalletCancellationPlan,PendingFundingFee,plan_wallet_cancellation,GAS
from app.live_execution.evm_replacement_state import EoaReplacementState
from app.live_execution.evm_transaction import word,quantity
from app.portfolio import dex_gas_funding as original,dex_native_resolution as native
from app.portfolio.dex_deployments import _identity,_hex_ref
from app.portfolio.dex_operator_journal import _history
from app.portfolio.models import RealDexWalletCancellationIntent
from app.portfolio.wallet_funding import _encoded,_decoded,_time,_uint
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA='orbis.dex.wallet.cancellation.v1'
TRANSITIONS={'PREPARED':{'WALLET_REQUESTED','CANCELLED','SETTLED'},
    'WALLET_REQUESTED':{'HASH_REPORTED','WALLET_REJECTED','SETTLED'},'HASH_REPORTED':{'SETTLED'}}
STATES=set(TRANSITIONS)|{'CANCELLED','WALLET_REJECTED','SETTLED'}


def _schema(db):
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<70:raise DexExecutorError('Actualiza la bóveda antes de cancelar desde wallet')


def _seal(db,purpose,body):
    return hmac.new(_salt(db),(SCHEMA+':'+purpose+':').encode()+_encoded(body)[0].encode(),hashlib.sha256).hexdigest()


def _sealed_state(db,digest,state):
    body={key:value for key,value in state.items() if key!='seal'}
    return {**body,'seal':_seal(db,'state:'+digest,body)}


def _anchor(snapshot):
    return {field.name:(getattr(snapshot,field.name).isoformat() if isinstance(getattr(snapshot,field.name),datetime)
        else str(getattr(snapshot,field.name)) if type(getattr(snapshot,field.name)) is int else getattr(snapshot,field.name))
        for field in fields(EoaReplacementState) if field.name not in {'sender','counterparty'}}


def _read_anchor(value,at):
    expected={field.name for field in fields(EoaReplacementState)}-{'sender','counterparty'}
    if set(value)!=expected:raise ValueError('wallet cancellation anchor')
    data=dict(value)
    for key in ('block_number','latest_number','balance','latest_balance','nonce','latest_nonce','live_nonce','pending_nonce'):
        data[key]=_uint(value[key],key,maximum=2**64-1 if 'nonce' in key else 2**256-1,positive=False)
    for key in ('block_at','latest_at','observed_at'):data[key]=_time(datetime.fromisoformat(value[key]))
    result=EoaReplacementState('0x'+'11'*20,'0x'+'22'*20,**data);result.require_fresh(at)
    if _anchor(result)!=value:raise ValueError('canonical wallet anchor')
    return result


def _fee(snapshot):
    fee=snapshot.original_fee
    return None if fee is None else {'transaction_type':fee.transaction_type,'gas_limit':str(fee.gas_limit),
        'fee_cap':str(fee.fee_cap),'priority_fee':str(fee.priority_fee) if fee.priority_fee is not None else None}


def _source_claim(state):
    statuses=[event['status'] for event in state['history']]
    if 'WALLET_REQUESTED' not in statuses:raise ValueError('unclaimed original funding')
    return {**state,'status':'WALLET_REQUESTED','tx_ref':None,'receipt':None,'nonce_observation':None,
        'history':state['history'][:statuses.index('WALLET_REQUESTED')+1]}


def _read_review(terms,at):
    snapshot=_read_anchor(terms['anchor'],at);fee=None;value=terms['original_fee']
    if terms['original_hash_ref'] is not None and not _hex_ref(terms['original_hash_ref']):raise ValueError('original hash ref')
    dummy_hash='0x'+'11'*32 if terms['original_hash_ref'] is not None else None
    if value is not None:
        if set(value)!={'transaction_type','gas_limit','fee_cap','priority_fee'} or dummy_hash is None:raise ValueError('original fee shape')
        fee=PendingFundingFee(snapshot.sender,snapshot.counterparty,dummy_hash,int(terms['nonce']),int(terms['amount_wei']),
            value['transaction_type'],_uint(value['gas_limit'],'Gas original',maximum=100000),
            _uint(value['fee_cap'],'Precio original',maximum=10**12),
            _uint(value['priority_fee'],'Propina original',maximum=10**12,positive=False) if value['priority_fee'] is not None else None)
        fee.validate()
    state=WalletCancellationState(snapshot,int(terms['amount_wei']),dummy_hash,fee)
    return state


def _base(db,identifier):
    _schema(db);row=db.get(RealDexWalletCancellationIntent,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Cancelación desde wallet no encontrada')
    try:
        terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
        keys={'schema','id','account_id','funding_id','wallet_digest','operator_digest','source_plan_digest','source_claim_digest',
            'nonce','amount_wei','anchor','original_hash_ref','original_fee','gas_price','bump_percent','max_gas_wei','accept_unknown_fee',
            'previous_claims','expires_at','seal'}
        parent,source,pstate=original.funding_record(db,row.funding_id);claim=_source_claim(pstate)
        if (set(terms)!=keys or terms['schema']!=SCHEMA or terms['id']!=row.id or terms['account_id']!=row.account_id
                or row.account_id!=parent.account_id or terms['funding_id']!=row.funding_id
                or terms['wallet_digest']!=row.wallet_digest or row.wallet_digest!=source['wallet_digest']
                or terms['operator_digest']!=row.operator_digest or row.operator_digest!=source['operator_digest']
                or terms['source_plan_digest']!=parent.payload_digest or terms['source_claim_digest']!=_encoded(claim)[1]
                or parent.created_at>row.created_at or datetime.fromisoformat(claim['history'][-1]['at'])>row.created_at
                or terms['nonce']!=source['nonce'] or terms['amount_wei']!=source['amount_wei']
                or terms['expires_at']!=(row.created_at+timedelta(seconds=120)).isoformat()
                or not hmac.compare_digest(terms['seal'],_seal(db,'plan',{key:value for key,value in terms.items() if key!='seal'}))):raise ValueError('wallet source/plan')
        refs=terms['previous_claims']
        if (not isinstance(refs,list) or any(set(ref)!={'id','payload_digest','claim_digest'} or str(UUID(ref['id']))!=ref['id']
                or not _hex_ref(ref['payload_digest']) or not _hex_ref(ref['claim_digest']) for ref in refs)
                or len({ref['id'] for ref in refs})!=len(refs) or refs!=sorted(refs,key=lambda ref:ref['id'])):raise ValueError('previous wallet claims')
        reviewed=_read_review(terms,row.created_at)
        if (reviewed.snapshot.nonce!=int(terms['nonce']) or reviewed.snapshot.block_number<int(source['anchor']['block_number'])
                or reviewed.snapshot.block_number==int(source['anchor']['block_number']) and reviewed.snapshot.block_hash!=source['anchor']['block_hash']):
            raise ValueError('wallet anchor/source nonce')
        # Prices are authenticated against predecessor rows by cancellation_record.
        plan_wallet_cancellation(reviewed,gas_price=_uint(terms['gas_price'],'Precio',maximum=10**12),bump_percent=terms['bump_percent'],
            max_gas_wei=_uint(terms['max_gas_wei'],'Coste',maximum=10**17),accept_unknown_fee=terms['accept_unknown_fee'],
            previous_fees_unknown=bool(refs),now=row.created_at)
        if set(state)!={'status','history','tx_ref','settlement','seal'} or row.status not in STATES:raise ValueError('wallet state')
        statuses=_history(row,state,TRANSITIONS,'PREPARED')
        if (not hmac.compare_digest(state['seal'],_sealed_state(db,row.payload_digest,state)['seal'])
                or ('HASH_REPORTED' in statuses)!=(state['tx_ref'] is not None)
                or state['tx_ref'] is not None and not _hex_ref(state['tx_ref'])
                or (row.status=='SETTLED')!=(state['settlement'] is not None)
                or 'WALLET_REQUESTED' in statuses and datetime.fromisoformat(state['history'][statuses.index('WALLET_REQUESTED')]['at'])>=datetime.fromisoformat(terms['expires_at'])):
            raise ValueError('wallet seal/history')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):raise DexExecutorError('El diario de cancelación desde wallet no es coherente') from None
    return row,terms,state


def _claim_digest(db,row,terms,state):
    statuses=[event['status'] for event in state['history']]
    if 'WALLET_REQUESTED' not in statuses:return None
    prefix={**state,'status':'WALLET_REQUESTED','tx_ref':None,'settlement':None,'history':state['history'][:statuses.index('WALLET_REQUESTED')+1]}
    return _encoded(_sealed_state(db,row.payload_digest,prefix))[1]


def _claims(db,funding_id):
    refs=[];prices=[]
    for identifier in db.scalars(select(RealDexWalletCancellationIntent.id).where(RealDexWalletCancellationIntent.funding_id==funding_id)):
        row,terms,state=_base(db,identifier);digest=_claim_digest(db,row,terms,state)
        if digest is not None:refs.append({'id':row.id,'payload_digest':row.payload_digest,'claim_digest':digest});prices.append(int(terms['gas_price']))
    return sorted(refs,key=lambda ref:ref['id']),prices


def _covers(row,terms,state,proof,resolved_at):
    claimed=next((datetime.fromisoformat(event['at']) for event in state['history'] if event['status']=='WALLET_REQUESTED'),None)
    receipt=proof['receipt']
    return (claimed is not None and claimed<=resolved_at and datetime.fromisoformat(receipt['block_at'])>=claimed-timedelta(seconds=15)
        and int(receipt['block_number'])>int(terms['anchor']['latest_number'])
        and proof['outcome']=='CANCELLED' and receipt['transaction_type']==0 and receipt['gas_limit']==str(GAS)
        and receipt['fee_cap']==terms['gas_price'] and receipt['gas_fee_wei']==str(GAS*int(terms['gas_price'])))


def _winner(db,funding_id,proof,resolved_at):
    winner=None
    for identifier in db.scalars(select(RealDexWalletCancellationIntent.id).where(RealDexWalletCancellationIntent.funding_id==funding_id)):
        row,terms,state=_base(db,identifier)
        if state['tx_ref']==proof['tx_ref'] and _covers(row,terms,state,proof,resolved_at):
            if winner is not None:raise ValueError('duplicate winning wallet hash')
            winner=row.id
    return winner


def _settlement(db,row,terms,state):
    value=state['settlement']
    if value is None:return None
    if set(value)!={'kind','source_digest','resolution_id','winner_id'}:raise ValueError('wallet settlement shape')
    if value['kind']=='ORIGINAL':
        parent,_,pstate=original.funding_record(db,row.funding_id)
        if (parent.status not in {'FINALIZED','REVERTED'} or parent.state_digest!=value['source_digest'] or parent.updated_at>row.updated_at
                or value['resolution_id'] is not None or value['winner_id'] is not None
                or int(pstate['receipt']['block_number'])<=int(terms['anchor']['latest_number'])):raise ValueError('original wallet settlement')
        return {'winner':'ORIGINAL','winner_id':None,'resolution_id':None,'receipt':pstate['receipt']}
    if value['kind']!='NATIVE':raise ValueError('native wallet kind')
    resolved=native.resolution_record(db,value['resolution_id'])
    if resolved is None:raise ValueError('missing wallet resolution')
    record,payload=resolved
    if (payload['source_kind']!='GAS_FUNDING' or payload['source_id']!=row.funding_id or record.payload_digest!=value['source_digest']
            or record.created_at>row.updated_at):raise ValueError('wallet resolution source')
    winner=_winner(db,row.funding_id,payload['proof'],record.created_at)
    if winner!=value['winner_id']:raise ValueError('wallet winner link')
    return {'winner':'CANCELLATION' if winner else 'EXTERNAL','winner_id':winner,'resolution_id':record.id,'receipt':payload['proof']['receipt']}


def cancellation_record(db,identifier):
    row,terms,state=_base(db,identifier)
    try:
        prices=[]
        for ref in terms['previous_claims']:
            previous,review,grant=_base(db,ref['id'])
            at=next((datetime.fromisoformat(event['at']) for event in grant['history'] if event['status']=='WALLET_REQUESTED'),None)
            if (previous.id==row.id or previous.funding_id!=row.funding_id or previous.created_at>row.created_at or at is None or at>row.created_at
                    or previous.payload_digest!=ref['payload_digest'] or _claim_digest(db,previous,review,grant)!=ref['claim_digest']):raise ValueError('previous wallet request')
            prices.append(int(review['gas_price']))
        plan_wallet_cancellation(_read_review(terms,row.created_at),gas_price=int(terms['gas_price']),bump_percent=terms['bump_percent'],
            max_gas_wei=int(terms['max_gas_wei']),accept_unknown_fee=terms['accept_unknown_fee'],previous_prices=tuple(prices),
            previous_fees_unknown=bool(prices),now=row.created_at)
        _settlement(db,row,terms,state)
    except (ValueError,KeyError,TypeError,AttributeError):raise DexExecutorError('La revisión o recibo de cancelación desde wallet cambió') from None
    return row,terms,state


def _public(db,row,terms,state,now):
    settled=_settlement(db,row,terms,state)
    prices=[int(_base(db,ref['id'])[1]['gas_price']) for ref in terms['previous_claims']]
    return {'id':row.id,'funding_id':row.funding_id,'account_id':row.account_id,'status':row.status,'plan_digest':row.payload_digest,
        'nonce':terms['nonce'],'gas_price':int(terms['gas_price']),'gas_wei':str(GAS*int(terms['gas_price'])),'max_gas_wei':terms['max_gas_wei'],
        'bump_percent':terms['bump_percent'],'original_fee':terms['original_fee'],'original_fee_unknown':terms['original_fee'] is None,
        'original_hash_required':terms['original_hash_ref'] is not None,
        'accept_unknown_fee':terms['accept_unknown_fee'],'previous_prices':prices,'previous_fees_unknown':bool(prices),
        'request_attempted':any(event['status']=='WALLET_REQUESTED' for event in state['history']),
        'expires_at':terms['expires_at'],'expired':_time(now)>=datetime.fromisoformat(terms['expires_at']),
        'winner':settled['winner'] if settled else None,'winner_id':settled['winner_id'] if settled else None,
        'resolution_id':settled['resolution_id'] if settled else None,'receipt':settled['receipt'] if settled else None,'automatic_execution':False}


def view_cancellation(db,identifier,*,now):return _public(db,*cancellation_record(db,identifier),now)


def _owned(db,identifier,account,wallet=None):
    row,terms,state=cancellation_record(db,identifier)
    if row.account_id!=str(UUID(account)) or wallet is not None and row.wallet_digest!=_identity(db,'wallet',canonical_wallet_address(wallet)):
        raise DexExecutorError('Cancelación desde otra cuenta o wallet')
    return row,terms,state


def _profile(db,parent,source,snapshot,wallet,now):
    if type(snapshot) is not WalletCancellationState:raise DexExecutorError('Revisa el estado exacto de la financiación')
    snapshot.require_fresh(now);state=snapshot.snapshot
    if (state.sender!=wallet or source['wallet_digest']!=_identity(db,'wallet',wallet)
            or source['operator_digest']!=_identity(db,'wallet',state.counterparty) or source['nonce']!=str(state.nonce)
            or source['amount_wei']!=str(snapshot.original_amount_wei) or state.block_number<int(source['anchor']['block_number'])
            or state.block_number==int(source['anchor']['block_number']) and state.block_hash!=source['anchor']['block_hash']):
        raise DexExecutorError('La wallet, destinatario, nonce o importe original cambió')
    _,_,pstate=original.funding_record(db,parent.id)
    if snapshot.original_hash is not None and pstate['tx_ref'] is not None and pstate['tx_ref']!=original._identity(db,'tx',snapshot.original_hash):
        raise DexExecutorError('Conserva el hash original de la financiación')


def _open(db,funding_id,wallet):
    from app.portfolio.wallet_nonce_guard import assert_funding_cancellation_available
    parent,source,state=original.funding_record(db,funding_id)
    assert_funding_cancellation_available(db,wallet,source['nonce'],parent.id)
    return parent,source,state


def prepare_cancellation(db,*,identifier,funding_id,account_id,wallet_address,snapshot,gas_price,bump_percent,max_gas_wei,accept_unknown_fee,now):
    try:
        _integer(gas_price,'Precio',10**12);_integer(bump_percent,'Aumento mínimo',1000);_integer(max_gas_wei,'Coste máximo',10**17)
        if type(accept_unknown_fee) is not bool:raise DexExecutorError('Revisa la aceptación de tarifa desconocida')
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');_schema(db);now=_time(now)
        identifier,funding_id,account_id=map(lambda value:str(UUID(value)),(identifier,funding_id,account_id));wallet=canonical_wallet_address(wallet_address)
        if db.get(RealDexWalletCancellationIntent,identifier) is not None:
            row,terms,state=_owned(db,identifier,account_id,wallet)
            if (row.funding_id!=funding_id or terms['gas_price']!=str(gas_price) or terms['bump_percent']!=bump_percent
                    or terms['max_gas_wei']!=str(max_gas_wei) or type(accept_unknown_fee) is not bool or terms['accept_unknown_fee']!=accept_unknown_fee):
                raise DexExecutorError('La revisión desde wallet ya tiene otros términos')
            return {**_public(db,row,terms,state,now),'replayed':True}
        parent,source,pstate=_open(db,funding_id,wallet)
        if parent.account_id!=account_id:raise DexExecutorError('La financiación pertenece a otra cuenta')
        _profile(db,parent,source,snapshot,wallet,now);refs,prices=_claims(db,funding_id)
        plan=plan_wallet_cancellation(snapshot,gas_price=gas_price,bump_percent=bump_percent,max_gas_wei=max_gas_wei,
            accept_unknown_fee=accept_unknown_fee,previous_prices=tuple(prices),previous_fees_unknown=bool(prices),now=now)
        body={'schema':SCHEMA,'id':identifier,'account_id':account_id,'funding_id':funding_id,'wallet_digest':source['wallet_digest'],
            'operator_digest':source['operator_digest'],'source_plan_digest':parent.payload_digest,'source_claim_digest':_encoded(_source_claim(pstate))[1],
            'nonce':source['nonce'],'amount_wei':source['amount_wei'],'anchor':_anchor(snapshot.snapshot),
            'original_hash_ref':_identity(db,'tx',snapshot.original_hash) if snapshot.original_hash is not None else None,'original_fee':_fee(snapshot),
            'gas_price':str(plan.gas_price),'bump_percent':bump_percent,'max_gas_wei':str(max_gas_wei),'accept_unknown_fee':accept_unknown_fee,
            'previous_claims':refs,'expires_at':(now+timedelta(seconds=120)).isoformat()}
        raw,digest=_encoded({**body,'seal':_seal(db,'plan',body)})
        state=_sealed_state(db,digest,{'status':'PREPARED','history':[{'status':'PREPARED','at':now.isoformat()}],'tx_ref':None,'settlement':None})
        sraw,sdigest=_encoded(state)
        db.add(RealDexWalletCancellationIntent(id=identifier,funding_id=funding_id,account_id=account_id,wallet_digest=source['wallet_digest'],operator_digest=source['operator_digest'],
            payload_json=raw,payload_digest=digest,state_json=sraw,state_digest=sdigest,status='PREPARED',created_at=now,updated_at=now));db.flush()
        result=view_cancellation(db,identifier,now=now);db.commit();return result
    except Exception:db.rollback();raise


def _advance(db,row,terms,state,status,now,*,commit=True):
    now=_time(now)
    if now<row.updated_at or status not in TRANSITIONS.get(row.status,set()):raise DexExecutorError('La cancelación desde wallet cambió de estado')
    state['status']=row.status=status;row.updated_at=now;state['history'].append({'status':status,'at':now.isoformat()})
    state=_sealed_state(db,row.payload_digest,state);row.state_json,row.state_digest=_encoded(state);db.flush()
    result=view_cancellation(db,row.id,now=now)
    if commit:db.commit()
    return result


def claim_cancellation(db,identifier,*,account_id,wallet_address,plan,plan_digest,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now);wallet=canonical_wallet_address(wallet_address)
    row,terms,state=_owned(db,identifier,account_id,wallet)
    if row.payload_digest!=plan_digest:raise DexExecutorError('La revisión de cancelación desde wallet cambió')
    if row.status!='PREPARED':return {**_public(db,row,terms,state,now),'claim_granted':False}
    parent,source,_=_open(db,row.funding_id,wallet)
    if type(plan) is not WalletCancellationPlan:raise DexExecutorError('Revisa la cancelación exacta desde wallet')
    plan.validate(now);_profile(db,parent,source,plan.state,wallet,now)
    refs,prices=_claims(db,row.funding_id);current=plan.state.snapshot
    if (now>=datetime.fromisoformat(terms['expires_at']) or refs!=terms['previous_claims'] or plan.previous_prices!=tuple(prices)
            or plan.previous_fees_unknown!=bool(prices) or str(plan.gas_price)!=terms['gas_price'] or plan.bump_percent!=terms['bump_percent']
            or str(plan.max_gas_wei)!=terms['max_gas_wei'] or plan.accept_unknown_fee!=terms['accept_unknown_fee'] or _fee(plan.state)!=terms['original_fee']
            or (_identity(db,'tx',plan.state.original_hash) if plan.state.original_hash is not None else None)!=terms['original_hash_ref']
            or current.block_number<int(terms['anchor']['block_number'])
            or current.block_number==int(terms['anchor']['block_number']) and current.block_hash!=terms['anchor']['block_hash']):
        raise DexExecutorError('El origen, tarifa o revisión cambió; revisa otra vez')
    return {**_advance(db,row,terms,state,'WALLET_REQUESTED',now),'claim_granted':True,'transaction':plan.transaction()}


def _unique_hash(db,identifier,reference):
    for other in db.scalars(select(RealDexWalletCancellationIntent.id).where(RealDexWalletCancellationIntent.id!=identifier)):
        if _base(db,other)[2]['tx_ref']==reference:raise DexExecutorError('Este hash ya corresponde a otra cancelación')


def report_hash(db,identifier,*,account_id,tx_hash,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=_owned(db,identifier,account_id);reference=_identity(db,'tx',word(tx_hash))
    if row.status in {'HASH_REPORTED','SETTLED'} and state['tx_ref']==reference:return _public(db,row,terms,state,now)
    if row.status!='WALLET_REQUESTED':raise DexExecutorError('No hay una solicitud de wallet pendiente de hash')
    _unique_hash(db,row.id,reference);state['tx_ref']=reference
    return _advance(db,row,terms,state,'HASH_REPORTED',now)


def report_rejected(db,identifier,*,account_id,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=_owned(db,identifier,account_id)
    if row.status=='WALLET_REJECTED':return _public(db,row,terms,state,now)
    if row.status!='WALLET_REQUESTED':raise DexExecutorError('No hay una solicitud de wallet que registrar como rechazada')
    return _advance(db,row,terms,state,'WALLET_REJECTED',now)


def cancel_review(db,identifier,*,account_id,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=_owned(db,identifier,account_id)
    if row.status=='CANCELLED':return _public(db,row,terms,state,now)
    if row.status!='PREPARED':raise DexExecutorError('Una solicitud incierta de wallet no se descarta')
    return _advance(db,row,terms,state,'CANCELLED',now)


def _known(db,row):
    parent,_,_=original.funding_record(db,row.funding_id)
    if parent.status in {'FINALIZED','REVERTED'}:return {'kind':'ORIGINAL','source_digest':parent.state_digest,'resolution_id':None,'winner_id':None}
    resolved=native.resolution_record(db,native.identifier('GAS_FUNDING',row.funding_id))
    if resolved is None:return None
    record,payload=resolved
    return {'kind':'NATIVE','source_digest':record.payload_digest,'resolution_id':record.id,
        'winner_id':_winner(db,row.funding_id,payload['proof'],record.created_at)}


def _settle(db,row,terms,state,now):
    if row.status=='SETTLED':return _public(db,row,terms,state,now)
    known=_known(db,row)
    if known is None:raise DexExecutorError('Todavía no hay un recibo ganador verificado')
    state['settlement']=known;return _advance(db,row,terms,state,'SETTLED',now)


def settle_known(db,identifier,*,account_id,now):
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');return _settle(db,*_owned(db,identifier,account_id),now)


def observe_cancellation(db,identifier,*,account_id,wallet_address,evidence,now):
    try:
        now=_time(now);row,terms,state=_owned(db,identifier,account_id,wallet_address)
        if row.status=='SETTLED':return _public(db,row,terms,state,now)
        if _known(db,row) is not None:
            db.commit();return settle_known(db,identifier,account_id=account_id,now=now)
        if quantity(evidence['transaction']['value'])!=0:
            plain={key:value for key,value in evidence.items() if key in {'chain_id','tx_hash','transaction','receipt','block','finalized'}}
            original.check_funding(db,row.funding_id,account_id=account_id,evidence=plain,now=now,commit=False)
        else:
            preview=native.preview_resolution(db,'GAS_FUNDING',row.funding_id,account_id=account_id,wallet_address=wallet_address,evidence=evidence,accept_changes=True,now=now)
            reference=preview['proof']['tx_ref'];candidate=None
            for other in db.scalars(select(RealDexWalletCancellationIntent.id).where(RealDexWalletCancellationIntent.funding_id==row.funding_id)):
                action,review,grant=_base(db,other)
                if grant['tx_ref']==reference and _covers(action,review,grant,preview['proof'],now):candidate=action.id
            bind_current=(candidate is None and row.status=='WALLET_REQUESTED' and state['tx_ref'] is None
                and _covers(row,terms,state,preview['proof'],now))
            if candidate is None and not bind_current:raise DexExecutorError('La wallet modificó el envío o su tarifa; revisa el reemplazo explícitamente')
            native.confirm_resolution(db,'GAS_FUNDING',row.funding_id,account_id=account_id,wallet_address=wallet_address,evidence=evidence,
                accept_changes=True,preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],now=now,commit=False)
            if bind_current:
                _unique_hash(db,row.id,reference);state['tx_ref']=reference
                _advance(db,row,terms,state,'HASH_REPORTED',now,commit=False)
        return _settle(db,*_owned(db,identifier,account_id,wallet_address),now)
    except Exception:db.rollback();raise


def require_wallet_nonce_available(db,wallet,nonce,*,exclude_funding_id=None):
    reference=_identity(db,'wallet',canonical_wallet_address(wallet))
    for identifier in db.scalars(select(RealDexWalletCancellationIntent.id).where(RealDexWalletCancellationIntent.wallet_digest==reference)):
        row,terms,state=cancellation_record(db,identifier)
        if row.funding_id==exclude_funding_id:continue
        if _claim_digest(db,row,terms,state) is None or row.status=='WALLET_REJECTED':continue
        known=_known(db,row)
        if known is not None:
            if terms['nonce']==nonce:raise DexExecutorError('Este nonce ya corresponde a una financiación resuelta')
            continue
        if row.status=='WALLET_REQUESTED' or terms['nonce']==nonce:raise DexExecutorError('Hay una cancelación desde wallet sin resultado verificado')


def verify_all_cancellations(db,*,version):
    if version<70:
        names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'real_dex_wallet_cancellation_intents' in names and db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_wallet_cancellation_intents').scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite cancelaciones desde wallet')
        return
    hashes=set()
    for identifier in db.scalars(select(RealDexWalletCancellationIntent.id)):
        _,_,state=cancellation_record(db,identifier)
        if state['tx_ref'] is not None:
            if state['tx_ref'] in hashes:raise DexExecutorError('Hash de cancelación desde wallet duplicado')
            hashes.add(state['tx_ref'])
