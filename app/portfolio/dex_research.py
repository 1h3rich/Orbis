"""Encrypted, immutable research records separate from frozen DEX accounting.

New HTTP authorizations replay originals. Archived reads work with LAB/PAPER
locked and never grant execution. A RAM worker pins the authorization digest;
removing qualification cannot downgrade that worker into the legacy path.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.models import (RealDexBotBinding, RealDexBotRun,
    RealDexResearchBinding, RealDexResearchAuthorization)
from app.portfolio.wallet_funding import _decoded, _encoded
from app.research.dex_binding import CheckedDexResearch, DexSignalResearch, read_dex_signal_research
from app.research.references import Digest, Timestamp


class BindingQualification(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True)
    schema_name: Literal['orbis.dex.research.binding.v1'] = Field(alias='schema')
    binding_id: UUID
    binding_header_digest: Digest
    evidence: DexSignalResearch
    created_at: str


class ActivationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    candle_t: Timestamp
    close: str
    dca_initial_reference: str | None
    dca_skip_through_t: Timestamp | None


class AuthorizationQualification(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True)
    schema_name: Literal['orbis.dex.research.authorization.v1'] = Field(alias='schema')
    run_id: UUID
    run_header_digest: Digest
    binding_id: UUID
    binding_header_digest: Digest
    evidence: DexSignalResearch
    activation: ActivationContext
    created_at: str


def _version(db: Session) -> int:
    return db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()


def _require_current(db: Session) -> None:
    if _version(db) < 72:
        raise DexExecutorError('Actualiza la bóveda para guardar la investigación DEX')


def _binding(db: Session, identifier: str):
    row = db.get(RealDexBotBinding, str(UUID(identifier)))
    if row is None:
        raise DexExecutorError('Vínculo de investigación DEX no encontrado')
    return row, _decoded(row.payload_json, row.payload_digest)


def _proof(evidence: DexSignalResearch, binding: dict) -> None:
    review = binding['origin']['review']
    read_dex_signal_research(evidence.exported(), review['source'])
    if evidence.paper.observation_t != review['candle_t']:
        raise DexExecutorError('La investigación DEX no corresponde a la observación vinculada')


def _activation(terms: dict) -> dict:
    last = terms['seed'][-1]
    dca = terms.get('dca')
    return {'candle_t': last['t'], 'close': last['c'],
            'dca_initial_reference': dca['initial_reference'] if dca else None,
            'dca_skip_through_t': dca['skip_through_t'] if dca else None}


def binding_qualification(db: Session, identifier: str) -> dict | None:
    """Validate a snapshot, not a replay or a recovered trading permission."""
    if _version(db) < 72:
        return None
    identifier = str(UUID(identifier))
    record = db.get(RealDexResearchBinding, identifier)
    if record is None:
        return None
    try:
        row, terms = _binding(db, identifier)
        material = BindingQualification.model_validate(_decoded(record.payload_json, record.payload_digest))
        if (str(material.binding_id) != row.id or material.binding_header_digest != row.payload_digest
                or material.created_at != record.created_at.isoformat() or record.created_at != row.created_at):
            raise ValueError('binding qualification identity')
        _proof(material.evidence, terms)
        return material.model_dump(mode='json', by_alias=True)
    except (ValueError, KeyError, TypeError, IndexError):
        raise DexExecutorError('La investigación guardada del vínculo DEX no es coherente') from None


def authorization_qualification(db: Session, identifier: str, *, expected_digest: str | None = None) -> dict | None:
    if _version(db) < 72:
        if expected_digest is not None:
            raise DexExecutorError('Falta la investigación del permiso DEX')
        return None
    record = db.get(RealDexResearchAuthorization, str(UUID(identifier)))
    if record is None:
        if expected_digest is not None:
            raise DexExecutorError('Falta la investigación del permiso DEX')
        return None
    try:
        if expected_digest is not None and record.payload_digest != expected_digest:
            raise ValueError('qualification changed')
        row = db.get(RealDexBotRun, record.run_id)
        if row is None:
            raise ValueError('missing run')
        terms = _decoded(row.payload_json, row.payload_digest)
        parent, binding = _binding(db, row.binding_id)
        material = AuthorizationQualification.model_validate(_decoded(record.payload_json, record.payload_digest))
        if (str(material.run_id) != row.id or material.run_header_digest != row.payload_digest
                or str(material.binding_id) != row.binding_id or record.binding_id != row.binding_id
                or material.binding_header_digest != parent.payload_digest
                or material.created_at != record.created_at.isoformat() or record.created_at != row.created_at
                or material.activation.model_dump(mode='json') != _activation(terms)
                or material.evidence.paper.observation_t > material.activation.candle_t):
            raise ValueError('authorization qualification identity')
        _proof(material.evidence, binding)
        return material.model_dump(mode='json', by_alias=True)
    except (ValueError, KeyError, TypeError, IndexError):
        raise DexExecutorError('La investigación guardada del permiso DEX no es coherente') from None


def save_binding_qualification(db: Session, identifier: str, checked: CheckedDexResearch) -> None:
    """Participates in the caller's binding transaction; never commits alone."""
    _require_current(db)
    if type(checked) is not CheckedDexResearch:
        raise DexExecutorError('Reproduce la investigación desde sus registros originales')
    checked.assert_current()
    row, binding = _binding(db, identifier)
    _proof(checked.evidence, binding)
    material = BindingQualification.model_validate({'schema': 'orbis.dex.research.binding.v1',
        'binding_id': row.id, 'binding_header_digest': row.payload_digest,
        'evidence': checked.evidence.exported(), 'created_at': row.created_at.isoformat()})
    raw, digest = _encoded(material.model_dump(mode='json', by_alias=True))
    db.add(RealDexResearchBinding(binding_id=row.id, payload_json=raw,
        payload_digest=digest, created_at=row.created_at))
    db.flush()
    binding_qualification(db, row.id)


