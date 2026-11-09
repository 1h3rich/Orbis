"""Detached full-history effects proposal; no storage or acceptance authority."""
from __future__ import annotations

from dataclasses import dataclass, replace
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Context, ROUND_HALF_EVEN, localcontext
import json
from typing import Sequence
from uuid import UUID, uuid5

from app.portfolio.accounting_view import validate_review_baseline
from app.portfolio.domain import CALC_PRECISION, EventInput, PortfolioEvent
from app.portfolio.economic_external_capture import ExternalEffectsInputs
from app.portfolio.economic_external_effects import (
    AssetScale, EffectAllocation, EffectAnnotation, ExternalEffectsReview, ManualEffect, _note, review_external_effects,
)
from app.portfolio.economic_projection import ExternalOrderClaim, ExternalOrderMember, ProjectionError, TieOrder, _uuid
from app.portfolio.repository import _digest
from app.portfolio.wallet_funding import _encoded, _time

SCHEMA = 'orbis.portfolio.external.effects.plan.v2'
BATCH_MEMBER_SCHEMA = 'orbis.portfolio.external.effects.plan.v3'
NAMESPACE = UUID('4d22a26a-5c4f-4c83-a41e-ac863bf9a879')


def event_id(review_id: str, key: str) -> str:
    return str(uuid5(NAMESPACE, 'event:'+_uuid(review_id)+':'+_uuid(key)))


def request_id(review_id: str, key: str, *, paired: bool = False) -> str:
    return str(uuid5(NAMESPACE, ('pair:' if paired else 'single:')+_uuid(review_id)+':'+_uuid(key)))


@dataclass(frozen=True, slots=True)
class NewExternalEvent:
    key: str
    account_id: str
    input: EventInput
    pair_key: str | None = None
    allow_similar: bool = False

    def __post_init__(self):
        _uuid(self.key); _uuid(self.account_id)
        if self.pair_key is not None: _uuid(self.pair_key)
        if not isinstance(self.input, EventInput) or type(self.allow_similar) is not bool:
            raise ProjectionError('El nuevo asiento externo perdió su declaración')
        # RealEvent stores a gross override only when it differs from price ×
        # quantity. Match that canonical original representation before sealing.
        if self.input.kind in ('BUY','SELL') and self.input.gross_override is not None:
            with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
                if self.input.gross_override==self.input.quantity*self.input.unit_price:
                    object.__setattr__(self,'input',replace(self.input,gross_override=None))
        if self.input.note: _note(self.input.note)
        paired = self.input.kind in ('TRANSFER_IN', 'TRANSFER_OUT', 'CASH_TRANSFER_IN', 'CASH_TRANSFER_OUT')
        if paired != (self.pair_key is not None):
            raise ProjectionError('Identifica los dos lados del traslado propio')


@dataclass(frozen=True)
class ExternalEffectsPlan:
    body_json: str
    digest: str
    review: ExternalEffectsReview
    created_events: tuple[PortfolioEvent, ...]


def _duplicate_key(account: str, item: EventInput) -> tuple:
    # A new asset transfer's cost is derived later. A different provisional
    # manual basis must not hide an otherwise identical original movement.
    return (account, *(getattr(item, key) for key in
        ('kind', 'quote_currency', 'asset', 'quantity', 'unit_price', 'amount', 'fee', 'fee_asset')),
        None if item.kind in ('TRANSFER_IN', 'TRANSFER_OUT') else item.gross_amount)


def _prepare_created(inputs: ExternalEffectsInputs, review_id: str, recorded_at: datetime,
        new_events: tuple[NewExternalEvent, ...]) -> tuple[PortfolioEvent, ...]:
    """Validate proposals against actual originals before any financial replay."""
    if not isinstance(inputs, ExternalEffectsInputs):
        raise ProjectionError('Falta la captura autenticada de los efectos externos')
    _uuid(review_id); stamp = _time(recorded_at)
    if stamp < max((inputs.not_before, *(event.recorded_at for event in inputs.events))):
        raise ProjectionError('La declaración precede a sus observaciones guardadas')
    if (type(new_events) is not tuple or len(new_events) > 8192
            or any(not isinstance(item, NewExternalEvent) for item in new_events)
            or len({item.key for item in new_events}) != len(new_events)
            or len(inputs.events)+len(new_events) > inputs.limits.max_events):
        raise ProjectionError('Los nuevos asientos superan sus límites o repiten una identidad')
    for _, current in inputs.currents:
        validate_review_baseline(current)
    certificate = json.loads(inputs.certificate_json)
    at = _time(datetime.fromisoformat(certificate['block_at']))
    bound = {item.account_id for item in inputs.bindings}
    originals = {event.id: event for event in inputs.events}; created = []
    # Bound the search to matching economic values and a narrow time window,
    # instead of scanning up to a million originals for EACH proposed event.
    similar = defaultdict(list)
    for event in inputs.events:
        similar[_duplicate_key(event.account_id, event.input)].append(event.input.occurred_at)
    for values in similar.values(): values.sort()
    for proposal in sorted(new_events, key=lambda item: item.key):
        identifier = event_id(review_id, proposal.key)
        if (proposal.account_id not in bound or identifier in originals
                or proposal.input.occurred_at != at):
            raise ProjectionError('El nuevo asiento necesita una cuenta vinculada y el tiempo real del recibo')
        times = similar.get(_duplicate_key(proposal.account_id, proposal.input), ())
        if not proposal.allow_similar and bisect_left(times, at-timedelta(minutes=2)) < bisect_right(times, at+timedelta(minutes=2)):
            raise ProjectionError('Ya hay un asiento semejante; selecciónalo o confirma expresamente otro movimiento')
        created.append(PortfolioEvent(identifier, proposal.account_id,
            request_id(review_id, proposal.pair_key or proposal.key, paired=proposal.pair_key is not None), stamp, proposal.input))
    return tuple(created)


