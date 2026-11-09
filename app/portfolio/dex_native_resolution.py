"""Native receipt resolutions: immutable originals, no implicit resend or capital."""
from datetime import datetime
import hashlib,hmac
from uuid import UUID,uuid5
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_native_resolution import verify_resolution,validate_proof,signer
from app.live_execution.evm_transaction import word
from app.portfolio.dex_deployments import _identity
from app.portfolio.models import RealDexNativeResolution,RealDexDeploymentResolution
from app.portfolio.wallet_funding import _encoded,_decoded,_time
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA='orbis.dex.native.resolution.v1'
KINDS=('GAS_FUNDING','GAS_RETURN')
NAMESPACE=UUID('f796c496-ce26-44d1-a78f-dce063ee98fa')
PENDING={'GAS_FUNDING':{'WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED'},'GAS_RETURN':{'BROADCAST_ATTEMPTED'}}


def _version(db):return db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()


def identifier(kind,source_id):
    if kind not in KINDS:raise DexExecutorError('Fuente BNB no compatible')
    return str(uuid5(NAMESPACE,kind+':'+str(UUID(source_id))))


def source_record(db,kind,source_id):
    if kind not in KINDS:raise DexExecutorError('Fuente BNB no compatible')
    from app.portfolio.dex_gas_funding import funding_record
    from app.portfolio.dex_gas_return import return_record
    return (funding_record if kind=='GAS_FUNDING' else return_record)(db,str(UUID(source_id)))


def _seal(db,payload):return hmac.new(_salt(db),b'orbis.dex.native.resolution.v1:'+_encoded(payload)[0].encode(),hashlib.sha256).hexdigest()


def resolution_record(db,resolution_id):
    if _version(db)<68:return None
    row=db.get(RealDexNativeResolution,str(UUID(resolution_id)))
    if row is None:return None
    try:
        payload=_decoded(row.payload_json,row.payload_digest);kind=payload['source_kind'];source_id=payload['source_id']
        parent,terms,state=source_record(db,kind,source_id)
        keys={'schema','id','source_kind','source_id','account_id','wallet_digest','signer_digest','source_plan_digest','source_state_digest',
            'proof','accept_changes','resolved_at','seal'}
        if (set(payload)!=keys or payload['schema']!=SCHEMA or payload['id']!=row.id or row.id!=identifier(kind,source_id)
                or row.funding_id!=(source_id if kind=='GAS_FUNDING' else None) or row.return_id!=(source_id if kind=='GAS_RETURN' else None)
                or payload['account_id']!=row.account_id or parent.account_id!=row.account_id or payload['wallet_digest']!=row.wallet_digest
                or row.wallet_digest!=terms['wallet_digest'] or row.signer_digest!=signer(terms,kind) or payload['signer_digest']!=row.signer_digest
                or row.nonce!=terms['nonce'] or parent.status not in PENDING[kind] or parent.updated_at>row.created_at
                or payload['source_plan_digest']!=parent.payload_digest or payload['source_state_digest']!=parent.state_digest
                or type(payload['accept_changes']) is not bool or row.updated_at!=row.created_at
                or _time(datetime.fromisoformat(payload['resolved_at']))!=row.created_at
                or not hmac.compare_digest(payload['seal'],_seal(db,{key:value for key,value in payload.items() if key!='seal'}))):raise ValueError('identity/parent')
        proof=payload['proof'];validate_proof(proof,terms,state,kind,parent,row.created_at)
        if row.status!=proof['receipt']['status'] or (proof['fee_overrun'] or proof['amount_changed']) and not payload['accept_changes']:raise ValueError('review/status')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):raise DexExecutorError('La resolución BNB cambió o perdió su origen') from None
    return row,payload


def public_record(record):
    row,payload=record;proof=payload['proof']
    return {'id':row.id,'source_kind':payload['source_kind'],'source_id':payload['source_id'],'resolved':True,'outcome':proof['outcome'],
        'receipt':proof['receipt'],'fee_overrun':proof['fee_overrun'],'amount_changed':proof['amount_changed'],
        'resolved_at':payload['resolved_at'],'automatic_execution':False}


def status(db,kind,source_id):
    record=resolution_record(db,identifier(kind,source_id));return public_record(record) if record else None


def require_unresolved(db,kind,source_id):
    if status(db,kind,source_id) is not None:raise DexExecutorError('La solicitud BNB ya está resuelta; actualiza su historial')


def _context(db,kind,source_id,account,wallet):
    if _version(db)<68:raise DexExecutorError('Actualiza la bóveda para resolver reemplazos BNB')
    row,terms,state=source_record(db,kind,source_id)
    if row.account_id!=str(UUID(account)) or terms['wallet_digest']!=_identity(db,'wallet',canonical_wallet_address(wallet)):
        raise DexExecutorError('La solicitud BNB pertenece a otra cuenta o wallet')
    return row,terms,state


