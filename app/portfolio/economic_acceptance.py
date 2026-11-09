"""Versioned acceptance archive; original events and trading authority stay separate.

Application commands capture under one read snapshot, calculate outside the
vault lease, then revalidate inputs/heads under BEGIN IMMEDIATE. Archive reads
return detached inputs for replay; saved result numbers are never balances.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from contextlib import AbstractContextManager,nullcontext
from functools import lru_cache
import hashlib
import hmac
import json
import re
from typing import TYPE_CHECKING,Callable,Sequence
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError
from app.portfolio.economic_projection import OrderClaim, ProjectionError, ProjectionLimits, TieOrder
from app.portfolio.economic_projection_review import (
    ALL_RECEIPTS, EXTERNAL_RECEIPTS, policy_rank, ProjectionInputs, ProjectionReview, _chunks, capture_projection_inputs, review_policy, review_projection,
)
from app.portfolio.economic_projection_source import (
    _existing_salt, _booking, capture_witness, claim_from_witness, witness_record,
)
from app.portfolio.models import RealAccount, RealEconomicAcceptance as Record, RealEconomicAcceptanceAccount as Member, RealEvent
from app.portfolio.repository import _digest, _from_row, _rows_book_revision
from app.portfolio.wallet_funding import _decoded, _encoded, _time


SCHEMA = 'orbis.portfolio.economic.acceptance.v1'
DRAFT = 'orbis.portfolio.economic.acceptance.draft.v1'
BATCH_DRAFT = 'orbis.portfolio.economic.acceptance.draft.v2'
GAS_DRAFT = 'orbis.portfolio.economic.acceptance.draft.v3'
GAS_BATCH_DRAFT = 'orbis.portfolio.economic.acceptance.draft.v4'
EXTERNAL_DRAFT = 'orbis.portfolio.economic.acceptance.draft.v5'
EXTERNAL_BATCH_DRAFT = 'orbis.portfolio.economic.acceptance.draft.v6'
EXTERNAL_EFFECTS_DRAFT = 'orbis.portfolio.economic.acceptance.draft.v7'
EXTERNAL_GROUP_DRAFT = 'orbis.portfolio.economic.acceptance.draft.v8'
NAMESPACE = UUID('dcc520c5-f7a1-4cf8-8f1a-a5c203808406')
TABLES = (Record.__tablename__, Member.__tablename__)
CommitGuard=Callable[[],AbstractContextManager[None]]
if TYPE_CHECKING:
    from app.portfolio.accounting_view import AccountingInputs, AccountingReadIssue


def identifier(digest: str) -> str:
    if not isinstance(digest, str) or re.fullmatch('[a-f0-9]{64}', digest) is None:
        raise ProjectionError('Referencia de aceptación inválida')
    return str(uuid5(NAMESPACE, digest))


def _schema(db: Session) -> None:
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() < 74:
        raise ProjectionError('Actualiza la bóveda para guardar conciliaciones')


def _snapshot(db: Session) -> None:
    if db.new or db.dirty or db.deleted:
        raise ProjectionError('Guarda o descarta los cambios pendientes antes de revisar')
    connection = db.connection()
    if connection.dialect.name != 'sqlite':
        raise ProjectionError('La conciliación local necesita SQLite')
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    db.expire_all()
    _schema(db)


def _seal(db: Session, body: dict) -> str:
    return hmac.new(_existing_salt(db), (SCHEMA + ':').encode() + _encoded(body)[0].encode(), hashlib.sha256).hexdigest()


def _header(db: Session, record_id: str):
    from app.portfolio.economic_verified_read import record
    return record(db, ('acceptance', record_id), lambda: _read_header(db, record_id))


def _read_header(db: Session, record_id: str):
    row = db.get(Record, str(UUID(record_id)))
    if row is None:
        raise ProjectionError('No se encuentra la aceptación contable')
    try:
        payload = _decoded(row.payload_json, row.payload_digest)
        if (set(payload) != {'schema', 'id', 'draft_digest', 'draft', 'recorded_at', 'seal'}
                or payload['schema'] != SCHEMA or payload['id'] != row.id
                or payload['draft_digest'] != row.draft_digest or row.id != identifier(row.draft_digest)
                or not hmac.compare_digest(payload['seal'], _seal(db, {k: v for k, v in payload.items() if k != 'seal'}))
                or payload['recorded_at'] != row.created_at.isoformat()):
            raise ValueError('identity or seal')
        draft = payload['draft']
        draft_keys={'schema','review','prefix','parents','limits'}
        if draft.get('schema')in(BATCH_DRAFT,GAS_BATCH_DRAFT,EXTERNAL_BATCH_DRAFT):draft_keys.add('receipt_batch')
        if draft.get('schema')==EXTERNAL_EFFECTS_DRAFT:draft_keys.add('external_effects')
        if draft.get('schema')==EXTERNAL_GROUP_DRAFT:draft_keys.add('external_group')
        if (set(draft) != draft_keys or draft['schema'] not in (DRAFT,BATCH_DRAFT,GAS_DRAFT,GAS_BATCH_DRAFT,EXTERNAL_DRAFT,EXTERNAL_BATCH_DRAFT,EXTERNAL_EFFECTS_DRAFT,EXTERNAL_GROUP_DRAFT)
                or _encoded(draft)[1] != row.draft_digest
                or draft['review']['root_account_id'] != row.root_account_id):
            raise ValueError('draft')
        policy = review_policy(draft['review'])
        expected_rank=3 if draft['schema'] in(EXTERNAL_DRAFT,EXTERNAL_BATCH_DRAFT,EXTERNAL_EFFECTS_DRAFT,EXTERNAL_GROUP_DRAFT)else 2 if draft['schema']in(GAS_DRAFT,GAS_BATCH_DRAFT)else 1
        if draft['schema']==EXTERNAL_GROUP_DRAFT and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<79:
            raise ValueError('joint acceptance schema')
        if (policy_rank(policy)!=expected_rank
                or db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() < (78 if expected_rank==3 else 75 if expected_rank==2 else 74)):
            raise ValueError('source policy')
        limits = ProjectionLimits(**draft['limits'])
        accounts = set(draft['prefix'])
        if (not 0 < len(accounts) <= limits.max_accounts or row.root_account_id not in accounts
                or accounts != set(draft['parents']) or accounts != set(draft['review']['account_revisions'])):
            raise ValueError('accounts')
        ids = []
        for account, values in draft['prefix'].items():
            if str(UUID(account)) != account or type(values) is not list or values != sorted(values):
                raise ValueError('prefix')
            for value in values:
                if str(UUID(value)) != value:
                    raise ValueError('event identity')
            ids.extend(values)
        if (not (0 <= len(ids) <= limits.max_events if policy==EXTERNAL_RECEIPTS else 0 < len(ids) <= limits.max_events)
                or not ids and not any(item.get('source_kind')=='EXTERNAL_OBSERVATION'for item in draft['review']['witnesses'])
                or len(set(ids)) != len(ids) or draft['review']['events_count'] != len(ids)):
            raise ValueError('prefix bounds')
        expected = {}
        for account, parent in draft['parents'].items():
            if parent is not None and (set(parent) != {'id', 'digest'}
                    or str(UUID(parent['id'])) != parent['id']
                    or re.fullmatch('[a-f0-9]{64}', parent['digest']) is None):
                raise ValueError('parent')
            expected[account] = parent['id'] if parent is not None else None
        actual = {member.account_id: member.previous_acceptance_id for member in db.scalars(
            select(Member).where(Member.acceptance_id == row.id))}
        if actual != expected:
            raise ValueError('members')
        if draft['schema']in(BATCH_DRAFT,GAS_BATCH_DRAFT,EXTERNAL_BATCH_DRAFT):
            from app.portfolio.economic_receipt_acceptance import validate_batch_manifest
            validate_batch_manifest(db,draft['receipt_batch'],draft['review'],limits=limits)
        if draft['schema']==EXTERNAL_EFFECTS_DRAFT:
            from app.portfolio.economic_external_acceptance import validate_external_manifest
            validate_external_manifest(db,draft['external_effects'],draft['review'])
        if draft['schema']==EXTERNAL_GROUP_DRAFT:
            from app.portfolio.economic_external_group import validate_manifest
            validate_manifest(db,draft['external_group'],draft['review'])
        return row, payload
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise ProjectionError('La aceptación contable cambió o perdió su prefijo original') from None


def _lineage(db: Session, record_id: str):
    """Iterative, sealed ancestor links; no recursion or inferred latest state."""
    root = _header(db, record_id)
    queue = [root]
    seen = {root[0].id}
    headers = {root[0].id: root}
    checked_edges = set()
    while queue:
        child, body = queue.pop()
        draft = body['draft']
        for account, link in draft['parents'].items():
            if link is None:
                continue
            if link['id'] not in headers:
                headers[link['id']] = _header(db, link['id'])
            parent, original = headers[link['id']]
            previous = original['draft']
            if policy_rank(review_policy(previous['review'])) > policy_rank(review_policy(draft['review'])):
                raise ProjectionError('El linaje debe conservar el criterio de fuentes con gas')
            if (parent.payload_digest != link['digest'] or parent.created_at >= child.created_at
                    or account not in previous['prefix']):
                raise ProjectionError('El linaje contable perdió una cuenta o una revisión anterior')
            edge = (child.id, parent.id)
            if edge not in checked_edges:
                if (not set(previous['prefix']) <= set(draft['prefix'])
                        or any(not set(ids) <= set(draft['prefix'][key]) for key, ids in previous['prefix'].items())):
                    raise ProjectionError('El linaje contable perdió asientos de una revisión anterior')
                checked_edges.add(edge)
            if parent.id not in seen:
                seen.add(parent.id)
                queue.append((parent, original))
    return root


def _verified_registry(db: Session, *, accepted_through: datetime | None = None) -> tuple[dict, dict]:
    """Authenticate membership before using an index to infer absence/latest.

    A deleted member must not make an accepted account silently fall back to
    the legacy fold. No registry result is cached beyond this SQL snapshot.
    """
    heads, selected, records = {}, {}, {}
    member_count = 0
    for record_id in db.scalars(select(Record.id).order_by(Record.created_at, Record.id)).all():
        row, payload = _header(db, record_id)
        draft = payload['draft']
        checked = set()
        for account, parent in draft['parents'].items():
            if parent != heads.get(account):
                raise ProjectionError('El registro contable contiene una rama o un vínculo perdido')
            if parent is not None and parent['id'] not in checked:
                previous, previous_payload = records[parent['id']]
                prefix = previous_payload['draft']['prefix']
                if policy_rank(review_policy(previous_payload['draft']['review'])) > policy_rank(review_policy(draft['review'])):
                    raise ProjectionError('El registro debe conservar el criterio de fuentes con gas')
                if (previous.created_at >= row.created_at or not set(prefix) <= set(draft['prefix'])
                        or any(not set(ids) <= set(draft['prefix'][key]) for key, ids in prefix.items())):
                    raise ProjectionError('El registro contable perdió eventos o cuentas de una aceptación anterior')
                checked.add(parent['id'])
        records[row.id] = (row, payload)
        member_count += len(draft['parents'])
        for account in draft['parents']:
            heads[account] = {'id': row.id, 'digest': row.payload_digest}
            if accepted_through is None or row.created_at <= accepted_through:
                selected[account] = heads[account]
    from sqlalchemy import func
    if db.scalar(select(func.count()).select_from(Member)) != member_count:
        raise ProjectionError('El registro contable contiene miembros sin aceptación')
    return selected, records


def _registry(db: Session, *, accepted_through: datetime | None = None) -> dict:
    return _verified_registry(db, accepted_through=accepted_through)[0]


def _heads(db: Session, accounts: tuple[str, ...]) -> dict:
    verified = _registry(db)
    return {account: verified.get(account) for account in accounts}


def _ties(review: dict) -> tuple[TieOrder, ...]:
    return tuple(TieOrder(item['account_id'], datetime.fromisoformat(item['at']), tuple(item['nodes']))
        for item in review['tie_orders'])


@dataclass(frozen=True)
class ArchivedAcceptance:
    payload_json: str
    inputs: ProjectionInputs


def capture_applicable_acceptance(db: Session, account_id: str, *,
                                 accepted_through: datetime | None = None) -> ArchivedAcceptance | None:
    """Choose the accepted method known at a given time, without financial replay."""
    _snapshot(db)
    from app.portfolio.economic_verified_read import scope
    with scope(db):
        account = str(UUID(account_id))
        if db.get(RealAccount, account) is None:
            raise ProjectionError('No se encuentra la cuenta REAL')
        cutoff = _time(accepted_through) if accepted_through is not None else None
        head = _registry(db, accepted_through=cutoff).get(account)
        return capture_acceptance(db, head['id']) if head is not None else None


def capture_acceptance(db: Session, record_id: str) -> ArchivedAcceptance:
    """Authenticate the exact archived event set, excluding all later appends."""
    _snapshot(db)
    from app.portfolio.economic_verified_read import scope
    with scope(db):
        row, payload = _lineage(db, record_id)
        return _capture_prefix(db, row, payload)


def _capture_prefix(db: Session, row: Record, payload: dict, *,
                    verified_claim: Callable[[str], OrderClaim] | None = None) -> ArchivedAcceptance:
    """Verify this acceptance's financial inputs after authenticating its ancestry."""
    draft = payload['draft']
    limits = ProjectionLimits(**draft['limits'])
    events = []
    for account, ids in sorted(draft['prefix'].items()):
        rows = []
        for batch in _chunks(ids):
            rows.extend(db.scalars(select(RealEvent).where(RealEvent.id.in_(batch))).all())
        if (len(rows) != len(ids) or any(event.account_id != account for event in rows)
                or _rows_book_revision(account, rows) != draft['review']['account_revisions'][account]):
            raise ProjectionError('El prefijo aceptado perdió o modificó asientos originales')
        for original in rows:
            event = _from_row(original)
            if original.payload_digest != _digest(event.input) or event.recorded_at > row.created_at:
                raise ProjectionError('El prefijo aceptado contiene un asiento alterado o posterior')
            events.append(event)
    witnesses = draft['review']['witnesses']
    witness_json = tuple(_encoded(witness)[0] for witness in witnesses)
    claims = (tuple(claim_from_witness(db, witness) for witness in witnesses)
              if verified_claim is None else tuple(verified_claim(raw) for raw in witness_json))
    inputs = ProjectionInputs(row.root_account_id, tuple(sorted(draft['prefix'])),
        tuple(sorted(events, key=lambda event: event.id)), claims,
        tuple(sorted(draft['review']['account_revisions'].items())),
        witness_json, limits, review_policy(draft['review']))
    return ArchivedAcceptance(row.payload_json, inputs)


