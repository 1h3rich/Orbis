"""Sealed external execution journal, distinct from trade/financial resolution.

No RPC, signature, ledger entry, grant or source mutation. A consumed nonce
remains quarantined until a separately reviewed accounting record exists.
"""
from __future__ import annotations

import hashlib
import hmac
from uuid import UUID, uuid5

from sqlalchemy import select

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_external_observation import source_reference, validate_external_proof
from app.live_execution.evm_transaction import word
from app.portfolio import dex_call_resolution as original
from app.portfolio.dex_deployments import _hex_ref, _identity
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.models import RealDexExternalObservation
from app.portfolio.wallet_funding import _decoded, _encoded, _time
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA = 'orbis.dex.external.journal.v1'
PREVIEW = 'orbis.dex.external.preview.v1'
NAMESPACE = UUID('f66aba77-a15a-49b3-b468-7e4fbfbe9a63')
STATUS = 'EFFECTS_PENDING'


def identifier(kind, source_id):
    if kind not in original.KINDS:
        raise DexExecutorError('Fuente de observación externa no compatible')
    return str(uuid5(NAMESPACE, kind+':'+str(UUID(source_id))))


def preview(parent, kind, proof):
    value = {'schema': PREVIEW, 'source_kind': kind, 'source_id': parent.id,
        'account_id': parent.account_id, 'source_plan_digest': parent.payload_digest,
        'source_state_digest': parent.state_digest, 'source_status': parent.status,
        'observation': proof, 'observed': True, 'saved': False, 'resolves_intent': False,
        'moves_funds': False, 'trading_authority': False}
    return {**value, 'preview_digest': _encoded(value)[1]}


def _seal(db, body):
    return hmac.new(_existing_salt(db), (SCHEMA+':').encode()+_encoded(body)[0].encode(), hashlib.sha256).hexdigest()


def observation_record(db, observation_id):
    if original._version(db) < 76:
        return None
    row = db.get(RealDexExternalObservation, str(UUID(observation_id)))
    if row is None:
        return None
    try:
        body = _decoded(row.payload_json, row.payload_digest)
        keys = {'schema', 'id', 'source_kind', 'source_id', 'account_id', 'wallet_digest', 'signer_digest',
            'nonce', 'tx_digest', 'source_plan_digest', 'source_state_digest', 'source_status',
            'quiescence', 'proof', 'preview_digest', 'saved_at', 'status', 'seal'}
        kind = body['source_kind']; source_id = body['source_id']
        parent, terms, _, source = original.source_record(db, kind, source_id)
        if (set(body) != keys or body['schema'] != SCHEMA or body['id'] != row.id
                or row.id != identifier(kind, source_id) or row.source_kind != kind
                or row.swap_id != (source_id if kind == 'WALLET_SWAP' else None)
                or row.operator_id != (source_id if kind == 'OPERATOR' else None)
                or body['account_id'] != row.account_id or parent.account_id != row.account_id
                or any(body[key] != getattr(row, key) for key in ('wallet_digest', 'signer_digest', 'nonce', 'tx_digest', 'status'))
                or any(not _hex_ref(getattr(row, key)) for key in ('wallet_digest', 'signer_digest', 'tx_digest'))
                or row.status != STATUS or row.nonce != terms['nonce']
                or body['source_plan_digest'] != parent.payload_digest
                or body['source_state_digest'] != parent.state_digest
                or body['source_status'] != parent.status or parent.status not in original.PENDING[kind]
                or parent.updated_at > row.created_at or body['saved_at'] != row.created_at.isoformat()
                or not hmac.compare_digest(body['seal'], _seal(db, {k: v for k, v in body.items() if k != 'seal'}))):
            raise ValueError('source/identity/seal')
        if kind == 'WALLET_SWAP':
            if row.signer_digest != row.wallet_digest or body['quiescence'] is not None:
                raise ValueError('wallet signer')
        elif (row.wallet_digest != terms['owner_digest'] or row.signer_digest != terms['wallet_digest']
                or body['quiescence'] != original._quiescence(db, parent, row.created_at,
                    mode=body['quiescence']['mode'])):
            raise ValueError('operator stopped/owner')
        validate_external_proof(body['proof'], source,
            reference=source_reference(kind, _existing_salt(db)), now=row.created_at)
        if body['preview_digest'] != preview(parent, kind, body['proof'])['preview_digest']:
            raise ValueError('review identity')
        if original.resolution_record(db, original.identifier(kind, source_id)) is not None:
            raise ValueError('conflicting known outcome')
        # All shared nonce/tx columns use the deployment reference domain.
        for other in db.scalars(select(original.RealDexCallResolution)):
            original.resolution_record(db, other.id)
            if other.signer_digest == row.signer_digest and other.nonce == row.nonce or other.tx_digest == row.tx_digest:
                raise ValueError('conflicting call outcome')
        original._check_other_families(db, row.signer_digest, row.nonce, row.tx_digest)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('La observación externa cambió o perdió su origen verificado') from None
    return row, body


def public_record(record):
    row, body = record
    return {'schema': SCHEMA, 'id': row.id, 'source_kind': row.source_kind, 'source_id': body['source_id'],
        'account_id': row.account_id, 'status': row.status, 'observation': body['proof'],
        'preview_digest': body['preview_digest'], 'saved_at': body['saved_at'], 'saved': True,
        'nonce_consumed': True, 'effects_reviewed': False, 'accounting_complete': False,
        'resolves_intent': False, 'moves_funds': False, 'trading_authority': False}


