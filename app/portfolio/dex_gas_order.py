"""Immutable encrypted gas-order journal. No events, RPC, signer or capital.

Callers finish their read-only network observation before entering this module.
The writer recaptures originals and admits against both gas/trade histories in
one BEGIN IMMEDIATE transaction. Lost responses recover the exact same row.
"""
from __future__ import annotations

from datetime import datetime
import hashlib,hmac
from uuid import UUID,uuid5
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_order import verify_gas_order
from app.portfolio.dex_deployments import _hex_ref
from app.portfolio.dex_gas_order_source import gas_source,gas_reference,archived_gas_source
from app.portfolio.dex_order_history import require_compatible_history
from app.portfolio.dex_verified_read import scope
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.models import RealDexGasOrderProof,GAS_ORDER_SOURCES
from app.portfolio.wallet_funding import _decoded,_encoded,_time


SCHEMA='orbis.dex.gas.order.record.v1'
REVIEW='orbis.dex.gas.order.review.v1'
NAMESPACE=UUID('2c9d97f6-ccf8-4a5c-8b54-a744c9d02e9d')
TABLE=RealDexGasOrderProof.__tablename__


def _schema(db:Session)->None:
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<75:
        raise DexExecutorError('Actualiza la bóveda para guardar pruebas de gas DEX')


def identifier(kind:str,source_id:str)->str:
    if kind not in GAS_ORDER_SOURCES:raise DexExecutorError('Fuente de orden de gas no compatible')
    return str(uuid5(NAMESPACE,kind+':'+str(UUID(source_id))))


def _seal(db:Session,body:dict)->str:
    return hmac.new(_existing_salt(db),(SCHEMA+':').encode()+_encoded(body)[0].encode(),hashlib.sha256).hexdigest()


def _source_id(row:RealDexGasOrderProof)->str:
    fields={kind:getattr(row,field)for kind,(field,_)in GAS_ORDER_SOURCES.items()}
    if row.source_kind not in fields or sum(value is not None for value in fields.values())!=1:
        raise ValueError('source foreign keys')
    value=fields[row.source_kind]
    if value is None or str(UUID(value))!=value:raise ValueError('source foreign key')
    return value


def gas_order_record(db:Session,record_id:str):
    _schema(db);row=db.get(RealDexGasOrderProof,str(UUID(record_id)))
    if row is None:return None
    try:
        source_id=_source_id(row);body=_decoded(row.payload_json,row.payload_digest)
        keys={'schema','id','source_kind','source_id','context_account_id','wallet_digest','signer_digest',
            'nonce','tx_digest','source_updated_at','proof','recorded_at','seal'}
        if (set(body)!=keys or body['schema']!=SCHEMA or body['id']!=row.id
                or row.id!=identifier(row.source_kind,source_id)or body['source_kind']!=row.source_kind
                or body['source_id']!=source_id or body['context_account_id']!=row.account_id
                or any(not _hex_ref(value)for value in (row.wallet_digest,row.signer_digest,row.tx_digest))
                or not hmac.compare_digest(body['seal'],_seal(db,{key:value for key,value in body.items()if key!='seal'}))):
            raise ValueError('record identity/seal')
        proof=body['proof'];source=archived_gas_source(db,row.source_kind,source_id,proof=proof)
        if (source.account_id!=row.account_id or body['wallet_digest']!=row.wallet_digest or row.wallet_digest!=source.owner_digest
                or body['signer_digest']!=row.signer_digest or row.signer_digest!=source.signer_digest
                or body['nonce']!=row.nonce or row.nonce!=proof['nonce']
                or body['tx_digest']!=row.tx_digest or row.tx_digest!=proof['common_tx_ref']
                or body['source_updated_at']!=source.updated_at.isoformat()
                or body['recorded_at']!=row.created_at.isoformat()or row.created_at<source.updated_at
                or row.block_number!=proof['block_number']or row.block_hash!=proof['block_hash']
                or row.transaction_index!=proof['transaction_index']):raise ValueError('source/coordinates')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('El registro de orden del gas cambió o perdió sus originales')from None
    return row,body


