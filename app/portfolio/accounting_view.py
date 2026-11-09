"""Current accounting from accepted prefixes and causally compatible later events.

Capture authenticates original rows, accepted methods and new receipt witnesses.
Replay is pure. A missing proof or a retroactive change never selects the old
fold as a fallback. No result is stored or turned into trading authority here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.economic_acceptance import (
    ArchivedAcceptance, _registry, _snapshot, capture_acceptance, replay_acceptance,
)
from app.portfolio.economic_projection import (
    ENGINE, GAS_ENGINE, EXTERNAL_ENGINE, OrderClaim, ProjectionError, ProjectionLimits, ProjectionResult, TieOrder, project_events,
)
from app.portfolio.economic_projection_review import ALL_RECEIPTS, EXTERNAL_RECEIPTS, original_account_closure, _required_sources
from app.portfolio.economic_projection_source import _body, _booking, _seal, claim_from_witness
from app.portfolio.domain import PortfolioError, PortfolioEvent, utc_datetime
from app.portfolio.models import RealEvent
from app.portfolio.repository import _rows_book_revision
from app.portfolio.wallet_funding import _encoded
from app.portfolio.economic_verified_read import scope


VIEW_ENGINE = 'accepted-prefix-extension-1.0'
GAS_VIEW_ENGINE = 'accepted-prefix-extension-2.0'
EXTERNAL_VIEW_ENGINE = 'accepted-prefix-extension-3.0'


@dataclass(frozen=True)
class AccountingInputs:
    root_account_id: str
    events: tuple[PortfolioEvent, ...]
    accepted: tuple[ArchivedAcceptance, ...]
    extension_claims: tuple[OrderClaim, ...]
    revision: str
    limits: ProjectionLimits


@dataclass(frozen=True)
class AccountingReadIssue:
    """Authenticated prefixes with an unreadable live extension; never a balance."""
    accepted: tuple[ArchivedAcceptance, ...]
    error: str
    revision: str


def capture_review_baseline(db: Session, account_id: str, *,
                            limits: ProjectionLimits = ProjectionLimits()) -> AccountingInputs | AccountingReadIssue | None:
    """Separate trusted head capture from a potentially invalid live extension.

    Original prefix corruption is fatal. Only the later extension may become
    an explicit read issue, so a complete new review can repair its ordering.
    """
    _snapshot(db)
    with scope(db):
        accounts, events = original_account_closure(db, account_id, limits=limits)
        heads = _registry(db)
        accepted = tuple(capture_acceptance(db, record_id) for record_id in
            sorted({heads[account]['id'] for account in accounts if account in heads}))
        if not accepted:
            return None
        current_ids = {event.id for event in events}
        seen = set()
        for archive in accepted:
            if (seen.intersection(archive.inputs.accounts) or not set(archive.inputs.accounts) <= set(accounts)
                    or not {event.id for event in archive.inputs.events} <= current_ids):
                raise ProjectionError('Los precedentes contables no conservan su cierre original')
            seen.update(archive.inputs.accounts)
        try:
            return capture_accounting_inputs(db, account_id, limits=limits)
        except PortfolioError as exc:
            references = {json.loads(item.payload_json)['id']: _encoded(json.loads(item.payload_json))[1] for item in accepted}
            raw = {account: _rows_book_revision(account, db.scalars(select(RealEvent).where(RealEvent.account_id == account)).all())
                for account in accounts}
            revision = _encoded({'schema': 'orbis.portfolio.unreadable.accounting.revision.v1',
                'account_id': account_id, 'raw_revisions': raw, 'acceptances': references})[1]
            return AccountingReadIssue(accepted, str(exc), revision)


def validate_review_baseline(current: AccountingInputs | AccountingReadIssue | None) -> None:
    """Pure prefix verification; a broken later tail may be reviewed explicitly."""
    if current is not None:
        for archive in current.accepted:
            replay_acceptance(archive)


def _extension_witness(db: Session, kind: str, source_id: str, owners: dict[str, set[str]], cutoff, *, gas_only: bool = False):
    if kind=='EXTERNAL_OBSERVATION':
        from app.portfolio.economic_external_booking import archived_witness
        # External bookings always acquire a criterion in their own atomic
        # command; they cannot become an implicit extension of an old method.
        raise ProjectionError('La revisión externa necesita su aceptación contable explícita')
    if gas_only:
        from app.portfolio.dex_gas_order import identifier, gas_order_record as order_record
        from app.portfolio.economic_gas_source import _body as body_for, _seal as seal_for
    else:
        from app.portfolio.dex_receipt_order import identifier, order_record
        body_for, seal_for = _body, _seal
    record = order_record(db, identifier(kind, source_id))
    if record is None or cutoff is not None and record[0].created_at > cutoff:
        raise ProjectionError('Comprueba el orden del nuevo recibo DEX antes de usar su resultado')
    body = body_for(db, record, _booking(db, record))
    # Only an already authenticated and accepted owner→account relationship
    # can extend a legacy wallet booking without retaining its full address.
    # Conflicting historical relationships require a new explicit review.
    if owners.get(body['owner_digest']) != {body['account_id']}:
        raise ProjectionError('Revisa el vínculo contable de esta wallet antes de incorporar su operación')
    return {**body, 'seal': seal_for(db, body)}


def require_receipt_extension(db: Session, account_id: str, kind: str, source_id: str, *, gas_only: bool = False) -> None:
    """Preflight a new booking before it can make the active book unreadable."""
    from app.portfolio.repository import _accounting_inputs
    if gas_only:
        from app.portfolio.dex_gas_order import identifier, gas_order_record as order_record
    else:
        from app.portfolio.dex_receipt_order import identifier, order_record
    captured = _accounting_inputs(db, account_id)
    if captured is None:
        return
    if gas_only and not any(item.inputs.source_policy in (ALL_RECEIPTS,EXTERNAL_RECEIPTS) and account_id in item.inputs.accounts for item in captured.accepted):
        return
    record = order_record(db, identifier(kind, source_id))
    if record is None:
        raise ProjectionError('Comprueba el orden del nuevo recibo DEX antes de registrarlo')
    row, payload = record
    owners = defaultdict(set)
    boundary = None
    prior = []
    for accepted in captured.accepted:
        times = {member: claim.block_at for claim in accepted.inputs.claims
            for member in claim.event_ids}
        prior.extend(claim for claim in accepted.inputs.claims if claim.account_id == account_id)
        for witness in map(json.loads, accepted.inputs.witness_json):
            owners[witness['owner_digest']].add(witness['account_id'])
        for event in accepted.inputs.events:
            if event.account_id == account_id:
                at = times.get(event.id, event.occurred_at)
                boundary = max(boundary, at) if boundary is not None else at
    if owners.get(row.wallet_digest) != {account_id}:
        raise ProjectionError('Revisa el vínculo contable de esta wallet antes de registrar su operación')
    at = utc_datetime(datetime.fromisoformat(payload['proof']['block_at']))
    coordinate = int(row.block_number), int(row.transaction_index)
    if (boundary is not None and at < boundary
            or any(coordinate <= (claim.block_number, claim.transaction_index) for claim in prior)):
        raise ProjectionError('El recibo es anterior al prefijo aceptado; necesita una nueva revisión')


def capture_accounting_inputs(db: Session, account_id: str, *, recorded_through: datetime | None = None,
                              limits: ProjectionLimits = ProjectionLimits()) -> AccountingInputs | None:
    """One detached read snapshot. None means no accepted method in the closure."""
    if not isinstance(limits, ProjectionLimits):
        raise ProjectionError('Límites de cálculo inválidos')
    _snapshot(db)
    with scope(db):
        cutoff = utc_datetime(recorded_through) if recorded_through is not None else None
        heads = _registry(db, accepted_through=cutoff)
        if not heads:
            return None
        accounts, events = original_account_closure(db, account_id, limits=limits, recorded_through=cutoff)
        selected = {heads[account]['id'] for account in accounts if account in heads}
        if not selected:
            return None
        accepted = tuple(capture_acceptance(db, record_id) for record_id in sorted(selected))
        current_ids = {event.id for event in events}
        prefix_ids = set()
        sources = set()
        owners = defaultdict(set)
        related_accounts = set()
        references = {}
        for archive in accepted:
            payload = json.loads(archive.payload_json)
            references[payload['id']] = _encoded(payload)[1]
            if related_accounts.intersection(archive.inputs.accounts):
                raise ProjectionError('Dos revisiones actuales se solapan sin un linaje común')
            related_accounts.update(archive.inputs.accounts)
            ids = {event.id for event in archive.inputs.events}
            if not ids <= current_ids:
                raise ProjectionError('El cálculo actual perdió parte de un prefijo aceptado')
            prefix_ids.update(ids)
            for witness in map(json.loads, archive.inputs.witness_json):
                owners[witness['owner_digest']].add(witness['account_id'])
            sources.update((claim.source_kind, claim.source_id) for claim in archive.inputs.claims)
        gas_accounts = frozenset(account for archive in accepted if archive.inputs.source_policy in (ALL_RECEIPTS,EXTERNAL_RECEIPTS) for account in archive.inputs.accounts)
        external_accounts = frozenset(account for archive in accepted if archive.inputs.source_policy==EXTERNAL_RECEIPTS for account in archive.inputs.accounts)
        trade_sources = _required_sources(db, accounts, recorded_through=cutoff)
        required = _required_sources(db, accounts, recorded_through=cutoff, gas_accounts=gas_accounts,
            external_accounts=external_accounts) if gas_accounts else trade_sources
        if not sources <= required:
            raise ProjectionError('Un recibo aceptado perdió su contabilización original')
        extension_claims = []
        extension_json = []
        for kind, source_id in sorted(required - sources):
            witness = _extension_witness(db, kind, source_id, owners, cutoff, gas_only=(kind, source_id) not in trade_sources)
            claim = claim_from_witness(db, witness)
            members = set(claim.event_ids)
            if not members <= current_ids or members.intersection(prefix_ids):
                raise ProjectionError('Un recibo nuevo altera el prefijo aceptado; revisa de nuevo')
            extension_claims.append(claim)
            extension_json.append(witness)
        revisions = {}
        for account in accounts:
            query = select(RealEvent).where(RealEvent.account_id == account)
            if cutoff is not None:
                query = query.where(RealEvent.recorded_at <= cutoff)
            revisions[account] = _rows_book_revision(account, db.scalars(query).all())
        revision = _encoded({'schema': 'orbis.portfolio.accounting.revision.v1',
            'engine': EXTERNAL_VIEW_ENGINE if external_accounts else GAS_VIEW_ENGINE if gas_accounts else VIEW_ENGINE,
            'projection_engine': EXTERNAL_ENGINE if external_accounts else GAS_ENGINE if gas_accounts else ENGINE, 'account_id': account_id, 'raw_revisions': revisions,
            'acceptances': references, 'extensions': extension_json})[1]
        return AccountingInputs(account_id, events, accepted, tuple(extension_claims), revision, limits)


def replay_accounting_view(inputs: AccountingInputs) -> ProjectionResult:
    """Preserve every accepted per-account prefix and recalculate complete tails."""
    if not isinstance(inputs, AccountingInputs):
        raise ProjectionError('Faltan los datos autenticados del cálculo actual')
    old = {}
    claims = list(inputs.extension_claims)
    orders = defaultdict(list)
    seen_nodes = defaultdict(set)
    boundaries = {}
    for archive in inputs.accepted:
        projection = replay_acceptance(archive).projection
        claims.extend(archive.inputs.claims)
        for event in projection.events:
            original = event.original
            if original.id in old:
                raise ProjectionError('Las revisiones actuales repiten un asiento aceptado')
            old[original.id] = event
            key = original.account_id, event.accounting_at
            if event.node_id not in seen_nodes[key]:
                orders[key].append(event.node_id)
                seen_nodes[key].add(event.node_id)
            boundaries[original.account_id] = max(boundaries.get(original.account_id, event.accounting_at), event.accounting_at)
    extension_times = {member: claim.block_at for claim in inputs.extension_claims
        for member in claim.event_ids}
    for event in inputs.events:
        if event.id in old:
            continue
        at = extension_times.get(event.id, event.occurred_at)
        if event.account_id in boundaries and at < boundaries[event.account_id]:
            raise ProjectionError('Un asiento anterior cambia el prefijo aceptado; revisa la conciliación')
    prefixes = tuple(TieOrder(account, at, tuple(nodes)) for (account, at), nodes in sorted(orders.items()))
    projected = project_events(inputs.events, tuple(claims), prefix_orders=prefixes, limits=inputs.limits)
    for event in projected.events:
        accepted = old.get(event.original.id)
        if accepted is not None and accepted.accounting_input != event.accounting_input:
            raise ProjectionError('La operación posterior cambió un coste ya aceptado; revisa la conciliación')
    return projected
