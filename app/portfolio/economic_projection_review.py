"""Complete, read-only account review before accepting an economic projection.

Uses original rows, authenticated booked-order witnesses and the transitive
closure of paired transfers. It neither changes the active accounting method
nor treats an account review as a reconciliation with the on-chain balance.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Context, ROUND_HALF_EVEN, localcontext
from datetime import datetime
from json import loads
from typing import Literal, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.dex_verified_read import scope
from app.portfolio.domain import CALC_PRECISION, AccountSnapshot, PortfolioError, PortfolioEvent, fold_events, realized_pnl_by_currency, utc_datetime
from app.portfolio.economic_projection import (
    PAIR_KINDS, ExternalOrderClaim, GasOrderClaim, OrderClaim, ProjectionError, ProjectionLimits, ProjectionResult, TieOrder, project_events,
)
from app.portfolio.economic_projection_source import claim_from_witness, is_gas_witness, is_external_witness
from app.portfolio.models import RealAccount, RealEvent, RealDexReceiptBooking, RealWalletSwapBooking
from app.portfolio.repository import _digest, _from_row, original_book_revision as account_book_revision
from app.portfolio.wallet_funding import _encoded


SCHEMA = 'orbis.portfolio.economic.review.v1'
GAS_SCHEMA = 'orbis.portfolio.economic.review.v2'
EXTERNAL_SCHEMA = 'orbis.portfolio.economic.review.v3'
SourcePolicy = Literal['dex-trades-1', 'dex-trades-and-gas-2','dex-trades-gas-and-external-3']
TRADES: SourcePolicy = 'dex-trades-1'
ALL_RECEIPTS: SourcePolicy = 'dex-trades-and-gas-2'
EXTERNAL_RECEIPTS: SourcePolicy = 'dex-trades-gas-and-external-3'
CHUNK = 400


def review_policy(body: dict) -> SourcePolicy:
    if body.get('schema') == SCHEMA and 'source_policy' not in body:
        return TRADES
    if body.get('schema') == GAS_SCHEMA and body.get('source_policy') == ALL_RECEIPTS:
        return ALL_RECEIPTS
    if body.get('schema') == EXTERNAL_SCHEMA and body.get('source_policy') == EXTERNAL_RECEIPTS:
        return EXTERNAL_RECEIPTS
    raise ProjectionError('La revisión perdió la versión de sus fuentes contables')


def policy_rank(value):
    if value not in (TRADES,ALL_RECEIPTS,EXTERNAL_RECEIPTS): raise ProjectionError('Perfil contable inválido')
    return (TRADES,ALL_RECEIPTS,EXTERNAL_RECEIPTS).index(value)+1


def _chunks(values):
    values = sorted(values)
    for offset in range(0, len(values), CHUNK):
        yield values[offset:offset + CHUNK]


def original_account_closure(db: Session, root_account_id: str, *, limits: ProjectionLimits,
                             recorded_through: datetime | None = None):
    """Discover both sides, then let the pure engine validate every pair."""
    root = str(UUID(root_account_id))
    pending = {root}
    accounts = set()
    requests = set()
    events = []
    cutoff = utc_datetime(recorded_through) if recorded_through is not None else None
    while pending:
        frontier = pending - accounts
        pending = set()
        new_requests = set()
        if len(accounts | frontier) > limits.max_accounts:
            raise ProjectionError('Demasiadas cuentas enlazadas para una revisión')
        for batch in _chunks(frontier):
            found = set(db.scalars(select(RealAccount.id).where(RealAccount.id.in_(batch))))
            if found != set(batch):
                raise ProjectionError('Falta una cuenta del historial enlazado')
            query = select(RealEvent).where(RealEvent.account_id.in_(batch))
            if cutoff is not None:
                query = query.where(RealEvent.recorded_at <= cutoff)
            rows = db.scalars(query
                .order_by(RealEvent.id).limit(limits.max_events - len(events) + 1)).all()
            if len(events) + len(rows) > limits.max_events:
                raise ProjectionError('Demasiados asientos para una revisión')
            for row in rows:
                event = _from_row(row)
                if row.payload_digest != _digest(event.input):
                    raise ProjectionError('El historial enlazado contiene un asiento alterado')
                events.append(event)
                if event.input.kind in PAIR_KINDS:
                    new_requests.add(event.request_id)
        accounts.update(frontier)
        if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78:
            from app.portfolio.economic_external_booking import associated_bookings
            for _,body in associated_bookings(db,frontier,recorded_through=cutoff):
                pending.update({item['account_id'] for item in body['plan']['custody']}-accounts)
        new_requests -= requests
        requests.update(new_requests)
        for batch in _chunks(new_requests):
            query = select(RealEvent.account_id).where(RealEvent.request_id.in_(batch), RealEvent.kind.in_(PAIR_KINDS))
            if cutoff is not None:
                query = query.where(RealEvent.recorded_at <= cutoff)
            peers = db.scalars(query
                .distinct().limit(limits.max_accounts + 1)).all()
            if len(peers) > limits.max_accounts:
                raise ProjectionError('Demasiadas cuentas enlazadas para una revisión')
            pending.update(set(peers) - accounts)
    events.sort(key=lambda event: event.id)
    return tuple(sorted(accounts)), tuple(events)


def _required_sources(db: Session, accounts: tuple[str, ...], *,
                      recorded_through: datetime | None = None,
                      gas_accounts: frozenset[str] = frozenset(),
                      external_accounts: frozenset[str] = frozenset()) -> set[tuple[str, str]]:
    """Every economic DEX booking in the closure must have its order proof."""
    from app.portfolio import wallet_swap_booking, dex_receipt_booking
    required = set()
    cutoff = utc_datetime(recorded_through) if recorded_through is not None else None
    def visible(payload):
        return cutoff is None or (utc_datetime(datetime.fromisoformat(payload['booked_at'])) <= cutoff
            and all(db.get(RealEvent, item['id']).recorded_at <= cutoff for item in payload['events']))
    for batch in _chunks(accounts):
        for row in db.scalars(select(RealWalletSwapBooking).where(RealWalletSwapBooking.account_id.in_(batch))):
            _, payload = wallet_swap_booking.booking_record(db, row.action_id)
            if not visible(payload):
                continue
            if row.account_id in gas_accounts or any(item['role'] == 'TRADE' for item in payload['events']):
                required.add(('WALLET_SWAP', row.action_id))
        for row in db.scalars(select(RealDexReceiptBooking).where(RealDexReceiptBooking.account_id.in_(batch))):
            _, payload = dex_receipt_booking.booking_record(db, row.id)
            if not visible(payload):
                continue
            if any(item['role'] == 'TRADE' for item in payload['events']):
                if row.source_kind not in ('OPERATOR', 'CALL_RESOLUTION'):
                    raise ProjectionError('Hay un origen económico DEX sin conciliación compatible')
                required.add((row.source_kind, row.source_id))
            elif row.account_id in gas_accounts:
                required.add((row.source_kind, row.source_id))
    if external_accounts and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78:
        from app.portfolio.economic_external_booking import associated_bookings
        required.update(('EXTERNAL_OBSERVATION',row.observation_id) for row,_ in associated_bookings(
            db,external_accounts,recorded_through=cutoff))
    return required


def _snapshot(snapshot: AccountSnapshot) -> dict:
    decimals = lambda values: {key: str(value) for key, value in sorted(values.items())}
    return {
        'account_id': snapshot.account_id,
        'cash': decimals(snapshot.cash), 'contributions': decimals(snapshot.contributions),
        'withdrawals': decimals(snapshot.withdrawals), 'cash_fees': decimals(snapshot.cash_fees),
        'realized_pnl': decimals(realized_pnl_by_currency(snapshot)),
        'positions': [{
            'asset': position.asset, 'quote_currency': position.quote_currency,
            'quantity': str(position.quantity), 'cost_basis': str(position.cost_basis),
            'average_cost': str(position.average_cost), 'realized_pnl': str(position.realized_pnl),
            'fees': str(position.fees), 'fees_by_asset': decimals(position.fees_by_asset),
        } for _, position in sorted(snapshot.positions.items())],
    }


@dataclass(frozen=True)
class ProjectionInputs:
    root_account_id: str
    accounts: tuple[str, ...]
    events: tuple[PortfolioEvent, ...]
    claims: tuple[OrderClaim, ...]
    account_revisions: tuple[tuple[str, str], ...]
    witness_json: tuple[str, ...]
    limits: ProjectionLimits
    source_policy: SourcePolicy = TRADES


@dataclass(frozen=True)
class ProjectionReview:
    body: dict
    digest: str
    projection: ProjectionResult


def capture_projection_inputs(db: Session, root_account_id: str, witnesses: Sequence[dict], *,
                              limits: ProjectionLimits = ProjectionLimits()) -> ProjectionInputs:
    """Pin one SQLite read snapshot and detach inputs, without computing a book.

    Callers must close the session/release its vault lease before replaying.
    Pending ORM writes are refused rather than implicitly flushed. A fresh
    read snapshot also avoids combining events and revisions from two commits.
    """
    if (not isinstance(limits, ProjectionLimits) or not isinstance(witnesses, Sequence)
            or not 0 < len(witnesses) <= limits.max_claims):
        raise ProjectionError('La revisión necesita pruebas contables dentro de sus límites')
    if db.new or db.dirty or db.deleted:
        raise ProjectionError('Guarda o descarta los cambios pendientes antes de revisar')
    connection = db.connection()
    if connection.dialect.name != 'sqlite':
        raise ProjectionError('La revisión local necesita SQLite')
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    db.expire_all()
    with localcontext(Context(prec=CALC_PRECISION, rounding=ROUND_HALF_EVEN)), scope(db, force_full=True):
        accounts, events = original_account_closure(db, root_account_id, limits=limits)
        claims = tuple(claim_from_witness(db, witness) for witness in witnesses)
        supplied = {(claim.source_kind, claim.source_id) for claim in claims}
        policy = EXTERNAL_RECEIPTS if any(is_external_witness(witness)for witness in witnesses) else ALL_RECEIPTS if any(is_gas_witness(witness) for witness in witnesses) else TRADES
        if connection.exec_driver_sql('PRAGMA user_version').scalar_one() >= 74:
            from app.portfolio.economic_acceptance import _heads, _header
            heads = _heads(db, accounts)
            for head in heads.values():
                if head is not None:
                    prior=review_policy(_header(db,head['id'])[1]['draft']['review'])
                    if policy_rank(prior)>policy_rank(policy): policy=prior
        required = _required_sources(db, accounts, gas_accounts=frozenset(accounts) if policy_rank(policy)>=2 else frozenset(),
            external_accounts=frozenset(accounts) if policy==EXTERNAL_RECEIPTS else frozenset())
        if len(supplied) != len(claims) or supplied != required:
            raise ProjectionError('Revisa el orden de todas las operaciones DEX en las cuentas enlazadas')
        if any(claim.account_id not in accounts for claim in claims):
            raise ProjectionError('Una prueba pertenece a otra cuenta sin enlace')
        return ProjectionInputs(str(UUID(root_account_id)), accounts, events, claims,
            tuple((account, account_book_revision(db, account)) for account in accounts),
            tuple(_encoded(witness)[0] for witness in sorted(witnesses, key=lambda item: item['order_record_id'])), limits, policy)


def review_projection(inputs: ProjectionInputs, *, tie_orders: Sequence[TieOrder] = ()) -> ProjectionReview:
    """Pure replay after releasing SQL and the vault mutex. No storage access."""
    if not isinstance(inputs, ProjectionInputs):
        raise ProjectionError('Faltan los datos autenticados de la revisión')
    if any(isinstance(claim,ExternalOrderClaim)for claim in inputs.claims) and inputs.source_policy!=EXTERNAL_RECEIPTS:
        raise ProjectionError('Los efectos externos necesitan su contrato de aceptación versionado')
    if (inputs.source_policy not in (TRADES, ALL_RECEIPTS,EXTERNAL_RECEIPTS)
            or inputs.source_policy == TRADES and (any(isinstance(claim, GasOrderClaim) for claim in inputs.claims)
                or any(is_gas_witness(loads(raw)) for raw in inputs.witness_json))):
        raise ProjectionError('La revisión necesita la versión de fuentes que incluye gas')
    accounts, events, claims = inputs.accounts, inputs.events, inputs.claims
    with localcontext(Context(prec=CALC_PRECISION, rounding=ROUND_HALF_EVEN)):
        projection = project_events(events, claims, tie_orders=tie_orders, limits=inputs.limits)
        by_account = defaultdict(list)
        for event in events:
            by_account[event.account_id].append(event)
        comparisons = []
        for account in accounts:
            try:
                before = _snapshot(fold_events(account, by_account[account]))
                before_error = None
            except PortfolioError:
                before = None
                before_error = 'El orden original no permite calcular un inventario válido'
            after = _snapshot(projection.snapshots.get(account, AccountSnapshot(account)))
            comparisons.append({'account_id': account, 'before': before, 'before_error': before_error,
                'after': after, 'changed': before != after})
        body = {
            'schema': EXTERNAL_SCHEMA if inputs.source_policy==EXTERNAL_RECEIPTS else GAS_SCHEMA if inputs.source_policy == ALL_RECEIPTS else SCHEMA,
            'engine': projection.engine, 'root_account_id': inputs.root_account_id,
            'account_revisions': dict(inputs.account_revisions),
            'events_count': len(events), 'claimed_transactions': len(claims),
            'adjusted_transfer_legs': projection.adjusted_transfer_legs,
            'witnesses': [loads(raw) for raw in inputs.witness_json],
            'tie_orders': sorted([{'account_id': item.account_id, 'at': item.at.isoformat(),
                'nodes': list(item.nodes)} for item in tie_orders], key=lambda item: (item['at'], item['account_id'])),
            'comparisons': comparisons, 'applied_to_ledger': False, 'trading_authority': False,
        }
        if inputs.source_policy != TRADES:
            body['source_policy'] = inputs.source_policy
        # Canonical round-trip detaches caller-owned nested witness dictionaries.
        raw, digest = _encoded(body)
        return ProjectionReview(loads(raw), digest, projection)