def public_record(record)->dict:
    row,body=record
    return {'id':row.id,'source_kind':row.source_kind,'source_id':body['source_id'],'context_account_id':row.account_id,
        'recorded_at':row.created_at.isoformat(),'digest':row.payload_digest,'block_number':row.block_number,
        'transaction_index':row.transaction_index,'proof':body['proof'],'saved':True,
        'applied_to_ledger':False,'trading_authority':False}


def _review(db:Session,kind:str,source_id:str,*,context_account_id:str,wallet_address:str,evidence:dict,now:datetime):
    _schema(db);source=gas_source(db,kind,source_id,wallet_address=wallet_address)
    if source.account_id!=str(UUID(context_account_id)):raise DexExecutorError('El gas pertenece a otro contexto')
    proof=verify_gas_order(source,evidence,reference=gas_reference(db,source),now=_time(now))
    body={'schema':REVIEW,'id':identifier(kind,source.id),'source_kind':kind,'source_id':source.id,
        'context_account_id':source.account_id,'source_updated_at':source.updated_at.isoformat(),
        'tx_digest':proof['common_tx_ref'],'proof':proof}
    return {**body,'preview_digest':_encoded(body)[1]},source


def _compatible(existing,review:dict)->bool:
    _,body=existing
    return (body['proof']==review['proof']and body['tx_digest']==review['tx_digest']
        and body['context_account_id']==review['context_account_id']and body['source_updated_at']==review['source_updated_at'])


def preview_gas_order(db:Session,kind:str,source_id:str,*,context_account_id:str,wallet_address:str,evidence:dict,now:datetime)->dict:
    with scope(db):
        review,_=_review(db,kind,source_id,context_account_id=context_account_id,wallet_address=wallet_address,evidence=evidence,now=now)
        existing=gas_order_record(db,review['id'])
        if existing is not None and not _compatible(existing,review):raise DexExecutorError('Ya existe otra prueba para ese gas')
        return {**review,'already_saved':existing is not None,'applied_to_ledger':False,'trading_authority':False}


def confirm_gas_order(db:Session,kind:str,source_id:str,*,context_account_id:str,wallet_address:str,
                      evidence:dict,preview_digest:str,now:datetime)->dict:
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now)
        with scope(db):
            review,source=_review(db,kind,source_id,context_account_id=context_account_id,wallet_address=wallet_address,evidence=evidence,now=now)
            if review['preview_digest']!=preview_digest:raise DexExecutorError('El origen o la prueba del gas cambió; revisa de nuevo')
            existing=gas_order_record(db,review['id'])
            if existing is not None:
                if not _compatible(existing,review):raise DexExecutorError('Ya existe otra prueba para ese gas')
                result={**public_record(existing),'created':False};db.commit();return result
            proof=review['proof'];require_compatible_history(db,proof)
            body={'schema':SCHEMA,'id':review['id'],'source_kind':kind,'source_id':source.id,'context_account_id':source.account_id,
                'wallet_digest':source.owner_digest,'signer_digest':source.signer_digest,'nonce':proof['nonce'],
                'tx_digest':proof['common_tx_ref'],'source_updated_at':source.updated_at.isoformat(),'proof':proof,'recorded_at':now.isoformat()}
            raw,digest=_encoded({**body,'seal':_seal(db,body)})
            field=GAS_ORDER_SOURCES[kind][0]
            db.add(RealDexGasOrderProof(id=body['id'],source_kind=kind,**{field:source.id},account_id=source.account_id,
                wallet_digest=source.owner_digest,signer_digest=source.signer_digest,nonce=proof['nonce'],tx_digest=proof['common_tx_ref'],
                block_number=proof['block_number'],transaction_index=proof['transaction_index'],block_hash=proof['block_hash'],
                payload_json=raw,payload_digest=digest,created_at=now))
            db.flush();result={**public_record(gas_order_record(db,body['id'])),'created':True};db.commit();return result
    except Exception:db.rollback();raise
