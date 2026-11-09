"""Immutable supplemental external chronology, with no ledger/trading authority.

The HTTP port verifies the signature/RPC evidence outside the vault lease.
This writer reauthenticates the sealed observation/certificate and admits its
common identity against ALL order families in one write transaction.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_external_order import common_reference, validate_external_order
from app.portfolio import dex_call_resolution as original, dex_external_observation as journal
from app.portfolio.dex_deployments import _hex_ref
from app.portfolio.dex_order_history import require_compatible_history
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.models import RealDexExternalOrderProof
from app.portfolio.wallet_funding import _decoded, _encoded, _time

SCHEMA = 'orbis.dex.external.order.record.v1'
REVIEW = 'orbis.dex.external.order.review.v1'
NAMESPACE = UUID('a75b0a0c-f648-491f-831a-e203c4d14c46')


def identifier(observation_id: str) -> str:
    return str(uuid5(NAMESPACE, str(UUID(observation_id))))


def _seal(db: Session, body: dict) -> str:
    return hmac.new(_existing_salt(db), (SCHEMA+':').encode()+_encoded(body)[0].encode(), hashlib.sha256).hexdigest()


def _source(db: Session, observation_id: str):
    record = journal.observation_record(db, str(UUID(observation_id)))
    if record is None:
        raise DexExecutorError('Guarda primero la observación externa verificada')
    row, body = record
    _, _, _, source = original.source_record(db, body['source_kind'], body['source_id'])
    return row, body, source


def _validate(db: Session, observation_id: str, proof: dict):
    row, body, source = _source(db, observation_id)
    validate_external_order(source, row.id, row.payload_digest, body['proof'], proof,
        common=common_reference(_existing_salt(db)))
    if (proof['owner_digest'] != row.wallet_digest or proof['signer_digest'] != row.signer_digest
            or proof['common_tx_ref'] != row.tx_digest or proof['nonce'] != row.nonce):
        raise DexExecutorError('El orden externo no conserva la identidad común de su observación')
    return row, body


def order_record(db: Session, record_id: str):
    if original._version(db) < 77:
        return None
    row = db.get(RealDexExternalOrderProof, str(UUID(record_id)))
    if row is None:
        return None
    try:
        body = _decoded(row.payload_json, row.payload_digest)
        keys = {'schema', 'id', 'source_kind', 'source_id', 'context_account_id', 'observation_digest',
            'wallet_digest', 'signer_digest', 'nonce', 'tx_digest', 'proof', 'recorded_at', 'seal'}
        if (set(body) != keys or body['schema'] != SCHEMA or body['id'] != row.id
                or row.id != identifier(row.observation_id) or row.source_kind != 'EXTERNAL_OBSERVATION'
                or body['source_kind'] != row.source_kind or body['source_id'] != row.observation_id
                or body['context_account_id'] != row.account_id
                or any(body[key] != getattr(row, key) for key in ('wallet_digest', 'signer_digest', 'nonce', 'tx_digest'))
                or any(not _hex_ref(getattr(row, key)) for key in ('wallet_digest', 'signer_digest', 'tx_digest'))
                or body['recorded_at'] != row.created_at.isoformat()
                or not hmac.compare_digest(body['seal'], _seal(db, {k: v for k, v in body.items() if k != 'seal'}))):
            raise ValueError('record identity/seal')
        observation, _ = _validate(db, row.observation_id, body['proof']); proof = body['proof']
        if (body['observation_digest'] != observation.payload_digest or row.account_id != observation.account_id
                or row.wallet_digest != observation.wallet_digest or row.signer_digest != observation.signer_digest
                or row.nonce != observation.nonce or row.tx_digest != observation.tx_digest
                or row.created_at < observation.created_at or row.block_number != proof['block_number']
                or row.transaction_index != proof['transaction_index'] or row.block_hash != proof['block_hash']):
            raise ValueError('source/coordinates')
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('El registro de orden externo cambió o perdió su observación') from None
    return row, body


def _proposal(observation_id: str, account_id: str, observation_digest: str, proof: dict) -> dict:
    return {'schema': REVIEW, 'id': identifier(observation_id), 'source_kind': 'EXTERNAL_OBSERVATION',
        'source_id': observation_id, 'context_account_id': account_id,
        'observation_digest': observation_digest, 'proof': proof,
        'saved': False, 'applied_to_ledger': False, 'resolves_intent': False, 'trading_authority': False}


def preview(observation, proof: dict) -> dict:
    body = _proposal(observation.id, observation.account_id, observation.payload_digest, proof)
    return {**body, 'preview_digest': _encoded(body)[1]}


def public_record(record) -> dict:
    row, body = record
    return {'id': row.id, 'source_kind': row.source_kind, 'source_id': row.observation_id,
        'context_account_id': row.account_id, 'digest': row.payload_digest, 'proof': body['proof'],
        'preview_digest': _encoded(_proposal(row.observation_id, row.account_id, body['observation_digest'], body['proof']))[1],
        'recorded_at': row.created_at.isoformat(), 'saved': True, 'applied_to_ledger': False,
        'resolves_intent': False, 'trading_authority': False}


def save_order(db: Session, observation_id: str, *, account_id: str, proof: dict,
               preview_digest: str, now: datetime) -> dict:
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE'); now = _time(now)
        if original._version(db) < 77:
            raise DexExecutorError('Actualiza la bóveda para guardar el orden externo')
        observation, source = _validate(db, observation_id, proof)
        if observation.account_id != str(UUID(account_id)):
            raise DexExecutorError('Observación externa de otra cuenta')
        proposed = preview(observation, proof)
        if proposed['preview_digest'] != preview_digest:
            raise DexExecutorError('La observación o el orden cambió; revisa de nuevo')
        existing = order_record(db, proposed['id'])
        if existing is not None:
            if existing[1]['proof'] != proof:
                raise DexExecutorError('La observación ya tiene otro certificado de orden')
            result = public_record(existing); db.commit()
            return result
        if now < observation.created_at:
            raise DexExecutorError('El orden precede al guardado de su observación')
        if source['source_kind'] == 'OPERATOR':
            parent, _, _, _ = original.source_record(db, source['source_kind'], source['source_id'])
            # The observation already attests the fully checked historic run.
            # Saving chronology grants nothing; require current stop metadata,
            # without running financial replay inside this write lease.
            original._quiescence(db, parent, now, mode=source['quiescence']['mode'])
        require_compatible_history(db, {**proof, 'signer_ref': proof['signer_digest']})
        body = {'schema': SCHEMA, 'id': proposed['id'], 'source_kind': 'EXTERNAL_OBSERVATION',
            'source_id': observation.id, 'context_account_id': observation.account_id,
            'observation_digest': observation.payload_digest, 'wallet_digest': observation.wallet_digest,
            'signer_digest': observation.signer_digest, 'nonce': observation.nonce, 'tx_digest': observation.tx_digest,
            'proof': proof, 'recorded_at': now.isoformat()}
        raw, digest = _encoded({**body, 'seal': _seal(db, body)})
        db.add(RealDexExternalOrderProof(id=body['id'], source_kind=body['source_kind'], observation_id=observation.id,
            account_id=observation.account_id, wallet_digest=observation.wallet_digest, signer_digest=observation.signer_digest,
            nonce=observation.nonce, tx_digest=observation.tx_digest, block_number=proof['block_number'],
            transaction_index=proof['transaction_index'], block_hash=proof['block_hash'], payload_json=raw,
            payload_digest=digest, created_at=now))
        db.flush(); result = public_record(order_record(db, body['id'])); db.commit()
        return result
    except Exception:
        db.rollback()
        raise
