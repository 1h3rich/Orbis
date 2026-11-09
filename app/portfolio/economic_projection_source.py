"""Authenticated booked receipt → pure economic-order claim.

A witness binds the supplemental order proof, the original booking and every
original event, including registration metadata. Capturing it requires the
currently linked wallet; reopening it needs no address, RPC or trading key.
These witnesses are inputs to a reviewed projection, never ledger authority.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.dex_receipt_order import order_record
from app.portfolio.dex_verified_read import scope
from app.portfolio.economic_projection import ChainOrderClaim, OrderClaim, ProjectionError
from app.portfolio.models import RealEvent, RealMexcAccountLink, RealWalletIdentitySalt
from app.portfolio.repository import _digest, _from_row
from app.portfolio.wallet_funding import _encoded, _time
from app.portfolio.wallet_funding_credit import _wallet_owner
from app.portfolio.wallet_proof import canonical_wallet_address


SCHEMA = 'orbis.portfolio.booked.order.witness.v1'


def _existing_salt(db: Session) -> bytes:
    # The general wallet helper creates a salt on first use. A verification
    # port must instead reject missing identity without creating any row.
    if db.new or db.dirty or db.deleted:
        raise ProjectionError('Guarda o descarta los cambios pendientes antes de verificar')
    row = db.get(RealWalletIdentitySalt, 1)
    try:
        if row is None:
            raise ValueError('missing identity')
        value = bytes.fromhex(row.salt_hex)
        if len(value) != 32:
            raise ValueError('identity length')
        return value
    except (ValueError, TypeError):
        raise ProjectionError('Falta la identidad original de las pruebas DEX') from None


def _seal(db: Session, body: dict) -> str:
    return hmac.new(_existing_salt(db), (SCHEMA + ':').encode() + _encoded(body)[0].encode(), hashlib.sha256).hexdigest()


def _booking(db: Session, order):
    row, payload = order
    if row.source_kind == 'EXTERNAL_OBSERVATION':
        from app.portfolio.economic_external_booking import booking_record, identifier
        record=booking_record(db,identifier(row.observation_id))
        if record is None: raise ProjectionError('Falta el booking externo')
        return record
    if row.source_kind == 'WALLET_SWAP':
        from app.portfolio.wallet_swap_booking import booking_record
        record = booking_record(db, payload['source_id'])
    else:
        from app.portfolio.dex_receipt_booking import booking_id, booking_record
        record = booking_record(db, booking_id(row.source_kind, payload['source_id']))
    if record is None:
        raise ProjectionError('Contabiliza el recibo antes de conciliar su orden')
    return record


def _members(db: Session, booking) -> list[dict]:
    booked, original = booking
    members = []
    for reference in original['events']:
        event_row = db.get(RealEvent, reference['id'])
        if event_row is None or event_row.account_id != booked.account_id:
            raise ProjectionError('El recibo perdió un asiento de su cuenta')
        event = _from_row(event_row)
        if event_row.payload_digest != _digest(event.input):
            raise ProjectionError('Un asiento del recibo cambió')
        members.append({
            'id': event.id, 'role': reference['role'], 'input_digest': event_row.payload_digest,
            'account_id': event.account_id, 'request_id': event.request_id,
            'recorded_at': event.recorded_at.isoformat(),
        })
    members.sort(key=lambda member: member['id'])
    return members


def _body(db: Session, order, booking) -> dict:
    row, payload = order
    booked, _ = booking
    members = _members(db, booking)
    trades = [member for member in members if member['role'] == 'TRADE']
    gas = [member for member in members if member['role'] == 'GAS']
    if len(trades) != 1 or not gas or len(trades) + len(gas) != len(members):
        raise ProjectionError('La conciliación requiere una operación DEX y su gas')
    return {
        'schema': SCHEMA, 'order_record_id': row.id, 'order_record_digest': row.payload_digest,
        'source_kind': row.source_kind, 'source_id': payload['source_id'],
        'context_account_id': row.account_id, 'account_id': booked.account_id,
        'owner_digest': row.wallet_digest,
        'booking_id': booked.action_id if row.source_kind == 'WALLET_SWAP' else booked.id,
        'booking_digest': booked.payload_digest, 'events': members,
    }


def capture_booked_order(db: Session, order_record_id: str, *, wallet_address: str) -> dict:
    """Capture target ownership while the actual linked wallet is available.

    In particular, legacy wallet bookings cannot supply a keyed target binding
    by themselves. Do not infer that binding from their public payload hash.
    """
    from app.portfolio.dex_deployments import _identity
    from app.portfolio.dex_receipt_order_source import order_source

    wallet = canonical_wallet_address(wallet_address)
    _existing_salt(db)
    with scope(db):
        record = order_record(db, str(UUID(order_record_id)))
        if record is None:
            raise ProjectionError('Comprueba y guarda primero el orden del recibo')
        row, payload = record
        live = order_source(db, row.source_kind, payload['source_id'], wallet_address=wallet)
        owner = _wallet_owner(db, wallet)
        booking = _booking(db, record)
        if (owner is None or owner.id != booking[0].account_id
                or live.owner_digest != row.wallet_digest
                or row.wallet_digest != _identity(db, 'wallet', wallet)
                or db.scalar(select(RealMexcAccountLink.account_ref).where(
                    RealMexcAccountLink.account_id == owner.id)) is not None):
            raise ProjectionError('El recibo y sus asientos deben pertenecer a la wallet vinculada')
        body = _body(db, record, booking)
        return {**body, 'seal': _seal(db, body)}


def is_gas_witness(witness: dict) -> bool:
    from app.portfolio.economic_gas_source import SCHEMA as GAS_SCHEMA
    return isinstance(witness, dict) and witness.get('schema') == GAS_SCHEMA


def is_external_witness(witness: dict) -> bool:
    return isinstance(witness,dict) and witness.get('schema')=='orbis.portfolio.external.booked.order.witness.v1'


def witness_record(db: Session, witness: dict):
    """Select a sealed profile explicitly, never by an ID or a missing row."""
    if is_external_witness(witness):
        from app.portfolio.dex_external_order import order_record as external_order
        record=external_order(db,witness['order_record_id'])
    elif is_gas_witness(witness):
        from app.portfolio.dex_gas_order import gas_order_record
        record = gas_order_record(db, witness['order_record_id'])
    elif isinstance(witness, dict) and witness.get('schema') == SCHEMA:
        record = order_record(db, witness['order_record_id'])
    else:
        raise ProjectionError('Perfil de prueba contable no compatible')
    if record is None:
        raise ProjectionError('La revisión perdió la prueba original de orden')
    return record


def capture_witness(db: Session, witness: dict, *, wallet_address: str) -> dict:
    if is_external_witness(witness):
        from app.portfolio.economic_external_booking import capture_external_witness
        return capture_external_witness(db,witness['order_record_id'],wallet_address=wallet_address)
    if is_gas_witness(witness):
        from app.portfolio.economic_gas_source import capture_booked_gas_order
        return capture_booked_gas_order(db, witness['order_record_id'], wallet_address=wallet_address)
    if not isinstance(witness, dict) or witness.get('schema') != SCHEMA:
        raise ProjectionError('Perfil de prueba contable no compatible')
    return capture_booked_order(db, witness['order_record_id'], wallet_address=wallet_address)


def seal_witness(db: Session, body: dict) -> str:
    if is_external_witness(body):
        from app.portfolio.economic_external_booking import seal,WITNESS
        return seal(db,body,domain=WITNESS)
    if is_gas_witness(body):
        from app.portfolio.economic_gas_source import _seal as gas_seal
        return gas_seal(db, body)
    if body.get('schema') != SCHEMA:
        raise ProjectionError('Perfil de prueba contable no compatible')
    return _seal(db, body)


def claim_from_witness(db: Session, witness: dict) -> OrderClaim:
    """Authenticate and reopen all original dependencies on every read."""
    if is_external_witness(witness):
        from app.portfolio.economic_external_booking import claim_from_external_witness
        return claim_from_external_witness(db,witness)
    if is_gas_witness(witness):
        from app.portfolio.economic_gas_source import gas_claim_from_witness
        return gas_claim_from_witness(db, witness)
    try:
        if not isinstance(witness, dict):
            raise ValueError('witness type')
        body = {key: value for key, value in witness.items() if key != 'seal'}
        if (body.get('schema') != SCHEMA or not isinstance(witness.get('seal'), str)
                or not hmac.compare_digest(witness['seal'], _seal(db, body))):
            raise ValueError('witness seal')
        with scope(db):
            record = order_record(db, str(UUID(body['order_record_id'])))
            if record is None or _body(db, record, _booking(db, record)) != body:
                raise ValueError('witness dependencies')
            row, payload = record
            proof = payload['proof']
            trade = next(member['id'] for member in body['events'] if member['role'] == 'TRADE')
            gas = tuple(member['id'] for member in body['events'] if member['role'] == 'GAS')
            return ChainOrderClaim(
                source_kind=row.source_kind, source_id=payload['source_id'],
                order_record_id=row.id, order_record_digest=row.payload_digest,
                context_account_id=row.account_id, account_id=body['account_id'],
                block_number=int(proof['block_number']), transaction_index=int(proof['transaction_index']),
                block_at=_time(datetime.fromisoformat(proof['block_at'])), block_hash=proof['block_hash'],
                block_transactions_ref=proof['block_transactions_ref'],
                block_transaction_count=int(proof['block_transaction_count']),
                signer_digest=row.signer_digest, nonce=int(row.nonce),
                trade_event_id=trade, gas_event_ids=gas,
            )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, StopIteration):
        raise ProjectionError('La prueba contable de orden cambió o perdió sus originales') from None
