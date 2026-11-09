"""Durable wallet CREATE journal. No keys, RPC sends or trading permissions."""
from datetime import datetime,timedelta
import hashlib,hmac,re
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import (DexExecutorError,ARTIFACT_SHA256,PROTOCOL,FEES,
    creation_transaction,executor_artifact)
from app.portfolio.models import RealAccount,RealDexDeploymentIntent,RealWalletIdentitySalt
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_funding import _encoded,_decoded,_time,_uint,exact_units


STATES={'PREPARED','WALLET_REQUESTED','HASH_REPORTED','WALLET_REJECTED','FINALIZED','REVERTED','CODE_OBSERVED'}


def _identity(db:Session,kind:str,value:str)->str:
    return hmac.new(_salt(db),f'dex-deployment-v1:{kind}:0x38:{value.lower()}'.encode(),hashlib.sha256).hexdigest()


def _hex_ref(value:object)->bool:
    return isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value) is not None


def deployment_record(db:Session,identifier:str)->tuple[RealDexDeploymentIntent,dict,dict]:
    row=db.get(RealDexDeploymentIntent,str(UUID(identifier)))
    if row is None:raise DexExecutorError('Despliegue DEX no encontrado')
    terms,state=_decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        if (set(terms)!={'schema','id','account_id','wallet_digest','chain_id','protocol','artifact_sha256','fee','nonce',
                'gas','gas_price','max_gas_wei','expires_at'} or terms['schema']!='orbis.dex.deployment.v1'
                or terms['id']!=row.id or terms['account_id']!=row.account_id or terms['wallet_digest']!=row.wallet_digest
                or terms['chain_id']!='0x38' or terms['protocol']!=PROTOCOL or terms['artifact_sha256']!=ARTIFACT_SHA256
                or not _hex_ref(row.wallet_digest) or type(terms['fee']) is not int or terms['fee'] not in FEES):raise ValueError('terms')
        UUID(terms['id']);UUID(terms['account_id']);executor_artifact()
        salt=db.get(RealWalletIdentitySalt,1)
        if salt is None or not _hex_ref(salt.salt_hex):raise ValueError('identity salt')
        _uint(terms['nonce'],'Nonce',maximum=2**64-1,positive=False)
        gas=_uint(terms['gas'],'Gas',maximum=10_000_000);price=_uint(terms['gas_price'],'Gas price',maximum=10**12)
        if gas*price>_uint(terms['max_gas_wei'],'Gas máximo'):raise ValueError('gas budget')
        if (set(state)!={'status','tx_ref','history','code_observation','receipt'} or row.status not in STATES
                or state['status']!=row.status):raise ValueError('state')
        history=state['history']
        if not isinstance(history,list) or not 1<=len(history)<=6 or any(set(event)!={'status','at'} for event in history):
            raise ValueError('history')
        statuses=[event['status'] for event in history]
        transitions={'PREPARED':{'WALLET_REQUESTED'},'WALLET_REQUESTED':{'HASH_REPORTED','WALLET_REJECTED','CODE_OBSERVED'},
            'HASH_REPORTED':{'FINALIZED','REVERTED','CODE_OBSERVED'},'CODE_OBSERVED':{'HASH_REPORTED','FINALIZED'}}
        if (statuses[0]!='PREPARED' or statuses[-1]!=row.status or len(statuses)!=len(set(statuses))
                or any(right not in transitions.get(left,set()) for left,right in zip(statuses,statuses[1:]))):raise ValueError('history')
        stamps=[_time(datetime.fromisoformat(event['at'])) for event in history]
        if (stamps!=sorted(stamps) or stamps[0]!=row.created_at or stamps[-1]!=row.updated_at
                or any(stamp.isoformat()!=event['at'] for stamp,event in zip(stamps,history))):raise ValueError('clock')
        if terms['expires_at']!=(row.created_at+timedelta(seconds=120)).isoformat():raise ValueError('expiry')
        hashed='HASH_REPORTED' in statuses
        if (hashed and not _hex_ref(state['tx_ref']) or not hashed and state['tx_ref'] is not None):raise ValueError('hash report')
        if row.status in {'FINALIZED','REVERTED'} and not hashed:raise ValueError('receipt without original hash')
        if ('CODE_OBSERVED' in statuses)!=(state['code_observation'] is not None):raise ValueError('code observation')
        if state['code_observation'] is not None:
            from app.portfolio.dex_deployment_receipt import validate_archived_code
            code_at=stamps[statuses.index('CODE_OBSERVED')]
            validate_archived_code(state['code_observation'],terms,row,code_at)
            if row.status=='REVERTED':raise ValueError('contradictory deployment')
        if row.status in {'FINALIZED','REVERTED'}:
            from app.portfolio.dex_deployment_receipt import validate_archived_receipt
            validate_archived_receipt(state['receipt'],terms,row)
            if state['code_observation'] is not None:
                code,receipt=state['code_observation'],state['receipt']
                if (int(code['block_number'])<int(receipt['block_number'])
                        or code['block_number']==receipt['block_number'] and code['block_hash']!=receipt['block_hash']):
                    raise ValueError('code observation contradicts receipt')
        elif state['receipt'] is not None:raise ValueError('unexpected receipt')
    except (KeyError,ValueError,TypeError,AttributeError):
        raise DexExecutorError('El registro de despliegue DEX no es coherente') from None
    return row,terms,state


