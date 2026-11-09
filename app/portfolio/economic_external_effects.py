"""Pure manual effects review for an authenticated external observation.

Inputs are NOT acceptance authority. A storage port must authenticate the
observation, supplemental order, wallet bindings and original events first.
This module reconciles exact resource deltas, then uses the shared v3 schedule;
it never infers a trade from logs, persists a balance or resolves an intent.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import json
import re
from typing import Literal, Sequence
from types import MappingProxyType

from app.live_execution.dex_external_observation import SCHEMA as OBSERVATION, TOKEN_REGISTRY
from app.portfolio.domain import CALC_PRECISION, EventInput, PortfolioError, PortfolioEvent, _symbol, utc_datetime
from app.portfolio.economic_projection import (
    PAIR_KINDS, ExternalOrderClaim, OrderClaim, ProjectionError, ProjectionLimits,
    ProjectionResult, TieOrder, _uuid, project_events,
)
from app.portfolio.wallet_funding import _encoded
from app.portfolio.repository import _digest as event_input_digest
from app.security.sensitive_text import contains_sensitive_ai_text

SCHEMA = 'orbis.portfolio.external.effects.review.v1'
MAX_MEMBERS = 8192
MAX_FACTS = 4096
REF = re.compile(r'[0-9a-f]{64}')
SCALES = MappingProxyType({'BNB': 18, 'USDT': 18, 'WBNB': 18})
Classification = Literal['TRADE', 'OWN_TRANSFER', 'COMPOSITE', 'EXTERNAL_FLOW', 'ADJUSTMENT', 'EXPENSE', 'NO_LEDGER_CHANGE']
Disposition = Literal['PERMISSION_REVIEWED', 'NON_FINANCIAL', 'MANUAL_EFFECTS_DECLARED', 'MANUAL_COMPLETE', 'UNKNOWN']
DeltaKey = tuple[str, str]


def _reference(value: str) -> None:
    if not isinstance(value, str) or REF.fullmatch(value) is None:
        raise ProjectionError('Referencia privada externa inválida')


def _note(value: str) -> None:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 1000 or contains_sensitive_ai_text(value, question=True)):
        raise ProjectionError('Describe la declaración sin direcciones, hashes de transacción ni secretos')


def _unsigned(value: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r'0|[1-9][0-9]{0,77}', value) is None or int(value) >= 2**256:
        raise ProjectionError('Cantidad de red externa inválida')
    return int(value)


@dataclass(frozen=True, slots=True)
class PartyBinding:
    address_ref: str
    account_id: str
    role: Literal['OWNER', 'SIGNER', 'PROVEN_WALLET']

    def __post_init__(self):
        _reference(self.address_ref); _uuid(self.account_id)
        if self.role not in ('OWNER', 'SIGNER', 'PROVEN_WALLET'):
            raise ProjectionError('Vínculo externo inválido')


@dataclass(frozen=True, slots=True)
class AssetScale:
    asset: str
    decimals: int

    def __post_init__(self):
        if (_symbol(self.asset, 'asset') != self.asset or self.asset in SCALES
                or type(self.decimals) is not int or not 0 <= self.decimals <= 36):
            raise ProjectionError('Precisión manual inválida o sustituye un token del registro')


@dataclass(frozen=True, slots=True)
class ResourceDelta:
    account_id: str
    asset: str
    units: int

    def __post_init__(self):
        _uuid(self.account_id)
        if (_symbol(self.asset, 'asset') != self.asset or type(self.units) is not int
                or not 0 < abs(self.units) < 2**256):
            raise ProjectionError('Movimiento manual externo inválido')


@dataclass(frozen=True, slots=True)
class ManualEffect:
    id: str
    annotation_ref: str
    reason: str
    deltas: tuple[ResourceDelta, ...]
    own_transfer: bool = False

    def __post_init__(self):
        _uuid(self.id); _note(self.reason)
        if (not isinstance(self.annotation_ref, str) or type(self.deltas) is not tuple
                or not 1 <= len(self.deltas) <= MAX_FACTS
                or any(not isinstance(item, ResourceDelta) for item in self.deltas)
                or len({(item.account_id, item.asset) for item in self.deltas}) != len(self.deltas)
                or type(self.own_transfer) is not bool):
            raise ProjectionError('Declaración manual de efectos inválida')
        if self.own_transfer:
            totals = defaultdict(int)
            for item in self.deltas: totals[item.asset] += item.units
            if len({item.account_id for item in self.deltas}) < 2 or any(totals.values()):
                raise ProjectionError('El traslado manual propio debe conservar sus unidades entre cuentas')


@dataclass(frozen=True, slots=True)
class EffectAllocation:
    effect_refs: tuple[str, ...]
    event_ids: tuple[str, ...]
    classification: Classification
    description: str

    def __post_init__(self):
        _note(self.description)
        if (self.classification not in ('TRADE', 'OWN_TRANSFER', 'COMPOSITE', 'EXTERNAL_FLOW', 'ADJUSTMENT', 'EXPENSE', 'NO_LEDGER_CHANGE')
                or type(self.effect_refs) is not tuple or not 1 <= len(self.effect_refs) <= MAX_FACTS
                or any(not isinstance(value, str) for value in self.effect_refs)
                or len(set(self.effect_refs)) != len(self.effect_refs)
                or type(self.event_ids) is not tuple or len(self.event_ids) > MAX_MEMBERS
                or len(set(self.event_ids)) != len(self.event_ids)):
            raise ProjectionError('Asignación externa inválida')
        for value in self.event_ids: _uuid(value)


@dataclass(frozen=True, slots=True)
class EffectAnnotation:
    reference: str
    disposition: Disposition
    note: str

    def __post_init__(self):
        _note(self.note)
        if not isinstance(self.reference, str) or self.disposition not in (
                'PERMISSION_REVIEWED', 'NON_FINANCIAL', 'MANUAL_EFFECTS_DECLARED', 'MANUAL_COMPLETE', 'UNKNOWN'):
            raise ProjectionError('Anotación externa inválida')


@dataclass(frozen=True)
class ExternalEffectsReview:
    body_json: str
    digest: str
    projection: ProjectionResult

    @property
    def body(self) -> dict:
        return json.loads(self.body_json)


def _clean(values: dict[DeltaKey, int]) -> dict[DeltaKey, int]:
    return {key: value for key, value in values.items() if value}


def _units(value: Decimal, asset: str, scales: dict[str, int]) -> int:
    if asset not in scales:
        raise ProjectionError('Declara la precisión de cada activo manual utilizado')
    scaled = value.scaleb(scales[asset])
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise ProjectionError('El asiento contiene una fracción de la unidad mínima del activo')
    return int(scaled)


def _event_deltas(event: PortfolioEvent, scales: dict[str, int]) -> dict[DeltaKey, int]:
    item = event.input; account = event.account_id; result = defaultdict(int)
    def add(asset, value): result[account, asset] += _units(value, asset, scales)
    if item.kind == 'TRANSFER':
        pass
    elif item.kind in ('CONTRIBUTION', 'CASH_TRANSFER_IN'):
        add(item.quote_currency, item.amount)
    elif item.kind in ('WITHDRAWAL', 'CASH_TRANSFER_OUT'):
        add(item.quote_currency, -item.amount-item.fee)
    elif item.kind == 'FEE':
        add(item.asset or item.quote_currency, -(item.quantity if item.asset is not None else item.amount))
    elif item.kind in ('BUY', 'SELL'):
        base_fee = item.fee if item.fee_asset == item.asset else Decimal(0)
        quote_fee = item.fee if base_fee == 0 else Decimal(0)
        add(item.asset, item.quantity-base_fee if item.kind == 'BUY' else -item.quantity-base_fee)
        add(item.quote_currency, -item.gross_amount-quote_fee if item.kind == 'BUY' else item.gross_amount-quote_fee)
    elif item.kind in ('ADJUSTMENT_IN', 'TRANSFER_IN'):
        add(item.asset, item.quantity)
    elif item.kind in ('ADJUSTMENT_OUT', 'TRANSFER_OUT'):
        add(item.asset, -item.quantity)
    else:
        raise ProjectionError('Clase de asiento externo no compatible')
    return _clean(result)


def _deltas(values: dict[DeltaKey, int]) -> list[dict]:
    return [{'account_id': account, 'asset': asset, 'units': str(units)}
        for (account, asset), units in sorted(_clean(values).items())]


def _kind_check(allocation: EffectAllocation, selected: list[PortfolioEvent], *, owned_flow: bool) -> None:
    kinds = {event.input.kind for event in selected}; mode = allocation.classification
    if owned_flow and mode not in ('OWN_TRANSFER', 'COMPOSITE'):
        raise ProjectionError('Un traslado propio necesita ambos lados; no es aportación ni beneficio')
    allowed = {'TRADE': {'BUY', 'SELL'}, 'OWN_TRANSFER': {*PAIR_KINDS, 'TRANSFER'},
        'COMPOSITE': {'BUY', 'SELL', 'FEE', *PAIR_KINDS, 'TRANSFER'},
        'EXTERNAL_FLOW': {'CONTRIBUTION', 'WITHDRAWAL', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT'},
        'ADJUSTMENT': {'ADJUSTMENT_IN', 'ADJUSTMENT_OUT'}, 'EXPENSE': {'FEE'}, 'NO_LEDGER_CHANGE': set()}
    if (not kinds <= allowed[mode] or mode == 'TRADE' and len(selected) != 1
            or mode not in ('NO_LEDGER_CHANGE', 'OWN_TRANSFER') and not selected
            or mode == 'NO_LEDGER_CHANGE' and selected):
        raise ProjectionError('La clasificación manual no corresponde a sus asientos')


def review_external_effects(events: Sequence[PortfolioEvent], claims: Sequence[OrderClaim], *,
        observation_id: str, observation_digest: str, observation_json: str, claim: ExternalOrderClaim,
        bindings: tuple[PartyBinding, ...], allocations: tuple[EffectAllocation, ...],
        annotations: tuple[EffectAnnotation, ...], manual_effects: tuple[ManualEffect, ...] = (),
        asset_scales: tuple[AssetScale, ...] = (), unavailable_event_ids: frozenset[str] = frozenset(),
        tie_orders: Sequence[TieOrder] = (), limits: ProjectionLimits = ProjectionLimits()) -> ExternalEffectsReview:
    """Match declarations to every observed flow and exact native gas, then replay.

    Unknown annotations remain explicitly incomplete. Prices/cost assignment
    come from manual originals, never from Transfer/Approval or a token symbol.
    The result is a detached proposal only, including when manual review is ready.
    """
    try:
        with localcontext(Context(prec=CALC_PRECISION, rounding=ROUND_HALF_EVEN)):
            return _review(events, claims, observation_id=observation_id, observation_digest=observation_digest,
                observation_json=observation_json, claim=claim, bindings=bindings, allocations=allocations,
                annotations=annotations, manual_effects=manual_effects, asset_scales=asset_scales,
                unavailable_event_ids=unavailable_event_ids, tie_orders=tie_orders, limits=limits)
    except (KeyError, TypeError, AttributeError, OverflowError, json.JSONDecodeError):
        raise ProjectionError('La propuesta externa perdió sus hechos, vínculos o asientos') from None
    except PortfolioError as exc:
        if isinstance(exc, ProjectionError):
            raise
        raise ProjectionError(str(exc)) from None


def _reconcile(events, claims, *, observation_id, observation_digest, observation_json, claim, bindings,
            allocations, annotations, manual_effects, asset_scales, unavailable_event_ids, tie_orders, limits):
    _uuid(observation_id); _reference(observation_digest)
    typed = ((bindings, PartyBinding, 1024), (allocations, EffectAllocation, MAX_FACTS),
        (annotations, EffectAnnotation, MAX_FACTS), (manual_effects, ManualEffect, MAX_FACTS), (asset_scales, AssetScale, 512))
    if (not isinstance(claim, ExternalOrderClaim) or claim.source_id != observation_id
            or not isinstance(observation_json, str) or len(observation_json.encode()) > 750000
            or any(type(values) is not tuple or len(values) > maximum
                or any(not isinstance(value, cls) for value in values) for values, cls, maximum in typed)
            or type(unavailable_event_ids) is not frozenset):
        raise ProjectionError('Propuesta externa fuera de sus límites')
    proof = json.loads(observation_json)
    if (_encoded(proof)[0] != observation_json or proof['schema'] != OBSERVATION
            or proof['token_registry'] != TOKEN_REGISTRY or proof['outcome'] != 'EXTERNAL_SUCCEEDED'
            or proof['chain_id'] != '0x38'
            or any(proof[key] is not False for key in ('effects_complete', 'accounting_complete', 'resolves_intent', 'trading_authority'))
            or claim.nonce != _unsigned(proof['nonce']) or claim.block_number != _unsigned(proof['receipt']['block_number'])
            or claim.transaction_index != _unsigned(proof['transaction_index'])
            or claim.block_hash != proof['receipt']['block_hash']
            or claim.block_at != utc_datetime(datetime.fromisoformat(proof['receipt']['block_at']))):
        raise ProjectionError('El perfil externo no conserva su observación y orden')
    _uuid(proof['source_id']); _reference(proof['observation_ref'])
    parties = {item.address_ref: item for item in bindings}
    if len(parties) != len(bindings) or len({item.role for item in bindings if item.role != 'PROVEN_WALLET'}) != sum(item.role != 'PROVEN_WALLET' for item in bindings):
        raise ProjectionError('La revisión repite un vínculo de wallet')
    owner = parties.get(proof['owner_address_ref']); signer = parties.get(proof['signer_address_ref'])
    if (owner is None or owner.role != 'OWNER' or signer is None or claim.account_id != owner.account_id
            or signer.role != ('OWNER' if proof['signer_is_owner'] else 'SIGNER')
            or any(item.role == 'OWNER' and item is not owner or item.role == 'SIGNER' and item is not signer for item in bindings)):
        raise ProjectionError('Falta el vínculo del propietario o del pagador real del gas')
    scales = dict(SCALES)
    for scale in asset_scales:
        if scale.asset in scales: raise ProjectionError('La revisión repite la precisión de un activo')
        scales[scale.asset] = scale.decimals
    original = {event.id: event for event in events}
    if len(original) != len(events) or any(not isinstance(event, PortfolioEvent) or event.accounting is not None for event in events):
        raise ProjectionError('La revisión necesita asientos originales sin contexto derivado')
    bound_accounts = {item.account_id for item in bindings}
    flows = {}; own = set()
    def flow(reference, asset, units, left, right, *, left_role=None, right_role=None):
        if reference in flows: raise ProjectionError('La observación repite un movimiento')
        delta = defaultdict(int)
        source = None if left_role == 'ZERO' else parties.get(left)
        target = None if right_role == 'ZERO' else parties.get(right)
        if left_role == 'ZERO' and left in parties or right_role == 'ZERO' and right in parties:
            raise ProjectionError('Una dirección nula no puede vincularse como wallet propia')
        if source is not None: delta[source.account_id, asset] -= units
        if target is not None: delta[target.account_id, asset] += units
        flows[reference] = _clean(delta)
        if source is not None and target is not None and source.account_id != target.account_id: own.add(reference)
    for item in proof['transfers']:
        _reference(item['log_ref'])
        if item['asset'] not in ('USDT', 'WBNB'): raise ProjectionError('Token fuera del registro observado')
        flow('token:'+item['log_ref'], item['asset'], _unsigned(item['units']), item['left_ref'], item['right_ref'],
            left_role=item['left_role'], right_role=item['right_role'])
    value = _unsigned(proof['native_value_wei'])
    if value:
        flow('native:'+proof['call_ref'], 'BNB', value, proof['signer_address_ref'], proof['target_ref'] or proof['created_ref'],
            right_role=proof['native_target_role'])
    required_annotations = {'unobserved-effects': {'MANUAL_COMPLETE', 'UNKNOWN'}}
    for item in proof['approvals']:
        required_annotations['approval:'+item['log_ref']] = {'PERMISSION_REVIEWED', 'UNKNOWN'}
    for item in proof['uninterpreted_logs']:
        required_annotations['uninterpreted:'+item['log_ref']] = {'NON_FINANCIAL', 'MANUAL_EFFECTS_DECLARED', 'UNKNOWN'}
    annotated = {item.reference: item for item in annotations}
    if len(annotated) != len(annotations) or set(annotated) != set(required_annotations):
        raise ProjectionError('Revisa cada permiso, log sin interpretar y la cobertura no observada')
    if any(item.disposition not in required_annotations[ref] for ref, item in annotated.items()):
        raise ProjectionError('La anotación no corresponde al tipo de hecho observado')
    manually_by_annotation = defaultdict(list)
    for item in manual_effects:
        ref = 'manual:'+item.id
        if ref in flows or item.annotation_ref not in annotated or item.annotation_ref.startswith('approval:'):
            raise ProjectionError('La declaración manual perdió su anotación de origen')
        manually_by_annotation[item.annotation_ref].append(item)
        delta = defaultdict(int)
        for value in item.deltas:
            if value.account_id not in bound_accounts or value.asset not in scales:
                raise ProjectionError('El efecto manual necesita una cuenta vinculada y precisión explícita')
            delta[value.account_id, value.asset] += value.units
        flows[ref] = _clean(delta)
        if item.own_transfer: own.add(ref)
    for ref, item in annotated.items():
        if (item.disposition == 'MANUAL_EFFECTS_DECLARED' and not manually_by_annotation[ref]
                or item.disposition == 'NON_FINANCIAL' and manually_by_annotation[ref]):
            raise ProjectionError('El log manual y sus movimientos declarados no coinciden')
    selected = set(); used = set(); expected_all = defaultdict(int)
    unavailable = set(unavailable_event_ids) | {event for prior in claims for event in prior.event_ids}
    for allocation in allocations:
        expected = defaultdict(int); actual = defaultdict(int); members = []
        for ref in allocation.effect_refs:
            if ref not in flows or ref in used: raise ProjectionError('El lote omite o reutiliza un efecto externo')
            used.add(ref)
            for key, units in flows[ref].items(): expected[key] += units
        for event_id in allocation.event_ids:
            event = original.get(event_id)
            if event is None or event_id in selected or event_id in unavailable or event.account_id not in bound_accounts:
                raise ProjectionError('El efecto perdió o reutiliza un asiento de una cuenta vinculada')
            members.append(event); selected.add(event_id)
            for key, units in _event_deltas(event, scales).items(): actual[key] += units
        _kind_check(allocation, members, owned_flow=bool(own.intersection(allocation.effect_refs)))
        if _clean(expected) != _clean(actual):
            raise ProjectionError('Los asientos declarados no conservan las unidades observadas de cada cuenta y activo')
        for key, units in expected.items(): expected_all[key] += units
    if used != set(flows): raise ProjectionError('Declara todos los movimientos observados y manuales, sin omitir ninguno')
    gas = _unsigned(proof['receipt']['gas_fee_wei']); paid = 0
    for event_id in claim.gas_event_ids:
        event = original.get(event_id)
        if (event is None or event_id in selected or event_id in unavailable or event.account_id != signer.account_id
                or event.input.kind != 'FEE' or not (event.input.asset == 'BNB' or event.input.asset is None and event.input.quote_currency == 'BNB')):
            raise ProjectionError('El gas necesita sus gastos originales y la cuenta de su pagador')
        selected.add(event_id)
        delta = _event_deltas(event, scales)
        paid -= delta.get((signer.account_id, 'BNB'), 0)
    if paid != gas or (gas == 0) != (not claim.gas_event_ids):
        raise ProjectionError('El gas declarado no coincide exactamente con el recibo')
    if (selected != set(claim.event_ids) or any(original[item.event_id].account_id != item.account_id
            or (item.role == 'GAS') != (item.event_id in claim.gas_event_ids) for item in claim.members)):
        raise ProjectionError('El orden externo no conserva todos los asientos revisados')
    # Never attribute one half of an owned transfer to another receipt/allocation.
    by_request = defaultdict(list)
    for event_id in selected:
        event = original[event_id]
        if event.input.kind in PAIR_KINDS: by_request[event.request_id].append(event)
    for pair in by_request.values():
        if len(pair) != 2: raise ProjectionError('La revisión necesita ambos lados del traslado propio')
        if not any(set(item.event_ids) >= {event.id for event in pair} for item in allocations):
            raise ProjectionError('Los dos lados del traslado deben declararse juntos')
    return proof, original, scales, expected_all, gas, signer


def validate_external_effect_units(events, claims=(), **kwargs) -> None:
    """Archive unit/role validation without financial replay or storage access."""
    try:
        with localcontext(Context(prec=CALC_PRECISION, rounding=ROUND_HALF_EVEN)):
            _reconcile(events, claims, tie_orders=(), limits=ProjectionLimits(),
                unavailable_event_ids=frozenset(), **kwargs)
    except (KeyError, TypeError, AttributeError, OverflowError, json.JSONDecodeError):
        raise ProjectionError('La declaración externa perdió sus hechos o miembros originales') from None


def review_arguments(body: dict, observation_json: str) -> dict:
    """Reconstruct typed manual declarations from the sealed internal profile."""
    from app.portfolio.economic_projection import ExternalOrderMember
    raw = dict(body['claim']); raw['block_at'] = datetime.fromisoformat(raw['block_at'])
    raw['members'] = tuple(ExternalOrderMember(**item) for item in raw['members'])
    return dict(observation_id=body['observation_id'], observation_digest=body['observation_digest'],
        observation_json=observation_json, claim=ExternalOrderClaim(**raw),
        bindings=tuple(PartyBinding(**item) for item in body['bindings']),
        allocations=tuple(EffectAllocation(tuple(item['effect_refs']), tuple(item['event_ids']),
            item['classification'], item['description']) for item in body['allocations']),
        annotations=tuple(EffectAnnotation(**item) for item in body['annotations']),
        manual_effects=tuple(ManualEffect(item['id'], item['annotation_ref'], item['reason'],
            tuple(ResourceDelta(delta['account_id'], delta['asset'], int(delta['units'])) for delta in item['deltas']),
            item['own_transfer']) for item in body['manual_effects']),
        asset_scales=tuple(AssetScale(item['asset'], item['decimals']) for item in body['asset_scales'] if item['basis']=='USER_DECLARED'))


def _review(events, claims, *, observation_id, observation_digest, observation_json, claim, bindings,
            allocations, annotations, manual_effects, asset_scales, unavailable_event_ids, tie_orders, limits):
    proof, original, scales, expected_all, gas, signer = _reconcile(events, claims,
        observation_id=observation_id, observation_digest=observation_digest, observation_json=observation_json,
        claim=claim, bindings=bindings, allocations=allocations, annotations=annotations, manual_effects=manual_effects,
        asset_scales=asset_scales, unavailable_event_ids=unavailable_event_ids, tie_orders=tie_orders, limits=limits)
    result = project_events(events, [*claims, claim], tie_orders=tie_orders, limits=limits)
    complete = all(item.disposition != 'UNKNOWN' for item in annotations)
    def event_body(event):
        return {'id': event.id, 'account_id': event.account_id, 'request_id': event.request_id,
            'recorded_at': event.recorded_at.isoformat(), 'input_digest': event_input_digest(event.input)}
    def claim_body(value):
        return {**asdict(value), 'block_at': value.block_at.isoformat()}
    allocation_bodies = [{**asdict(item), 'effect_refs': sorted(item.effect_refs), 'event_ids': sorted(item.event_ids)}
        for item in sorted(allocations, key=lambda item: tuple(sorted(item.effect_refs)))]
    manual_bodies = [{**asdict(item), 'deltas': [asdict(value) | {'units': str(value.units)}
        for value in sorted(item.deltas, key=lambda value: (value.account_id, value.asset))]}
        for item in sorted(manual_effects, key=lambda item: item.id)]
    body = {'schema': SCHEMA, 'projection_engine': result.engine, 'observation_id': observation_id,
        'observation_digest': observation_digest, 'observation_ref': proof['observation_ref'],
        'order_record_id': claim.order_record_id, 'order_record_digest': claim.order_record_digest,
        'claim': claim_body(claim), 'limits': asdict(limits),
        'ledger_input_digest': _encoded([event_body(event) for event in sorted(events, key=lambda event: event.id)])[1],
        'prior_claims_digest': _encoded([claim_body(value) for value in sorted(claims, key=lambda value: value.order_record_id)])[1],
        'tie_orders': [{'account_id': item.account_id, 'at': item.at.isoformat(), 'nodes': list(item.nodes)}
            for item in sorted(tie_orders, key=lambda item: (item.at, item.account_id))],
        'projection_digest': _encoded([{'event_id': item.original.id, 'account_id': item.original.account_id,
            'input_digest': event_input_digest(item.accounting_input), 'ordinal': item.ordinal,
            'node_id': item.node_id, 'order_record_id': item.order_record_id,
            'transfer_basis_derived': item.transfer_basis_derived} for item in result.events])[1],
        'basis': 'USER_DECLARED_EFFECTS_WITH_OBSERVED_UNIT_CHECKS',
        'bindings': [asdict(item) for item in sorted(bindings, key=lambda item: item.address_ref)],
        'asset_scales': [{'asset': asset, 'decimals': decimals, 'basis': 'REGISTRY' if asset in SCALES else 'USER_DECLARED'}
            for asset, decimals in sorted(scales.items())],
        'allocations': allocation_bodies,
        'manual_effects': manual_bodies, 'annotations': [asdict(item) for item in sorted(annotations, key=lambda item: item.reference)],
        'observed_and_declared_deltas': _deltas(expected_all), 'gas_fee_wei': str(gas), 'gas_account_id': signer.account_id,
        'selected_events': [event_body(original[item.event_id]) | {'role': item.role} for item in claim.members],
        'manual_review_complete': complete, 'effects_proven_complete': False, 'strategy_executed': False,
        'saved': False, 'resolves_intent': False, 'accounting_acceptance_active': False, 'trading_authority': False}
    raw, digest = _encoded(body)
    return ExternalEffectsReview(raw, digest, result)
