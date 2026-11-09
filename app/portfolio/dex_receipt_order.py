"""Sealed supplemental order witnesses, without ledger or trading authority.

RPC observations arrive complete before entering this module. Confirmation
revalidates the source and commits one immutable witness under BEGIN IMMEDIATE.
Archive reads require no wallet address, network, signer or secret transaction.
"""
from __future__ import annotations

from datetime import datetime
import hashlib,hmac
from uuid import UUID,uuid5

from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_receipt_order import verify_order
from app.portfolio.dex_deployments import _hex_ref
from app.portfolio.dex_receipt_order_source import order_source,archived_order_source,order_reference
from app.portfolio.dex_verified_read import scope
from app.portfolio.models import RealDexReceiptOrderProof
from app.portfolio.wallet_funding import _decoded,_encoded,_time
from app.portfolio.wallet_links import _salt
from app.portfolio.dex_order_history import _less,_greater,require_compatible_history,verify_history


SCHEMA='orbis.dex.receipt.order.record.v1'
REVIEW='orbis.dex.receipt.order.review.v1'
NAMESPACE=UUID('f7164727-78af-4135-8e88-9bfc2cad3ec9')
KINDS=('WALLET_SWAP','OPERATOR','CALL_RESOLUTION')
TABLE=RealDexReceiptOrderProof.__tablename__


def _version(db: Session) -> int:return db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()


def _schema(db: Session) -> None:
    if _version(db)<73:raise DexExecutorError('Actualiza la bóveda para guardar pruebas de orden DEX')


def identifier(kind: str,source_id: str) -> str:
    if kind not in KINDS:raise DexExecutorError('Origen de orden DEX no compatible')
    return str(uuid5(NAMESPACE,kind+':'+str(UUID(source_id))))


def _seal(db: Session,body: dict) -> str:
    return hmac.new(_salt(db),(SCHEMA+':').encode()+_encoded(body)[0].encode(),hashlib.sha256).hexdigest()


def _source_id(row: RealDexReceiptOrderProof) -> str:
    fields={'WALLET_SWAP':row.swap_id,'OPERATOR':row.operator_id,'CALL_RESOLUTION':row.resolution_id}
    if row.source_kind not in fields or sum(value is not None for value in fields.values())!=1:
        raise ValueError('source foreign keys')
    value=fields[row.source_kind]
    if value is None or str(UUID(value))!=value:raise ValueError('source foreign key')
    return value


def order_record(db: Session,record_id: str):
    _schema(db);row=db.get(RealDexReceiptOrderProof,str(UUID(record_id)))
    if row is None:return None
    try:
        source_id=_source_id(row);payload=_decoded(row.payload_json,row.payload_digest)
        keys={'schema','id','source_kind','source_id','context_account_id','wallet_digest','signer_digest',
            'nonce','tx_digest','source_updated_at','proof','recorded_at','seal'}
        if (set(payload)!=keys or payload['schema']!=SCHEMA or payload['id']!=row.id
                or row.id!=identifier(row.source_kind,source_id) or payload['source_kind']!=row.source_kind
                or payload['source_id']!=source_id or payload['context_account_id']!=row.account_id
                or any(not _hex_ref(value) for value in (row.wallet_digest,row.signer_digest,row.tx_digest))
                or not hmac.compare_digest(payload['seal'],_seal(db,{key:value for key,value in payload.items() if key!='seal'}))):
            raise ValueError('record identity/seal')
        proof=payload['proof'];source=archived_order_source(db,row.source_kind,source_id,proof=proof)
        if (source.account_id!=row.account_id or payload['wallet_digest']!=row.wallet_digest or row.wallet_digest!=source.owner_digest
                or payload['signer_digest']!=row.signer_digest or row.signer_digest!=source.signer_digest
                or payload['nonce']!=row.nonce or row.nonce!=proof['nonce']
                or payload['tx_digest']!=row.tx_digest or row.tx_digest!=proof['common_tx_ref']
                or payload['source_updated_at']!=source.updated_at.isoformat()
                or payload['recorded_at']!=row.created_at.isoformat() or row.created_at<source.updated_at
                or row.block_number!=proof['block_number'] or row.block_hash!=proof['block_hash']
                or row.transaction_index!=proof['transaction_index']):raise ValueError('record source/coordinates')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('El registro de orden DEX cambió o perdió su recibo original') from None
    return row,payload


