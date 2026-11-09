"""Authenticated manual outcome, distinct from a demonstrated strategy fill.

No balances, RPC, signature, write or implicit reactivation. The first financial
acceptance fixes the observation time available to causal historical readers.
"""
from dataclasses import dataclass
from datetime import datetime
import json

from app.portfolio import dex_external_observation as observations
from app.portfolio.dex_verified_read import snapshot_pin as _read_pin
from app.portfolio.wallet_funding import _time, _encoded

SCHEMA = 'orbis.dex.external.outcome.witness.v1'


@dataclass(frozen=True, slots=True)
class ExternalOutcome:
    source_kind: str
    source_id: str
    observation_id: str
    observation_digest: str
    booking_id: str
    booking_digest: str
    order_id: str
    order_digest: str
    acceptance_id: str
    acceptance_digest: str
    link_digest: str
    accepted_at: datetime
    receipt_json: str
    nonce: str

    @property
    def gas_fee_wei(self) -> int:
        return int(json.loads(self.receipt_json)['gas_fee_wei'])

    def witness(self) -> dict:
        return {'schema': SCHEMA, 'source_kind': self.source_kind, 'source_id': self.source_id,
            'observation_id': self.observation_id, 'observation_digest': self.observation_digest,
            'booking_id': self.booking_id, 'booking_digest': self.booking_digest,
            'order_record_id': self.order_id, 'order_record_digest': self.order_digest,
            'acceptance_id': self.acceptance_id, 'acceptance_digest': self.acceptance_digest,
            'link_digest': self.link_digest, 'accepted_at': self.accepted_at.isoformat()}


def external_outcome(db, kind: str, source_id: str, *, as_of: datetime | None = None) -> ExternalOutcome | None:
    version = db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    if version < 78: return None
    from app.portfolio.dex_verified_read import KEY
    current = db.info.get(KEY)
    key = ('external-outcome', kind, source_id)
    cached = current['values'].get(key) if current is not None else None
    pin = _read_pin(db, version) if current is not None else None
    if cached is not None and pin is not None and cached[0] == pin:
        fact = cached[1]
    else:
        if current is not None: current['values'].pop(key, None)
        fact = _authenticated_outcome(db, kind, source_id)
        after = _read_pin(db, version) if current is not None else None
        if after is not None: current['values'][key] = (after, fact)
    # Authenticate present data before applying the historical cutoff. Caching
    # a historical None would otherwise hide a fact from a later causal read.
    if fact is not None and as_of is not None and fact.accepted_at > _time(as_of): return None
    return fact


def _authenticated_outcome(db, kind: str, source_id: str) -> ExternalOutcome | None:
    from app.portfolio import economic_external_booking as bookings, economic_external_acceptance as accepted
    from app.portfolio import economic_acceptance as archive
    record = observations.observation_record(db, observations.identifier(kind, source_id))
    if record is None: return None
    observed, body = record
    booked = bookings.booking_record(db, bookings.identifier(observed.id))
    if booked is None:
        # A lost booking cannot look pending while an acceptance still claims
        # it. Authenticate the shared registry before interpreting absence.
        archive._registry(db)
        return None
    linked = accepted.accepted_link(db, booked[0].id)
    link, material = linked
    prefix = archive.capture_acceptance(db, link.acceptance_id)
    # Capturing all original rows/lineage authenticates provenance without
    # folding financial snapshots under the vault's write/read lease.
    payload = json.loads(prefix.payload_json)
    certificate = booked[1]['plan']['review']
    return ExternalOutcome(kind, source_id, observed.id, observed.payload_digest, booked[0].id, booked[0].payload_digest,
        certificate['order_record_id'], certificate['order_record_digest'], link.acceptance_id,
        material['acceptance_digest'], link.payload_digest, link.created_at,
        _encoded(body['proof']['receipt'])[0], observed.nonce)


def public_outcome(fact: ExternalOutcome) -> dict:
    return {'outcome': 'EXTERNAL_REVIEWED', 'witness': fact.witness(), 'accepted_at': fact.accepted_at.isoformat(),
        'nonce': fact.nonce, 'nonce_consumed': True, 'gas_fee_wei': str(fact.gas_fee_wei),
        'accounting_review_accepted': True, 'effects_proven_complete': False,
        'strategy_executed': False, 'trading_authority': False, 'automatic_restart': False}
