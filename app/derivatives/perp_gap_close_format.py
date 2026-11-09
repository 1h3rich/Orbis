"""Additive PAPER closure receipt; results are comparison evidence only."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.derivatives.perp_gap_close import GapCloseInput, estimate_gap_close
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_wire import ExactInput
from app.lab.store import content_hash


TABLE = 'paper_observed_gap_closes'
DDL = '''CREATE TABLE paper_observed_gap_closes (
    session_id TEXT PRIMARY KEY REFERENCES paper_observed_sessions(id),
    request_id TEXT NOT NULL UNIQUE,receipt_blob BLOB NOT NULL,
    result_blob BLOB NOT NULL,hash TEXT NOT NULL
)'''
Hash = str


class GapCloseLink(ExactInput):
    session_id: str
    sequence: int = Field(ge=0, le=1_000_000_000)
    head_hash: Hash = Field(pattern=r'^[0-9a-f]{64}$')
    seed_hash: Hash = Field(pattern=r'^[0-9a-f]{64}$')
    origin_hash: Hash = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def canonical(self):
        if str(UUID(self.session_id)) != self.session_id:
            raise ValueError('Sesión de cierre no canónica')
        return self


class GapCloseReceipt(ExactInput):
    schema_name: Literal['orbis.paper.perp.gap-close.receipt.v1'] = Field(alias='schema')
    request_id: str
    created_at: str
    source: GapCloseLink
    input: GapCloseInput

    @model_validator(mode='after')
    def canonical(self):
        if str(UUID(self.request_id)) != self.request_id or datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError('Identificador o fecha de cierre no válidos')
        return self


def original_link(header, replayed) -> GapCloseLink:
    return GapCloseLink(session_id=header['id'], sequence=replayed.sequence,
                        head_hash=replayed.head_hash, seed_hash=replayed.seed_hash,
                        origin_hash=replayed.origin_hash)


def verify_result(state, link: GapCloseLink, receipt: GapCloseReceipt, result: dict, digest: str) -> dict:
    if receipt.source != link or content_hash({'receipt': receipt.payload(), 'result': result}) != digest:
        raise ObservedError('El cierre no pertenece al prefijo original o su huella cambió')
    computed = estimate_gap_close(state, receipt.input)
    if result != computed:
        raise ObservedError('El cierre estimado no reproduce sus resultados')
    return computed


def replay_gap_close(db, row):
    from app.derivatives.perp_observed_archive_format import replay_observed_session, unpack
    if row is None:
        raise ObservedError('Cierre estimado no encontrado')
    raw = unpack(row['receipt_blob'])
    receipt = GapCloseReceipt.model_validate(raw)
    if (receipt.payload() != raw or receipt.request_id != row['request_id']
            or receipt.source.session_id != row['session_id']):
        raise ObservedError('Recibo de cierre alterado o no canónico')
    header = db.execute('SELECT * FROM paper_observed_sessions WHERE id=?', (row['session_id'],)).fetchone()
    original = replay_observed_session(db, header)
    result = unpack(row['result_blob'])
    computed = verify_result(original.state, original_link(header, original), receipt, result, row['hash'])
    return receipt, computed, original