def status(db, kind, source_id):
    record = observation_record(db, identifier(kind, source_id))
    return public_record(record) if record else None


def require_unobserved(db, kind, source_id):
    if status(db, kind, source_id) is not None:
        raise DexExecutorError('El nonce tiene una ejecución externa guardada; revisa sus efectos pendientes')


def require_unused(db, signer_digest, nonce, tx_digest):
    if original._version(db) < 76:
        return
    for raw in db.scalars(select(RealDexExternalObservation).where(
            ((RealDexExternalObservation.signer_digest == signer_digest) & (RealDexExternalObservation.nonce == nonce))
            | (RealDexExternalObservation.tx_digest == tx_digest))):
        observation_record(db, raw.id)
        raise DexExecutorError('Este nonce o recibo ya tiene una observación externa')


def require_actor_available(db, wallet, nonce=None):
    if original._version(db) < 76:
        return
    actor = _identity(db, 'wallet', canonical_wallet_address(wallet))
    for raw in db.scalars(select(RealDexExternalObservation).where(RealDexExternalObservation.signer_digest == actor)):
        _,body=observation_record(db, raw.id)
        from app.portfolio.dex_external_outcomes import external_outcome
        reviewed=external_outcome(db,body['source_kind'],body['source_id'])
        if reviewed is None:raise DexExecutorError('Esta wallet tiene efectos externos pendientes de revisión')
        if nonce is None or int(nonce)<=int(raw.nonce):
            raise DexExecutorError('El nonce externo ya está consumido; revisa un nonce posterior')


def save_observation(db, kind, source_id, *, account_id, wallet_address, signer_address, tx_hash,
                     proof, preview_digest, source_state_digest, now):
    """Atomic, idempotent save of a server-verified proof; no slow work in here."""
    try:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE'); now = _time(now)
        if original._version(db) < 76:
            raise DexExecutorError('Actualiza la bóveda para guardar observaciones externas')
        parent, terms, _, source = original._context(db, kind, source_id, account_id, wallet_address)
        reference = source_reference(kind, _existing_salt(db))
        validate_external_proof(proof, source, reference=reference, now=now)
        sender = canonical_wallet_address(signer_address); tx_hash = word(tx_hash)
        if reference('wallet', sender) != source.signer_digest or reference('tx', tx_hash) != proof['tx_ref']:
            raise DexExecutorError('La firma o recibo no coincide con la observación revisada')
        proposal = preview(parent, kind, proof)
        if proposal['preview_digest'] != preview_digest or parent.state_digest != source_state_digest:
            raise DexExecutorError('El origen o recibo cambió; revisa de nuevo')
        prior = observation_record(db, identifier(kind, source_id))
        if prior:
            if prior[1]['preview_digest'] != preview_digest:
                raise DexExecutorError('Esta llamada ya tiene otra observación externa')
            return public_record(prior)
        if parent.status not in original.PENDING[kind] or original.resolution_record(db, original.identifier(kind, source_id)):
            raise DexExecutorError('Esta llamada ya no admite una observación externa')
        if parent.updated_at > now:
            raise DexExecutorError('La observación precede al origen actual')
        quiescence = original._quiescence(db, parent, now, verify_runs=True) if kind == 'OPERATOR' else None
        signer = _identity(db, 'wallet', sender); tx_ref = _identity(db, 'tx', tx_hash)
        original.require_unused(db, signer, terms['nonce'], tx_ref)
        original._check_other_families(db, signer, terms['nonce'], tx_ref)
        original._check_originals(db, kind, parent.id, sender, terms['nonce'], tx_hash)
        body = {'schema': SCHEMA, 'id': identifier(kind, source_id), 'source_kind': kind, 'source_id': parent.id,
            'account_id': parent.account_id, 'wallet_digest': _identity(db, 'wallet', canonical_wallet_address(wallet_address)),
            'signer_digest': signer, 'nonce': terms['nonce'], 'tx_digest': tx_ref,
            'source_plan_digest': parent.payload_digest, 'source_state_digest': parent.state_digest,
            'source_status': parent.status, 'quiescence': quiescence, 'proof': proof, 'preview_digest': preview_digest,
            'saved_at': now.isoformat(), 'status': STATUS}
        # Compute before adding pending ORM changes: the identity reader is read-only.
        raw, digest = _encoded({**body, 'seal': _seal(db, body)})
        db.add(RealDexExternalObservation(id=body['id'], source_kind=kind,
            swap_id=parent.id if kind == 'WALLET_SWAP' else None, operator_id=parent.id if kind == 'OPERATOR' else None,
            account_id=parent.account_id, wallet_digest=body['wallet_digest'], signer_digest=signer,
            nonce=terms['nonce'], tx_digest=tx_ref, status=STATUS, payload_json=raw, payload_digest=digest, created_at=now))
        db.flush(); result = public_record(observation_record(db, body['id'])); db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def verify_all_observations(db, *, version):
    if version < 76:
        names = {row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if ('real_dex_external_observations' in names and db.connection().exec_driver_sql(
                'SELECT count(*) FROM real_dex_external_observations').scalar_one()):
            raise DexExecutorError('Una bóveda anterior no admite observaciones externas')
        return
    seen = set(); hashes = set()
    for raw in db.scalars(select(RealDexExternalObservation)):
        row, _ = observation_record(db, raw.id); key = (row.signer_digest, row.nonce)
        if key in seen or row.tx_digest in hashes:
            raise DexExecutorError('Nonce o recibo externo duplicado')
        seen.add(key); hashes.add(row.tx_digest)