def replay_acceptance(archived: ArchivedAcceptance) -> ProjectionReview:
    """Replay after releasing SQL. Saved snapshots are comparison data only."""
    payload = json.loads(archived.payload_json)
    reviewed = review_projection(archived.inputs, tie_orders=_ties(payload['draft']['review']))
    if reviewed.body != payload['draft']['review']:
        raise ProjectionError('El resultado aceptado no se reproduce desde sus originales')
    if payload['draft']['schema']==EXTERNAL_EFFECTS_DRAFT:
        from app.portfolio.economic_external_acceptance import replay_external_manifest
        replay_external_manifest(archived.inputs,payload['draft']['external_effects'])
    if payload['draft']['schema']==EXTERNAL_GROUP_DRAFT:
        from app.portfolio.economic_external_group import replay_manifest
        replay_manifest(archived.inputs,payload['draft']['external_group'])
    return reviewed


def _live_witnesses(db, witnesses, wallet_addresses, limits):
    from app.portfolio.dex_deployments import _identity
    from app.portfolio.wallet_proof import canonical_wallet_address
    _snapshot(db)
    if (not isinstance(limits, ProjectionLimits)
            or not isinstance(witnesses, Sequence) or not 0 < len(witnesses) <= limits.max_claims
            or not isinstance(wallet_addresses, Sequence) or not 0 < len(wallet_addresses) <= limits.max_accounts):
        raise ProjectionError('Faltan las pruebas o wallets de la revisión')
    _existing_salt(db)
    addresses = {}
    for value in wallet_addresses:
        wallet = canonical_wallet_address(value)
        owner = _identity(db, 'wallet', wallet)
        if owner in addresses:
            raise ProjectionError('La revisión repite una wallet')
        addresses[owner] = wallet
    not_before = None
    trusted=None;live_checked=False
    for witness in witnesses:
        if not isinstance(witness, dict):
            raise ProjectionError('Prueba contable inválida')
        wallet = addresses.get(witness.get('owner_digest'))
        if wallet is not None:
            if capture_witness(db,witness,wallet_address=wallet)!=witness:
                raise ProjectionError('Cambió la wallet, el vínculo o los asientos de la revisión')
            live_checked=True
        else:
            # A previously accepted target binding is historical evidence.
            # Rechecking all originals does not require retaining its address.
            if trusted is None:
                heads=_heads(db,tuple(sorted({item['account_id']for item in witnesses})))
                trusted=set()
                for record_id in {head['id']for head in heads.values()if head is not None}:
                    trusted.update(capture_acceptance(db,record_id).inputs.witness_json)
            if _encoded(witness)[0]not in trusted:
                raise ProjectionError('Verifica la wallet de cada recibo que todavía no tenga un vínculo aceptado')
            claim_from_witness(db,witness)
        original = witness_record(db, witness)
        _, booking = _booking(db, original)
        required = max(original[0].created_at, _time(datetime.fromisoformat(booking['booked_at'])))
        not_before = max(not_before, required) if not_before is not None else required
    if not live_checked:raise ProjectionError('Verifica una wallet actual de los recibos revisados')
    return not_before


