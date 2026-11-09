"""Verified call outcomes and chronological guards for local wallet accounting.

No RPC, signer, database writes or fabricated prices. A successful cancellation
receipt is a gas expense, never a swap. References use the common owner domain.
"""
from datetime import datetime

from sqlalchemy import select

from app.portfolio import models as m
from app.portfolio.dex_deployments import _identity
from app.portfolio.wallet_proof import canonical_wallet_address


def source_record(db,identifier):
    from app.portfolio.dex_call_resolution import resolution_record,source_record as original
    from app.portfolio.dex_receipt_booking import Source,DexBookingError,operator_trade_terms
    record=resolution_record(db,identifier)
    if record is None:raise DexBookingError('Resolución de llamada DEX no encontrada')
    row,payload=record;proof=payload['proof']
    parent,terms,_,_=original(db,payload['source_kind'],payload['source_id'])
    if payload['source_kind']=='OPERATOR':
        trade=operator_trade_terms(db,parent,terms)
    else:
        trade={'id':parent.id,'action':terms['action'],'token_in':terms['token_in'],'amount_units':terms['amount_units']}
    # Status FINALIZED describes EVM success. Only an EXECUTED known swap has
    # economic trade effects; self-cancel and failed arbitrary calls have none.
    if proof['outcome']!='EXECUTED':trade={'id':parent.id,'action':'APPROVE'}
    receipt={**proof['receipt'],**(proof['effect'] or {})}
    action=terms['action'] if proof['outcome']=='EXECUTED' else proof['outcome']
    if payload['source_kind']=='WALLET_SWAP' and action=='SWAP':action='BUY' if terms['token_in']=='USDT' else 'SELL'
    return Source('CALL_RESOLUTION',row.id,row.account_id,row.wallet_digest,row.payload_digest,
        receipt,trade,row.updated_at,action,'0',row.signer_digest,int(row.nonce),
        int(proof['transaction_index']),proof['outcome'],payload['source_kind'],parent.id)


def _financial(source):
    return source.trade_terms['action']=='SWAP' and source.receipt['status']=='FINALIZED'


def _wallet_source(db,row,terms,state,wallet):
    from app.portfolio.dex_receipt_booking import Source
    owner=_identity(db,'wallet',canonical_wallet_address(wallet))
    return Source('WALLET_SWAP',row.id,row.account_id,owner,row.payload_digest,state['receipt'],terms,
        row.updated_at,terms['action'],'0',owner,int(terms['nonce']))


def _sources(db,wallet,owner):
    from app.portfolio.dex_receipt_booking import source_record as load
    from app.portfolio.wallet_swap import swap_record,_identity as wallet_ref
    for row in db.scalars(select(m.RealDexOperatorIntent).where(m.RealDexOperatorIntent.status=='FINALIZED')):
        # Authenticate before using an alleged owner as a filter.
        source=load(db,'OPERATOR',row.id)
        if source.wallet_digest==owner and _financial(source):yield source
    digest=wallet_ref(db,'wallet',canonical_wallet_address(wallet))
    for row in db.scalars(select(m.RealWalletSwapIntent).where(m.RealWalletSwapIntent.status=='FINALIZED')):
        parent,terms,state=swap_record(db,row.id)
        if parent.wallet_digest==digest:
            source=_wallet_source(db,parent,terms,state,wallet)
            if _financial(source):yield source
    for row in db.scalars(select(m.RealDexCallResolution)):
        source=source_record(db,row.id)
        if source.wallet_digest==owner and _financial(source):yield source


def _booked(db,source):
    if source.kind=='WALLET_SWAP':
        from app.portfolio.wallet_swap_booking import _status
        return _status(db,source.id)['booked']
    from app.portfolio.dex_receipt_booking import status
    return status(db,source.kind,source.id)['booked']


def _compare(left,right):
    """Chain ordering, never UUID/HTTP arrival/booking time."""
    from app.portfolio.dex_receipt_booking import DexBookingError
    height_a,height_b=int(left.receipt['block_number']),int(right.receipt['block_number'])
    if height_a!=height_b:return -1 if height_a<height_b else 1
    if left.receipt['block_hash']!=right.receipt['block_hash']:
        raise DexBookingError('Los recibos del mismo bloque no conservan su hash')
    if left.transaction_index is not None and right.transaction_index is not None:
        if left.transaction_index==right.transaction_index:raise DexBookingError('Dos recibos reclaman el mismo índice de transacción')
        return -1 if left.transaction_index<right.transaction_index else 1
    if left.signer_digest==right.signer_digest and left.nonce!=right.nonce:
        return -1 if left.nonce<right.nonce else 1
    raise DexBookingError('Los recibos de distintas operadoras en el mismo bloque necesitan comprobar su orden')


def require_trade_order(db,source,wallet):
    """New resolution-aware guard; frozen old paths stay identical without one.

    Returns whether the new guard applies. A later trade already recorded at
    the same economic instant needs reconciliation rather than backdated IDs.
    """
    from app.portfolio.dex_receipt_booking import DexBookingError
    if not _financial(source):return source.kind=='CALL_RESOLUTION'
    owner=_identity(db,'wallet',canonical_wallet_address(wallet))
    if source.wallet_digest!=owner:raise DexBookingError('La secuencia pertenece a otra wallet')
    if source.kind!='CALL_RESOLUTION':
        from app.portfolio.dex_call_resolution import resolution_record
        matches=False
        for identifier in db.scalars(select(m.RealDexCallResolution.id)):
            record=resolution_record(db,identifier)
            matches=matches or record[0].wallet_digest==owner
        if not matches:return False
    for other in _sources(db,wallet,owner):
        if (other.kind,other.id)==(source.kind,source.id):continue
        order=_compare(other,source);booked=_booked(db,other)
        if order<0 and not booked:raise DexBookingError('Contabiliza primero la ejecución anterior '+other.id[:8])
        if order>0 and booked and datetime.fromisoformat(other.receipt['block_at'])==datetime.fromisoformat(source.receipt['block_at']):
            raise DexBookingError('Ya hay una ejecución posterior registrada en ese instante; concilia el orden antes de continuar')
    return True


def require_wallet_trade_order(db,row,terms,state,wallet):
    return require_trade_order(db,_wallet_source(db,row,terms,state,wallet),wallet)