def _public(row:RealDexDeploymentIntent,terms:dict,state:dict,now:datetime)->dict:
    return {'id':row.id,'plan_digest':row.payload_digest,'account_id':row.account_id,'chain_id':'0x38','protocol':PROTOCOL,
        'status':row.status,'fee':terms['fee'],'nonce':terms['nonce'],'estimated_gas_wei':str(int(terms['gas'])*int(terms['gas_price'])),
        'max_gas_wei':terms['max_gas_wei'],'expires_at':terms['expires_at'],'expired':now>=datetime.fromisoformat(terms['expires_at']),
        'created_at':row.created_at.isoformat(),'executor_verified':row.status=='FINALIZED' or state['code_observation'] is not None,
        'automatic_execution':False,'receipt':state['receipt'],'code_observation':state['code_observation']}


def prepare_deployment(db:Session,*,identifier:str,account_id:str,wallet_address:str,fee:int,
        nonce:str,gas:str,gas_price:str,max_gas_bnb:str,now:datetime)->dict:
    now=_time(now);identifier=str(UUID(identifier));account_id=str(UUID(account_id));wallet=canonical_wallet_address(wallet_address)
    maximum=exact_units(max_gas_bnb,18)
    tx=creation_transaction(wallet,fee,nonce=_uint(nonce,'Nonce',maximum=2**64-1,positive=False),
        gas=_uint(gas,'Gas',maximum=10_000_000),gas_price=_uint(gas_price,'Gas price',maximum=10**12))
    if int(gas)*int(gas_price)>maximum:raise DexExecutorError('La comisión máxima no cubre el despliegue')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<58:raise DexExecutorError('Actualiza la bóveda DEX')
    if db.get(RealAccount,account_id) is None:raise DexExecutorError('Cuenta REAL no encontrada')
    terms={'schema':'orbis.dex.deployment.v1','id':identifier,'account_id':account_id,'wallet_digest':_identity(db,'wallet',wallet),
        'chain_id':'0x38','protocol':PROTOCOL,'artifact_sha256':ARTIFACT_SHA256,'fee':fee,'nonce':nonce,'gas':gas,'gas_price':gas_price,
        'max_gas_wei':str(maximum),'expires_at':(now+timedelta(seconds=120)).isoformat()}
    row=db.get(RealDexDeploymentIntent,identifier)
    if row is not None:
        row,prior,state=deployment_record(db,identifier)
        if {k:v for k,v in terms.items() if k!='expires_at'}!={k:v for k,v in prior.items() if k!='expires_at'}:
            raise DexExecutorError('El ID de despliegue ya se usó con otros datos')
        terms=prior
    else:
        state={'status':'PREPARED','tx_ref':None,'history':[{'status':'PREPARED','at':now.isoformat()}],'code_observation':None,'receipt':None}
        raw,digest=_encoded(terms);state_raw,state_digest=_encoded(state)
        row=RealDexDeploymentIntent(id=identifier,account_id=account_id,wallet_digest=terms['wallet_digest'],payload_json=raw,
            payload_digest=digest,state_json=state_raw,state_digest=state_digest,status='PREPARED',created_at=now,updated_at=now)
        db.add(row);db.flush()
    result={**_public(row,terms,state,now),'transaction':tx};db.commit();return result


def list_deployments(db:Session,account_id:str,*,now:datetime)->list[dict]:
    if db.get(RealAccount,account_id) is None:raise DexExecutorError('Cuenta REAL no encontrada')
    rows=db.scalars(select(RealDexDeploymentIntent).where(RealDexDeploymentIntent.account_id==account_id)
        .order_by(RealDexDeploymentIntent.status.in_(['WALLET_REQUESTED','HASH_REPORTED','CODE_OBSERVED']).desc(),
            RealDexDeploymentIntent.created_at.desc(),RealDexDeploymentIntent.id).limit(100)).all()
    from app.portfolio.dex_deployment_resolution import status as resolution_status
    return [{**_public(*deployment_record(db,row.id),_time(now)),**({'resolution':resolved} if (resolved:=resolution_status(db,row.id)) else {})} for row in rows]


def _advance(db:Session,row:RealDexDeploymentIntent,state:dict,status:str,now:datetime,*,commit:bool=True)->None:
    now=_time(now)
    if now<row.updated_at:raise DexExecutorError('El reloj de despliegue retrocedió')
    row.status=state['status']=status;state['history'].append({'status':status,'at':now.isoformat()});row.updated_at=now
    row.state_json,row.state_digest=_encoded(state);db.flush();deployment_record(db,row.id)
    if commit:db.commit()