def _live_capture(db, root, witnesses, wallet_addresses, limits):
    not_before = _live_witnesses(db, witnesses, wallet_addresses, limits)
    inputs = capture_projection_inputs(db, root, witnesses, limits=limits)
    not_before = max((not_before, *(event.recorded_at for event in inputs.events)))
    parents = _heads(db, inputs.accounts)
    return inputs, parents, not_before


def _draft(inputs, parents, tie_orders):
    reviewed = review_projection(inputs, tie_orders=tie_orders)
    prefix = {account: [] for account in inputs.accounts}
    for event in inputs.events:
        prefix[event.account_id].append(event.id)
    for ids in prefix.values():
        ids.sort()
    draft = {'schema': EXTERNAL_DRAFT if inputs.source_policy==EXTERNAL_RECEIPTS else GAS_DRAFT if inputs.source_policy == ALL_RECEIPTS else DRAFT,
        'review': reviewed.body, 'prefix': prefix,
        'parents': parents, 'limits': asdict(inputs.limits)}
    return draft, _encoded(draft)[1]


@dataclass(frozen=True)
class PreparedAcceptanceReview:
    inputs: ProjectionInputs
    current: AccountingInputs | AccountingReadIssue | None
    draft: dict
    digest: str


def prepare_acceptance_review(vault, root_account_id: str, witnesses: Sequence[dict], *,
                             wallet_addresses: Sequence[str], tie_orders: Sequence[TieOrder] = (),
                             limits: ProjectionLimits = ProjectionLimits()) -> PreparedAcceptanceReview:
    """Capture the actual book and proposal from the same physical snapshot."""
    from app.portfolio.accounting_view import capture_review_baseline, validate_review_baseline
    generation = vault.session_generation
    with vault.guard_session_generation(generation), vault.session() as db:
        inputs, parents, _ = _live_capture(db, root_account_id, witnesses, wallet_addresses, limits)
        current = capture_review_baseline(db, root_account_id, limits=limits)
    validate_review_baseline(current)
    draft, digest = _draft(inputs, parents, tie_orders)
    with vault.guard_session_generation(generation):
        return PreparedAcceptanceReview(inputs, current, draft, digest)