def public_record(record) -> dict:
    row,payload=record
    return {'id':row.id,'source_kind':row.source_kind,'source_id':payload['source_id'],
        'context_account_id':row.account_id,'recorded_at':row.created_at.isoformat(),'digest':row.payload_digest,
        'block_number':row.block_number,'transaction_index':row.transaction_index,'proof':payload['proof'],
        'saved':True,'applied_to_ledger':False,'trading_authority':False}


def _review(db: Session,kind: str,source_id: str,*,context_account_id: str,wallet_address: str,
            evidence: dict,now: datetime):
    _schema(db);source=order_source(db,kind,source_id,wallet_address=wallet_address)
    if source.account_id!=str(UUID(context_account_id)):raise DexExecutorError('El recibo pertenece a otro contexto')
    proof=verify_order(source,evidence,reference=order_reference(db,source),now=_time(now))
    tx_digest=proof['common_tx_ref']
    body={'schema':REVIEW,'id':identifier(kind,source.id),'source_kind':kind,'source_id':source.id,
        'context_account_id':source.account_id,'source_updated_at':source.updated_at.isoformat(),
        'tx_digest':tx_digest,'proof':proof}
    return {**body,'preview_digest':_encoded(body)[1]},source


def _compatible_record(existing,review: dict) -> bool:
    _,payload=existing
    return (payload['proof']==review['proof'] and payload['tx_digest']==review['tx_digest']
        and payload['context_account_id']==review['context_account_id']
        and payload['source_updated_at']==review['source_updated_at'])


def preview_order(db: Session,kind: str,source_id: str,*,context_account_id: str,wallet_address: str,
                  evidence: dict,now: datetime) -> dict:
    with scope(db):
        review,_=_review(db,kind,source_id,context_account_id=context_account_id,
            wallet_address=wallet_address,evidence=evidence,now=now)
        existing=order_record(db,review['id'])
        if existing is not None and not _compatible_record(existing,review):
            raise DexExecutorError('Ya existe una prueba distinta para ese origen DEX')
        return {**review,'already_saved':existing is not None,'applied_to_ledger':False,'trading_authority':False}


def _require_compatible_history(db: Session,review: dict) -> None:
    require_compatible_history(db,review['proof'])


def confirm_order(db: Session,kind: str,source_id: str,*,context_account_id: str,wallet_address: str,
                  evidence: dict,preview_digest: str,now: datetime) -> dict:
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE');now=_time(now)
        with scope(db):
            review,source=_review(db,kind,source_id,context_account_id=context_account_id,
                wallet_address=wallet_address,evidence=evidence,now=now)
            if review['preview_digest']!=preview_digest:raise DexExecutorError('El origen o la prueba de orden cambió; revisa de nuevo')
            existing=order_record(db,review['id'])
            if existing is not None:
                if not _compatible_record(existing,review):raise DexExecutorError('Ya existe otra prueba de orden para ese origen')
                result={**public_record(existing),'created':False};db.commit();return result
            _require_compatible_history(db,review);proof=review['proof']
            body={'schema':SCHEMA,'id':review['id'],'source_kind':kind,'source_id':source.id,
                'context_account_id':source.account_id,'wallet_digest':source.owner_digest,'signer_digest':source.signer_digest,
                'nonce':proof['nonce'],'tx_digest':review['tx_digest'],'source_updated_at':source.updated_at.isoformat(),
                'proof':proof,'recorded_at':now.isoformat()}
            raw,digest=_encoded({**body,'seal':_seal(db,body)})
            db.add(RealDexReceiptOrderProof(id=body['id'],source_kind=kind,swap_id=source.id if kind=='WALLET_SWAP' else None,
                operator_id=source.id if kind=='OPERATOR' else None,resolution_id=source.id if kind=='CALL_RESOLUTION' else None,
                account_id=source.account_id,wallet_digest=source.owner_digest,signer_digest=source.signer_digest,
                nonce=proof['nonce'],tx_digest=review['tx_digest'],block_number=proof['block_number'],
                transaction_index=proof['transaction_index'],block_hash=proof['block_hash'],
                payload_json=raw,payload_digest=digest,created_at=now))
            db.flush();result={**public_record(order_record(db,body['id'])),'created':True};db.commit();return result
    except Exception:db.rollback();raise


def verify_all_order_records(db: Session,*,version: int) -> None:
    verify_history(db,version=version)
