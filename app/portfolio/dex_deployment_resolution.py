"""Immutable resolution of an already mined CREATE replacement, with fee evidence."""
from datetime import datetime
import hashlib,hmac
from uuid import UUID
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.evm_transaction import word
from app.live_execution.dex_deployment_resolution import verify_resolution,validate_proof
from app.portfolio import dex_deployments as original
from app.portfolio.models import RealDexDeploymentResolution
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_funding import _decoded,_encoded,_time
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA='orbis.dex.deployment.resolution.v1'
PENDING={'WALLET_REQUESTED','HASH_REPORTED','CODE_OBSERVED'}


def _seal(db,body):return hmac.new(_salt(db),b'orbis.deployment.resolution.v1:'+_encoded(body)[0].encode(),hashlib.sha256).hexdigest()


def _version(db):return db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()


def resolution_record(db,identifier):
    if _version(db)<67:return None
    identifier=str(UUID(identifier));row=db.get(RealDexDeploymentResolution,identifier)
    if row is None:return None
    try:
        parent,terms,state=original.deployment_record(db,identifier);payload=_decoded(row.payload_json,row.payload_digest)
        keys={'schema','id','account_id','wallet_digest','source_plan_digest','source_state_before','source_state_before_digest',
            'source_state_after_digest','proof','accept_fee_overrun','resolved_at','seal'}
        if (set(payload)!=keys or payload['schema']!=SCHEMA or payload['id']!=row.id or payload['account_id']!=row.account_id
                or row.account_id!=parent.account_id or row.wallet_digest!=parent.wallet_digest or payload['wallet_digest']!=row.wallet_digest
                or payload['source_plan_digest']!=parent.payload_digest or payload['source_state_after_digest']!=parent.state_digest
                or payload['source_state_before_digest']!=_encoded(payload['source_state_before'])[1]
                or type(payload['accept_fee_overrun']) is not bool or row.created_at!=row.updated_at
                or _time(datetime.fromisoformat(payload['resolved_at']))!=row.created_at
                or not hmac.compare_digest(payload['seal'],_seal(db,{key:value for key,value in payload.items() if key!='seal'}))):raise ValueError('identity')
        proof=payload['proof'];validate_proof(proof,terms,parent,row.created_at)
        if row.status!=proof['receipt']['status'] or proof['fee_overrun'] and not payload['accept_fee_overrun']:raise ValueError('status or unaccepted fee')
        before=payload['source_state_before']
        if before['status'] not in PENDING or datetime.fromisoformat(before['history'][-1]['at'])>row.created_at:raise ValueError('original pending state')
        expected=before
        if proof['outcome']=='DEPLOYED' and before['code_observation'] is None:
            expected={**before,'status':'CODE_OBSERVED','code_observation':proof['code_observation'],
                'history':[*before['history'],{'status':'CODE_OBSERVED','at':payload['resolved_at']}]}
        if state!=expected:raise ValueError('original evidence changed')
        if proof['outcome']!='DEPLOYED' and before['code_observation'] is not None:raise ValueError('contradictory original code')
    except (ValueError,KeyError,TypeError,AttributeError):
        raise DexExecutorError('La resolución de despliegue cambió o contradice su origen') from None
    return row,payload


def public_record(record):
    row,payload=record;proof=payload['proof']
    return {'id':row.id,'deployment_id':row.id,'resolved':True,'outcome':proof['outcome'],'executor_verified':proof['outcome']=='DEPLOYED',
        'receipt':proof['receipt'],'fee_overrun':proof['fee_overrun'],'resolved_at':payload['resolved_at'],'automatic_execution':False}


def status(db,identifier):
    record=resolution_record(db,identifier)
    return public_record(record) if record else None


def require_unresolved(db,identifier):
    if resolution_record(db,identifier) is not None:raise DexExecutorError('La solicitud ya está resuelta; actualiza su historial')


def _context(db,identifier,account,wallet):
    if _version(db)<67:raise DexExecutorError('Actualiza la bóveda para resolver reemplazos')
    row,terms,state=original.deployment_record(db,str(UUID(identifier)))
    if row.account_id!=str(UUID(account)) or row.wallet_digest!=original._identity(db,'wallet',canonical_wallet_address(wallet)):
        raise DexExecutorError('La solicitud pertenece a otra cuenta o wallet')
    return row,terms,state