def preview_acceptance(vault, root_account_id: str, witnesses: Sequence[dict], *,
                       wallet_addresses: Sequence[str], tie_orders: Sequence[TieOrder] = (),
                       limits: ProjectionLimits = ProjectionLimits()) -> dict:
    prepared = prepare_acceptance_review(vault, root_account_id, witnesses,
        wallet_addresses=wallet_addresses, tie_orders=tie_orders, limits=limits)
    return {'id': identifier(prepared.digest), 'preview_digest': prepared.digest, 'draft': prepared.draft,
        'saved': False, 'applied_to_ledger': False, 'trading_authority': False}


def _result(row: Record, *, created: bool) -> dict:
    return {'id': row.id, 'root_account_id': row.root_account_id, 'draft_digest': row.draft_digest,
        'recorded_at': row.created_at.isoformat(), 'saved': True, 'created': created,
        'applied_to_ledger': False, 'accounting_method_active': True, 'trading_authority': False}


def _store_reviewed_draft(db: Session,root_account_id: str,draft: dict,digest: str,*,now: datetime,
                          not_before: datetime,wallet_addresses: Sequence[str]=()) -> dict:
    """Composable archive writer; caller owns revalidation and commit/rollback."""
    now=_time(now)
    if (not db.connection().connection.driver_connection.in_transaction or _encoded(draft)[1]!=digest
            or draft['review']['root_account_id']!=root_account_id
            or _heads(db,tuple(sorted(draft['parents'])))!=draft['parents']):
        raise ProjectionError('La aceptación no conserva su revisión y precedentes actuales')
    if now<not_before or any(now<=_header(db,parent['id'])[0].created_at for parent in draft['parents'].values()if parent is not None):
        raise ProjectionError('El reloj de aceptación no conserva el orden del historial')
    from app.portfolio.economic_operational_guard import require_quiescent
    require_quiescent(db,tuple(sorted(draft['prefix'])),draft['review']['witnesses'],now=now,wallet_addresses=wallet_addresses)
    record_id=identifier(digest)
    body={'schema':SCHEMA,'id':record_id,'draft_digest':digest,'draft':draft,'recorded_at':now.isoformat()}
    raw,payload_digest=_encoded({**body,'seal':_seal(db,body)})
    db.add(Record(id=record_id,root_account_id=root_account_id,draft_digest=digest,
        payload_json=raw,payload_digest=payload_digest,created_at=now));db.flush()
    for account,parent in draft['parents'].items():
        db.add(Member(acceptance_id=record_id,account_id=account,previous_acceptance_id=parent['id']if parent is not None else None))
    db.flush();row,_=_lineage(db,record_id)
    return _result(row,created=True)