def preview_resolution(db,kind,source_id,*,account_id,wallet_address,evidence,accept_changes=False,now):
    now=_time(now)
    if type(accept_changes) is not bool:raise DexExecutorError('Aceptación de cambios no válida')
    row,terms,state=_context(db,kind,source_id,account_id,wallet_address);prior=resolution_record(db,identifier(kind,source_id))
    if prior:
        if prior[1]['proof']['tx_ref']!=_identity(db,'tx',word(evidence['tx_hash'])):raise DexExecutorError('La solicitud se resolvió con otra transacción')
        return public_record(prior)
    if row.status not in PENDING[kind]:raise DexExecutorError('Esta solicitud BNB no necesita resolver un reemplazo')
    proof=verify_resolution(db,row,terms,state,kind,evidence,now)
    value={'id':identifier(kind,source_id),'source_kind':kind,'source_id':row.id,'resolved':False,'account_id':row.account_id,
        'source_plan_digest':row.payload_digest,'source_state_digest':row.state_digest,'proof':proof,'outcome':proof['outcome'],'receipt':proof['receipt'],
        'fee_overrun':proof['fee_overrun'],'amount_changed':proof['amount_changed'],'accept_changes':accept_changes,
        'available':accept_changes or not (proof['fee_overrun'] or proof['amount_changed']),
        'reviewed_max_gas_wei':terms['max_gas_wei'],'reviewed_gas_price_wei':terms['gas_price'],'reviewed_amount_wei':terms['amount_wei'],'automatic_execution':False}
    return {**value,'preview_digest':_encoded(value)[1]}


def require_unused(db,signer_digest,nonce,tx_ref,*,exclude=None):
    if _version(db)<68:return
    for row in db.scalars(select(RealDexNativeResolution)):
        if row.id==exclude:continue
        _,payload=resolution_record(db,row.id)
        if row.signer_digest==signer_digest and row.nonce==nonce or payload['proof']['tx_ref']==tx_ref:
            raise DexExecutorError('Este nonce o reemplazo BNB ya corresponde a otra solicitud')
    if _version(db)>=71:
        from app.portfolio.dex_call_resolution import require_unused as require_call_unused
        require_call_unused(db,signer_digest,nonce,tx_ref)


def _check_deployments(db,reference,nonce,tx_ref):
    from app.portfolio.dex_deployment_resolution import resolution_record as deployment_resolution
    for row in db.scalars(select(RealDexDeploymentResolution)):
        _,payload=deployment_resolution(db,row.id)
        if row.wallet_digest==reference and payload['proof']['nonce']==nonce or payload['proof']['tx_ref']==tx_ref:
            raise DexExecutorError('Este nonce o reemplazo ya resolvió un despliegue')


def confirm_resolution(db,kind,source_id,*,account_id,wallet_address,evidence,preview_digest,source_state_digest,accept_changes=False,now,commit=True):
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now)
        parent,terms,state=_context(db,kind,source_id,account_id,wallet_address);resolution_id=identifier(kind,source_id)
        prior=resolution_record(db,resolution_id)
        if prior:
            if prior[1]['proof']['tx_ref']!=_identity(db,'tx',word(evidence['tx_hash'])):raise DexExecutorError('Reemplazo distinto del ya resuelto')
            return public_record(prior)
        proposal=preview_resolution(db,kind,source_id,account_id=account_id,wallet_address=wallet_address,evidence=evidence,accept_changes=accept_changes,now=now)
        if proposal['preview_digest']!=preview_digest or parent.state_digest!=source_state_digest:raise DexExecutorError('La solicitud o el recibo cambió; revisa de nuevo')
        if not proposal['available']:raise DexExecutorError('Acepta los cambios de gas o importe respecto a la revisión original')
        proof=proposal['proof'];reference=signer(terms,kind)
        require_unused(db,reference,terms['nonce'],proof['tx_ref']);_check_deployments(db,reference,terms['nonce'],proof['tx_ref'])
        body={'schema':SCHEMA,'id':resolution_id,'source_kind':kind,'source_id':parent.id,'account_id':parent.account_id,
            'wallet_digest':terms['wallet_digest'],'signer_digest':reference,'source_plan_digest':parent.payload_digest,'source_state_digest':parent.state_digest,
            'proof':proof,'accept_changes':accept_changes,'resolved_at':now.isoformat()}
        raw,digest=_encoded({**body,'seal':_seal(db,body)})
        db.add(RealDexNativeResolution(id=resolution_id,funding_id=parent.id if kind=='GAS_FUNDING' else None,return_id=parent.id if kind=='GAS_RETURN' else None,
            account_id=parent.account_id,wallet_digest=terms['wallet_digest'],signer_digest=reference,nonce=terms['nonce'],status=proof['receipt']['status'],
            payload_json=raw,payload_digest=digest,created_at=now,updated_at=now));db.flush()
        result=public_record(resolution_record(db,resolution_id))
        if commit:db.commit()
        return result
    except Exception:db.rollback();raise


def receipt_source(db,resolution_id):
    record=resolution_record(db,resolution_id)
    if record is None:raise DexExecutorError('Resolución BNB no encontrada')
    row,payload=record;proof=payload['proof']
    return row,{'id':row.id,'wallet_digest':row.wallet_digest,'signer_digest':row.signer_digest,'nonce':row.nonce,
        'action':'CANCEL' if proof['outcome']=='CANCELLED' else payload['source_kind']},{'receipt':proof['receipt']}


def verify_all_resolutions(db,*,version):
    if version<68:
        tables={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'real_dex_native_resolutions' in tables and db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_native_resolutions').scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite resoluciones BNB')
        return
    seen=set();hashes=set()
    for raw in db.scalars(select(RealDexNativeResolution)):
        row,payload=resolution_record(db,raw.id);proof=payload['proof'];key=(row.signer_digest,row.nonce)
        if key in seen or proof['tx_ref'] in hashes:raise DexExecutorError('Nonce o reemplazo BNB duplicado')
        seen.add(key);hashes.add(proof['tx_ref']);_check_deployments(db,row.signer_digest,row.nonce,proof['tx_ref'])
