"""Sealed gas booking witnesses; no artificial trade, principal or SQL writes.

Capture authenticates the currently linked target wallet. Archive reopening
rechecks the original order, booking and event metadata without that address.
The profile has its own seal domain and cannot replace a trade witness.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.dex_deployments import _identity
from app.portfolio.dex_gas_order import gas_order_record
from app.portfolio.dex_gas_order_source import gas_source
from app.portfolio.dex_verified_read import scope
from app.portfolio.economic_projection import GasOrderClaim, ProjectionError
from app.portfolio.economic_projection_source import _booking, _existing_salt, _members
from app.portfolio.models import RealMexcAccountLink
from app.portfolio.wallet_funding import _encoded, _time
from app.portfolio.wallet_funding_credit import _wallet_owner
from app.portfolio.wallet_proof import canonical_wallet_address


SCHEMA = 'orbis.portfolio.booked.gas.order.witness.v1'


def _seal(db: Session, body: dict) -> str:
    return hmac.new(_existing_salt(db), (SCHEMA + ':').encode() + _encoded(body)[0].encode(), hashlib.sha256).hexdigest()


def _body(db: Session, order, booking) -> dict:
    row, payload = order
    booked, _ = booking
    members = _members(db, booking)
    if not 1 <= len(members) <= 100 or any(member['role'] != 'GAS' for member in members):
        raise ProjectionError('La prueba de gas requiere exclusivamente sus comisiones originales')
    return {
        'schema': SCHEMA, 'order_record_id': row.id, 'order_record_digest': row.payload_digest,
        'source_kind': row.source_kind, 'source_id': payload['source_id'],
        'context_account_id': row.account_id, 'account_id': booked.account_id,
        'owner_digest': row.wallet_digest,
        'booking_id': booked.action_id if row.source_kind == 'WALLET_SWAP' else booked.id,
        'booking_digest': booked.payload_digest, 'events': members,
    }


def capture_booked_gas_order(db: Session, order_record_id: str, *, wallet_address: str) -> dict:
    wallet = canonical_wallet_address(wallet_address)
    _existing_salt(db)
    with scope(db, force_full=True):
        record = gas_order_record(db, str(UUID(order_record_id)))
        if record is None:
            raise ProjectionError('Comprueba y guarda primero el orden del gas')
        row, payload = record
        live = gas_source(db, row.source_kind, payload['source_id'], wallet_address=wallet)
        booking = _booking(db, record)
        owner = _wallet_owner(db, wallet)
        if (owner is None or owner.id != booking[0].account_id
                or live.owner_digest != row.wallet_digest
                or row.wallet_digest != _identity(db, 'wallet', wallet)
                or db.scalar(select(RealMexcAccountLink.account_ref).where(
                    RealMexcAccountLink.account_id == owner.id)) is not None):
            raise ProjectionError('El gas y sus asientos deben pertenecer a la wallet vinculada')
        body = _body(db, record, booking)
        return {**body, 'seal': _seal(db, body)}


def gas_claim_from_witness(db: Session, witness: dict) -> GasOrderClaim:
    try:
        if not isinstance(witness, dict):
            raise ValueError('witness type')
        body = {key: value for key, value in witness.items() if key != 'seal'}
        if (body.get('schema') != SCHEMA or not isinstance(witness.get('seal'), str)
                or not hmac.compare_digest(witness['seal'], _seal(db, body))):
            raise ValueError('witness seal')
        with scope(db, force_full=True):
            record = gas_order_record(db, str(UUID(body['order_record_id'])))
            if record is None or _body(db, record, _booking(db, record)) != body:
                raise ValueError('witness dependencies')
            row, payload = record
            proof = payload['proof']
            return GasOrderClaim(
                source_kind=row.source_kind, source_id=payload['source_id'],
                order_record_id=row.id, order_record_digest=row.payload_digest,
                context_account_id=row.account_id, account_id=body['account_id'],
                block_number=int(proof['block_number']), transaction_index=int(proof['transaction_index']),
                block_at=_time(datetime.fromisoformat(proof['block_at'])), block_hash=proof['block_hash'],
                block_transactions_ref=proof['block_transactions_ref'],
                block_transaction_count=int(proof['block_transaction_count']),
                signer_digest=row.signer_digest, nonce=int(row.nonce),
                gas_event_ids=tuple(member['id'] for member in body['events']),
            )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise ProjectionError('La prueba contable del gas cambió o perdió sus originales') from None
