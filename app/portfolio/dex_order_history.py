"""Shared sealed chronology across legacy trade and gas-only proof tables.

Both writers call admission under BEGIN IMMEDIATE. Neither table can bypass
the other table's transaction, block position or signer/nonce constraints.
Archive checks authenticate originals and reject hidden downgraded records.
"""
from __future__ import annotations

from typing import Callable
from sqlalchemy import and_,or_,func,select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.models import RealDexReceiptOrderProof,RealDexGasOrderProof,RealDexExternalOrderProof
from app.portfolio.dex_verified_read import scope


OrderRow=RealDexReceiptOrderProof|RealDexGasOrderProof|RealDexExternalOrderProof
Record=tuple[OrderRow,dict]
Reader=Callable[[Session,str],Record|None]


def _less(column,value:str):
    # Canonical unsigned decimal text: no SQLite INTEGER/float conversion.
    return or_(func.length(column)<len(value),and_(func.length(column)==len(value),column<value))


def _greater(column,value:str):
    return or_(func.length(column)>len(value),and_(func.length(column)==len(value),column>value))


def _profiles(db:Session,version:int):
    from app.portfolio.dex_receipt_order import order_record
    from app.portfolio.dex_gas_order import gas_order_record
    for minimum,model,reader in ((73,RealDexReceiptOrderProof,order_record),(75,RealDexGasOrderProof,gas_order_record)):
        if version<minimum:
            name=model.__tablename__
            exists=db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(name,)).first()
            if exists is not None and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one():
                raise DexExecutorError('Una bóveda anterior no admite pruebas de orden DEX ocultas')
            continue
        yield model,reader
    if version<77:
        name=RealDexExternalOrderProof.__tablename__
        exists=db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(name,)).first()
        if exists is not None and db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one():
            raise DexExecutorError('Una bóveda anterior no admite pruebas de orden externo ocultas')
    else:
        from app.portfolio.dex_external_order import order_record as external_order_record
        yield RealDexExternalOrderProof,external_order_record


def require_compatible_history(db:Session,proof:dict)->None:
    """Caller owns a write transaction; every candidate is reauthenticated."""
    if not db.connection().connection.driver_connection.in_transaction:
        raise DexExecutorError('La prueba de orden necesita una transacción de escritura')
    version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    height,index,nonce=[proof[key]for key in ('block_number','transaction_index','nonce')]
    for model,reader in _profiles(db,version):
        earlier=or_(_less(model.block_number,height),and_(model.block_number==height,_less(model.transaction_index,index)))
        later=or_(_greater(model.block_number,height),and_(model.block_number==height,_greater(model.transaction_index,index)))
        wrong_nonce=and_(model.signer_digest==proof['signer_ref'],or_(model.nonce==nonce,
            and_(_less(model.nonce,nonce),~earlier),and_(_greater(model.nonce,nonce),~later)))
        query=select(model).where(or_(model.block_number==height,model.tx_digest==proof['common_tx_ref'],wrong_nonce))
        for raw in db.scalars(query):
            record=reader(db,raw.id)
            if record is None:raise DexExecutorError('La prueba de orden perdió su registro')
            row,body=record;other=body['proof']
            if (row.tx_digest==proof['common_tx_ref']or (row.block_number,row.transaction_index)==(height,index)
                    or row.signer_digest==proof['signer_ref']and (row.nonce==nonce
                        or (int(row.nonce)<int(nonce))!=((int(row.block_number),int(row.transaction_index))<(int(height),int(index))))):
                raise DexExecutorError('El recibo o nonce ya está fijado o contradice otra prueba de orden')
            if row.block_number==height and any(other[key]!=proof[key]for key in
                    ('block_hash','block_at','block_transaction_count','block_transactions_ref')):
                raise DexExecutorError('Las pruebas de orden no conservan el mismo bloque')


def verify_history(db:Session,*,version:int)->None:
    identities=set();signer_nonces=set();positions=set();blocks={};nonces={}
    with scope(db):
        for model,reader in _profiles(db,version):
            for raw in db.scalars(select(model)):
                record=reader(db,raw.id)
                if record is None:raise DexExecutorError('El archivo de orden perdió un registro')
                row,body=record;proof=body['proof'];height,index=int(row.block_number),int(row.transaction_index)
                if row.tx_digest in identities or (row.signer_digest,row.nonce)in signer_nonces or (height,index)in positions:
                    raise DexExecutorError('El archivo de orden DEX repite una identidad económica')
                anchor=tuple(proof[key]for key in ('block_hash','block_at','block_transaction_count','block_transactions_ref'))
                if height in blocks and blocks[height]!=anchor:raise DexExecutorError('El archivo de orden DEX contiene bloques contradictorios')
                blocks[height]=anchor;identities.add(row.tx_digest);signer_nonces.add((row.signer_digest,row.nonce));positions.add((height,index))
                nonces.setdefault(row.signer_digest,[]).append(((height,index),int(row.nonce)))
    for values in nonces.values():
        values.sort()
        if any(a[1]>=b[1]for a,b in zip(values,values[1:])):
            raise DexExecutorError('El archivo de orden DEX contradice la secuencia de nonces')