def claim_deployment(db:Session,identifier:str,*,account_id:str,wallet_address:str,plan_digest:str,now:datetime)->dict:
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    now=_time(now);wallet=canonical_wallet_address(wallet_address)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=deployment_record(db,identifier)
    if row.account_id!=account_id or _identity(db,'wallet',wallet)!=row.wallet_digest or row.payload_digest!=plan_digest:
        raise DexExecutorError('Wallet, cuenta o revisión de despliegue distinta')
    if row.status!='PREPARED':return {**_public(row,terms,state,now),'claim_granted':False}
    if now>=datetime.fromisoformat(terms['expires_at']):raise DexExecutorError('La revisión de despliegue caducó')
    assert_wallet_nonce_available(db,wallet,terms['nonce'])
    _advance(db,row,state,'WALLET_REQUESTED',now)
    return {**_public(row,terms,state,now),'claim_granted':True}


def report_deployment(db:Session,identifier:str,*,account_id:str,now:datetime,tx_hash:str|None=None,rejected:bool=False)->dict:
    """Bind the original wallet response once; never infer chain execution."""
    now=_time(now)
    if type(rejected) is not bool or (rejected and tx_hash is not None) or (not rejected and not isinstance(tx_hash,str)):
        raise DexExecutorError('Indica el hash original o el rechazo de la wallet')
    if tx_hash is not None and re.fullmatch(r'0x[0-9a-fA-F]{64}',tx_hash) is None:
        raise DexExecutorError('Hash de despliegue no válido')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=deployment_record(db,identifier)
    if row.account_id!=account_id:raise DexExecutorError('Cuenta de despliegue distinta')
    from app.portfolio.dex_deployment_resolution import require_unresolved
    require_unresolved(db,row.id)
    reference=_identity(db,'tx',tx_hash) if tx_hash else None
    target='WALLET_REJECTED' if rejected else 'HASH_REPORTED'
    if (row.status==target or not rejected and row.status in {'FINALIZED','REVERTED','CODE_OBSERVED'}) and state['tx_ref']==reference:
        return _public(row,terms,state,now)
    if row.status!='WALLET_REQUESTED' and not (row.status=='CODE_OBSERVED' and state['tx_ref'] is None and not rejected):
        raise DexExecutorError('La respuesta original ya se registró o no se solicitó')
    if reference is not None:
        for item in db.scalars(select(RealDexDeploymentIntent).where(RealDexDeploymentIntent.id!=row.id)):
            if deployment_record(db,item.id)[2]['tx_ref']==reference:
                raise DexExecutorError('La transacción ya corresponde a otro despliegue')
    state['tx_ref']=reference
    _advance(db,row,state,target,now)
    return _public(row,terms,state,now)


def check_deployment(db:Session,identifier:str,*,account_id:str,evidence:dict,now:datetime)->dict:
    from app.portfolio.dex_deployment_receipt import observe_receipt
    now=_time(now)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=deployment_record(db,identifier)
    if row.account_id!=account_id or row.status not in {'HASH_REPORTED','FINALIZED','REVERTED','CODE_OBSERVED'} or state['tx_ref'] is None:
        raise DexExecutorError('Registra primero el hash original de este despliegue')
    from app.portfolio.dex_deployment_resolution import require_unresolved
    require_unresolved(db,row.id)
    receipt=observe_receipt(db,row,terms,state,evidence,now)
    if row.status in {'FINALIZED','REVERTED'}:
        if receipt!=state['receipt']:raise DexExecutorError('El recibo de despliegue ya registrado es distinto')
        return _public(row,terms,state,now)
    state['receipt']=receipt
    _advance(db,row,state,receipt['status'],now)
    return _public(row,terms,state,now)


def recover_deployment(db:Session,identifier:str,*,account_id:str,wallet_address:str,evidence:dict,now:datetime)->dict:
    """Observe existing CREATE address; never resend or invent a receipt/gas fee."""
    from app.portfolio.dex_deployment_receipt import observe_code
    now=_time(now);wallet=canonical_wallet_address(wallet_address)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row,terms,state=deployment_record(db,identifier)
    if (row.account_id!=account_id or _identity(db,'wallet',wallet)!=row.wallet_digest
            or row.status not in {'WALLET_REQUESTED','HASH_REPORTED','CODE_OBSERVED'}):
        raise DexExecutorError('Revisa la solicitud original de esta wallet')
    from app.portfolio.dex_deployment_resolution import require_unresolved
    require_unresolved(db,row.id)
    observation=observe_code(db,row,terms,wallet,evidence,now)
    if state['code_observation'] is not None:
        if observation!=state['code_observation']:raise DexExecutorError('La observación original ya se archivó')
        return _public(row,terms,state,now)
    state['code_observation']=observation
    _advance(db,row,state,'CODE_OBSERVED',now)
    return _public(row,terms,state,now)