def _claim_for(inputs, members, created):
    selected = {item.event_id for item in members}
    if not {event.id for event in created} <= selected:
        raise ProjectionError('La propuesta contiene asientos nuevos sin asignar al recibo')
    certificate = json.loads(inputs.certificate_json)
    at = _time(datetime.fromisoformat(certificate['block_at']))
    return ExternalOrderClaim('EXTERNAL_OBSERVATION', inputs.observation_id, inputs.order_id, inputs.order_digest,
        certificate['context_account_id'], inputs.root_account_id, int(certificate['block_number']),
        int(certificate['transaction_index']), at, certificate['block_hash'], certificate['block_transactions_ref'],
        int(certificate['block_transaction_count']), certificate['signer_digest'], int(certificate['nonce']), tuple(members))


def _review_args(inputs, claim, allocations, annotations, manual_effects, asset_scales, tie_orders):
    return dict(observation_id=inputs.observation_id, observation_digest=inputs.observation_digest,
        observation_json=inputs.observation_json, claim=claim, bindings=inputs.bindings, allocations=allocations,
        annotations=annotations, manual_effects=manual_effects, asset_scales=asset_scales,
        unavailable_event_ids=inputs.unavailable_event_ids, tie_orders=tie_orders, limits=inputs.limits)


def _assembled_plan(inputs, review_id, stamp, new_events, normalized, review, *, batch_ref=None):
    body = {'schema': SCHEMA if batch_ref is None else BATCH_MEMBER_SCHEMA,
        'review_id': review_id, 'recorded_at': stamp.isoformat(),
        'state_digest': inputs.state_digest, 'custody': json.loads(inputs.custody_json),
        'review': review.body, 'created_events': [{'id': item.id, 'account_id': item.account_id,
            'request_id': item.request_id, 'recorded_at': item.recorded_at.isoformat(), 'input_digest': _digest(item.input)}
            for item in normalized],
        'similar_event_overrides': sorted(item.key for item in new_events if item.allow_similar),
        'created_event_keys': [{'key': item.key, 'account_id': item.account_id, 'pair_key': item.pair_key}
            for item in sorted(new_events, key=lambda item: item.key)],
        'saved': False, 'accounting_acceptance_active': False, 'resolves_intent': False, 'trading_authority': False}
    if batch_ref is not None: body['batch_ref'] = batch_ref
    raw, digest = _encoded(body)
    return ExternalEffectsPlan(raw, digest, review, tuple(normalized))


def prepare_external_effects(inputs: ExternalEffectsInputs, *, review_id: str, recorded_at: datetime,
        members: Sequence[ExternalOrderMember], allocations: tuple[EffectAllocation, ...],
        annotations: tuple[EffectAnnotation, ...], new_events: tuple[NewExternalEvent, ...] = (),
        manual_effects: tuple[ManualEffect, ...] = (), asset_scales: tuple[AssetScale, ...] = (),
        tie_orders: Sequence[TieOrder] = ()) -> ExternalEffectsPlan:
    created = _prepare_created(inputs, review_id, recorded_at, new_events)
    stamp = _time(recorded_at)
    claim = _claim_for(inputs, members, created)
    args = _review_args(inputs, claim, allocations, annotations, manual_effects, asset_scales, tie_orders)
    review = review_external_effects((*inputs.events, *created), inputs.claims, **args)
    # New paired originals must store the actual basis derived from the complete
    # chronology. Existing originals remain immutable even if their old manual
    # basis differs. Repeat once against the definitive proposed originals.
    effective = {item.original.id: item.accounting_input for item in review.projection.events}
    normalized = tuple(replace(event, input=replace(event.input,
        gross_override=effective[event.id].gross_override))
        if event.input.kind in ('TRANSFER_IN', 'TRANSFER_OUT') else event for event in created)
    if normalized != tuple(created):
        review = review_external_effects((*inputs.events, *normalized), inputs.claims, **args)
    return _assembled_plan(inputs, review_id, stamp, new_events, normalized, review)