def preview_resolution(db,identifier,*,account_id,wallet_address,evidence,accept_fee_overrun=False,now):
    now=_time(now)
    if type(accept_fee_overrun) is not bool:raise DexExecutorError('Confirmación de gas no válida')
    row,terms,state=_context(db,identifier,account_id,wallet_address)
    stored=resolution_record(db,identifier)
    if stored:
        if stored[1]['proof']['tx_ref']!=original._identity(db,'tx',word(evidence['tx_hash'])):raise DexExecutorError('La solicitud se resolvió con otra transacción')
        return public_record(stored)
    if row.status not in PENDING:raise DexExecutorError('Esta solicitud no necesita resolución de reemplazo')
    proof=verify_resolution(db,row,terms,state,wallet_address,evidence,now)
    value={'id':row.id,'resolved':False,'account_id':row.account_id,'source_plan_digest':row.payload_digest,'source_state_digest':row.state_digest,
        'proof':proof,'outcome':proof['outcome'],'receipt':proof['receipt'],'executor_verified':proof['outcome']=='DEPLOYED',
        'fee_overrun':proof['fee_overrun'],'accept_fee_overrun':accept_fee_overrun,'available':not proof['fee_overrun'] or accept_fee_overrun,
        'reviewed_max_gas_wei':terms['max_gas_wei'],'reviewed_gas_price_wei':terms['gas_price'],'automatic_execution':False}
    return {**value,'preview_digest':_encoded(value)[1]}


def confirm_resolution(db,identifier,*,account_id,wallet_address,evidence,preview_digest,source_state_digest,accept_fee_overrun=False,now):
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now)
        row,terms,state=_context(db,identifier,account_id,wallet_address)
        existing=resolution_record(db,identifier)
        if existing:
            if existing[1]['proof']['tx_ref']!=original._identity(db,'tx',word(evidence['tx_hash'])):raise DexExecutorError('Reemplazo distinto del ya resuelto')
            return public_record(existing)
        proposal=preview_resolution(db,identifier,account_id=account_id,wallet_address=wallet_address,evidence=evidence,accept_fee_overrun=accept_fee_overrun,now=now)
        if proposal['source_state_digest']!=source_state_digest or proposal['preview_digest']!=preview_digest:raise DexExecutorError('La solicitud o el recibo cambió; revisa de nuevo')
        if not proposal['available']:raise DexExecutorError('Confirma el gas que supera la revisión original')
        before=_decoded(row.state_json,row.state_digest);before_digest=row.state_digest;proof=proposal['proof']
        from app.portfolio.dex_native_resolution import require_unused
        require_unused(db,row.wallet_digest,terms['nonce'],proof['tx_ref'])
        if proof['outcome']=='DEPLOYED' and state['code_observation'] is None:
            state['code_observation']=proof['code_observation']
            original._advance(db,row,state,'CODE_OBSERVED',now,commit=False)
        for other in db.scalars(select(RealDexDeploymentResolution).where(RealDexDeploymentResolution.wallet_digest==row.wallet_digest)):
            _,prior=resolution_record(db,other.id)
            if prior['proof']['nonce']==terms['nonce'] or prior['proof']['tx_ref']==proof['tx_ref']:
                raise DexExecutorError('Este reemplazo ya corresponde a otra solicitud')
        body={'schema':SCHEMA,'id':row.id,'account_id':row.account_id,'wallet_digest':row.wallet_digest,'source_plan_digest':row.payload_digest,
            'source_state_before':before,'source_state_before_digest':before_digest,'source_state_after_digest':row.state_digest,
            'proof':proof,'accept_fee_overrun':accept_fee_overrun,'resolved_at':now.isoformat()}
        payload={**body,'seal':_seal(db,body)};raw,digest=_encoded(payload)
        db.add(RealDexDeploymentResolution(id=row.id,account_id=row.account_id,wallet_digest=row.wallet_digest,status=proof['receipt']['status'],
            payload_json=raw,payload_digest=digest,created_at=now,updated_at=now));db.flush()
        result=public_record(resolution_record(db,identifier));db.commit();return result
    except Exception:db.rollback();raise


def receipt_source(db,identifier):
    row,payload=resolution_record(db,identifier) or (None,None)
    if row is None:raise DexExecutorError('Resolución no encontrada')
    _,terms,_=original.deployment_record(db,identifier);proof=payload['proof']
    action={'CANCELLED':'CANCEL','DEPLOYED':'REPLACED_CREATE','REVERTED':'REVERTED_CREATE'}[proof['outcome']]
    return row,{'id':row.id,'wallet_digest':row.wallet_digest,'nonce':terms['nonce'],'action':action},{'receipt':proof['receipt']}


def verify_all_resolutions(db,*,version):
    name='real_dex_deployment_resolutions'
    if version<67:
        tables={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if name in tables and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one():raise DexExecutorError('Una bóveda anterior no admite resoluciones de reemplazo')
        return
    seen=set();hashes=set()
    for row in db.scalars(select(RealDexDeploymentResolution)):
        _,payload=resolution_record(db,row.id);proof=payload['proof'];key=(row.wallet_digest,proof['nonce'])
        if key in seen or proof['tx_ref'] in hashes:raise DexExecutorError('Nonce o reemplazo duplicado')
        seen.add(key);hashes.add(proof['tx_ref'])