def save_authorization_qualification(db: Session, identifier: str, checked: CheckedDexResearch) -> str:
    _require_current(db)
    if type(checked) is not CheckedDexResearch:
        raise DexExecutorError('Reproduce la investigación antes de activar el bot')
    checked.assert_current()
    row = db.get(RealDexBotRun, str(UUID(identifier)))
    if row is None:
        raise DexExecutorError('Permiso DEX no encontrado')
    terms = _decoded(row.payload_json, row.payload_digest)
    parent, binding = _binding(db, row.binding_id)
    _proof(checked.evidence, binding)
    material = AuthorizationQualification.model_validate({'schema': 'orbis.dex.research.authorization.v1',
        'run_id': row.id, 'run_header_digest': row.payload_digest, 'binding_id': row.binding_id,
        'binding_header_digest': parent.payload_digest, 'evidence': checked.evidence.exported(),
        'activation': _activation(terms), 'created_at': row.created_at.isoformat()})
    raw, digest = _encoded(material.model_dump(mode='json', by_alias=True))
    db.add(RealDexResearchAuthorization(run_id=row.id, binding_id=row.binding_id,
        payload_json=raw, payload_digest=digest, created_at=row.created_at))
    db.flush()
    authorization_qualification(db, row.id, expected_digest=digest)
    return digest


def authorization_digest(db: Session, identifier: str) -> str:
    _require_current(db)
    row = db.get(RealDexResearchAuthorization, str(UUID(identifier)))
    if row is None:
        raise DexExecutorError('Falta la investigación del permiso DEX')
    authorization_qualification(db, identifier, expected_digest=row.payload_digest)
    return row.payload_digest


def verify_all_research(db: Session, *, version: int) -> None:
    if version < 72:
        return
    for identifier in db.scalars(select(RealDexResearchBinding.binding_id)):
        binding_qualification(db, identifier)
    for identifier in db.scalars(select(RealDexResearchAuthorization.run_id)):
        authorization_qualification(db, identifier)