def confirm_acceptance(vault, root_account_id: str, witnesses: Sequence[dict], *, wallet_addresses: Sequence[str],
                       preview_digest: str, now: datetime, tie_orders: Sequence[TieOrder] = (),
                       limits: ProjectionLimits = ProjectionLimits(),commit_guard: CommitGuard|None=None) -> dict:
    from app.portfolio.accounting_view import capture_review_baseline, validate_review_baseline
    generation = vault.session_generation
    record_id = identifier(preview_digest)
    now = _time(now)
    with vault.guard_session_generation(generation), vault.session() as db:
        _snapshot(db)
        existing = db.get(Record, record_id)
        if existing is not None:
            # A lost response refers to this exact historical prefix. New DEX
            # bookings or newly linked accounts must not force it to become a
            # different review. Current ownership is still checked separately.
            _live_witnesses(db, witnesses, wallet_addresses, limits)
            archived = capture_acceptance(db, record_id)
        else:
            archived = None
            inputs, parents, not_before = _live_capture(db, root_account_id, witnesses, wallet_addresses, limits)
            baseline = capture_review_baseline(db, root_account_id, limits=limits)
    if archived is not None:
        reviewed = replay_acceptance(archived)
        if (reviewed.body['root_account_id'] != str(UUID(root_account_id))
                or reviewed.body['witnesses'] != sorted(witnesses, key=lambda item: item['order_record_id'])
                or _ties(reviewed.body) != tuple(sorted(tie_orders, key=lambda item: (item.at, item.account_id)))):
            raise ProjectionError('La respuesta guardada pertenece a otra revisión')
        with vault.guard_session_generation(generation), vault.session() as db:
            _live_witnesses(db, witnesses, wallet_addresses, limits)
            row, _ = _lineage(db, record_id)
            return _result(row, created=False)
    validate_review_baseline(baseline)
    draft, digest = _draft(inputs, parents, tie_orders)
    if digest != preview_digest:
        raise ProjectionError('El libro o la revisión anterior cambió; revisa de nuevo')
    with (commit_guard or nullcontext)(),vault.guard_session_generation(generation), vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            _snapshot(db)
            existing = db.get(Record, record_id)
            if existing is not None:
                _live_witnesses(db, witnesses, wallet_addresses, limits)
                row, payload = _lineage(db, record_id)
                if payload['draft'] != draft:
                    raise ProjectionError('Ya existe una aceptación distinta para esta revisión')
                result = _result(row, created=False)
                db.commit()
                return result
            fresh, current_parents, current_not_before = _live_capture(db, root_account_id, witnesses, wallet_addresses, limits)
            if fresh != inputs or current_parents != parents:
                raise ProjectionError('El libro o la wallet cambió durante la revisión')
            if now < max(not_before, current_not_before) or any(
                    now <= _header(db, parent['id'])[0].created_at for parent in parents.values() if parent is not None):
                raise ProjectionError('El reloj de aceptación no conserva el orden del historial')
            result = _store_reviewed_draft(db,inputs.root_account_id,draft,digest,now=now,
                not_before=max(not_before,current_not_before),wallet_addresses=wallet_addresses)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def verify_all_acceptances(db: Session, *, version: int) -> None:
    if version < 74:
        for table in TABLES:
            exists = db.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).first()
            if exists is not None and db.connection().exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one():
                raise ProjectionError('Una bóveda anterior no admite aceptaciones contables ocultas')
        return
    from app.portfolio.dex_verified_read import snapshot_pin
    _snapshot(db)
    pin = snapshot_pin(db, version)
    if pin is None:
        raise ProjectionError('Falta una instantánea estable del archivo de conciliaciones')

    @lru_cache(maxsize=2048)
    def verified_claim(raw: str) -> OrderClaim:
        return claim_from_witness(db, json.loads(raw))

    # The global scan already authenticates every header, membership and
    # chronological parent edge. Replay each exact financial prefix, reusing
    # identical witnesses only within this bounded, read-only verification.
    _, records = _verified_registry(db)
    for row, payload in records.values():
        replay_acceptance(_capture_prefix(db, row, payload, verified_claim=verified_claim))
    current_version = db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    if snapshot_pin(db, current_version) != pin:
        raise ProjectionError('La instantánea de conciliaciones cambió durante la verificación')
