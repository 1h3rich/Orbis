"""Review which outside movements leave the bot's own MEXC fills attributable."""

from collections import Counter
from decimal import Decimal, localcontext
from itertools import groupby

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.live_execution.journal import LiveJournalError, recover_spot_intent_in_session
from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.policy import MEXC_MANUAL_ORIGIN
from app.live_execution.reconciliation_view import _project_fills
from app.live_execution.spot_bot_position import AssignedSpotSale, SpotBotLedger, fold_owned_spot_fills
from app.live_execution.spot_bot_store import (
    SpotBotStoreError, bot_external_book_events, bot_record, encoded,
    owned_spot_bot_ledger, ownership_fingerprint,
)
from app.portfolio.domain import PortfolioError
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent,
    RealLiveSpotSettlement, RealMexcAccountLink, RealMexcFillBooking, RealSpotBot,
)
from app.portfolio.repository import account_book_revision, account_snapshot
from app.portfolio.vault import RealVault


_CASH_IN = ('CONTRIBUTION', 'CASH_TRANSFER_IN')
_CASH_OUT = ('WITHDRAWAL', 'CASH_TRANSFER_OUT')
_MANUAL_TRADES = ("BUY", "SELL")
_MAX_MOVEMENTS = 64


def _manual_trade_proof(db: Session, row: RealSpotBot, event: RealEvent,
                        base: str, quote: str) -> tuple[str, str, str, str] | None:
    """Find an exact booked fill from an ORBIS manual, settled trade of this pair."""
    booking = db.scalar(select(RealMexcFillBooking).where(RealMexcFillBooking.event_id == event.id))
    fill = db.get(MexcObservedFill, booking.fill_id) if booking is not None else None
    if (fill is None or fill.account_ref != row.account_ref or fill.symbol != base + quote
            or fill.side != event.kind or fill.commission > 0 and fill.commission_asset not in (base, quote)):
        return None
    settlements = db.execute(
        select(RealLiveSpotSettlement, RealLiveOrderObservation)
        .join(RealLiveOrderObservation, RealLiveOrderObservation.id == RealLiveSpotSettlement.observation_id)
        .where(
            RealLiveSpotSettlement.account_ref == row.account_ref,
            RealLiveSpotSettlement.external_order_id == fill.order_id,
            RealLiveOrderObservation.account_ref == row.account_ref,
            RealLiveOrderObservation.symbol == fill.symbol,
            RealLiveOrderObservation.external_order_id == fill.order_id,
        )
        .limit(2)
    ).all()
    if len(settlements) != 1:
        return None
    settlement, observation = settlements[0]
    intent = db.get(RealLiveSpotIntent, settlement.intent_id)
    link = db.get(RealMexcAccountLink, row.account_ref)
    if (intent is None or intent.status != 'RESERVATION_CLOSED' or intent.side != fill.side
            or intent.account_ref != row.account_ref or intent.symbol != fill.symbol
            or intent.base_asset != base or intent.quote_asset != quote
            or observation.intent_id != intent.id or observation.status != settlement.external_status
            or settlement.balance_observed_at < observation.observed_at
            or not booked_event_matches(fill, intent, booking, event, link)):
        return None
    try:
        if recover_spot_intent_in_session(db, intent.id).strategy_version_id != MEXC_MANUAL_ORIGIN:
            return None
        fills = db.scalars(select(MexcObservedFill).where(
            MexcObservedFill.account_ref == row.account_ref, MexcObservedFill.symbol == fill.symbol,
            MexcObservedFill.order_id == fill.order_id,
        )).all()
        if _project_fills(intent, observation, fills)['status'] != 'NEEDS_ACCOUNT_LINK_AND_BOOKING':
            return None
    except (LiveJournalError, ValueError, TypeError, ArithmeticError):
        return None
    return fill.id, fill.payload_digest, observation.id, observation.payload_digest


def _sale_share(event: RealEvent, fill_id: str, units: Decimal, base: str) -> AssignedSpotSale:
    base_fee = event.fee if event.fee_asset == base else Decimal(0)
    return AssignedSpotSale(fill_id, event.occurred_at, units, event.quantity + base_fee,
                            event.gross_amount - (event.fee - base_fee))


def read_assigned_spot_sale(db: Session, row: RealSpotBot, symbol: str, evidence: dict) -> AssignedSpotSale:
    base, quote = symbol.split("/")
    event = db.get(RealEvent, evidence["event_id"])
    if (event is None or event.account_id != row.account_id or event.kind != "SELL" or
            event.payload_digest != evidence["event_hash"]):
        raise SpotBotStoreError("El asiento de la venta atribuida cambió")
    proof = _manual_trade_proof(db, row, event, base, quote)
    if proof != tuple(evidence[key] for key in ("fill_id", "fill_hash", "observation_id", "observation_hash")):
        raise SpotBotStoreError("La ejecución de la venta atribuida cambió")
    with localcontext() as context:
        context.prec = 300
        return _sale_share(event, evidence["fill_id"], Decimal(evidence["units"]), base)


def _outside_inventory(vault: RealVault, db: Session, row: RealSpotBot, base: str, quote: str,
                       owned: Decimal, trades: list[tuple[RealEvent, Decimal]]
                       ) -> tuple[Decimal, Decimal, list[tuple[RealEvent, Decimal]]] | None:
    """Check available outside inventory before every debit, at the fill's own timestamp."""
    last_other_asset_at = db.scalar(select(func.max(RealEvent.occurred_at)).where(
        RealEvent.account_id == row.account_id, RealEvent.asset == base,
        RealEvent.id.not_in([event.id for event, _ in trades]),
    ))
    if last_other_asset_at is not None and any(event.occurred_at <= last_other_asset_at for event, _ in trades):
        return None
    try:
        book = account_snapshot(db, row.account_id)
        if any(asset == base and currency != quote and position.quantity
               for (asset, currency), position in book.positions.items()):
            return None
        position = book.positions.get((base, quote))
        other_bots = db.scalars(select(RealSpotBot.id).where(
            RealSpotBot.account_id == row.account_id, RealSpotBot.id != row.id,
        )).all()
        other_owned = Decimal(0)
        for identifier in other_bots:
            _, other_config, _ = bot_record(db, identifier)
            if other_config['source']['symbol'].split('/')[0] == base:
                other, _ = owned_spot_bot_ledger(vault, identifier, db=db)
                other_owned += other.position.units
    except (PortfolioError, SpotBotStoreError):
        return None

    after = (position.quantity if position is not None else Decimal(0)) - owned - other_owned
    before = after - sum(change for _, change in trades)
    remaining = before
    if remaining < 0:
        return None
    ties = Counter(event.occurred_at for event, _ in trades)
    assigned = []
    remaining_owned = owned
    # Sales precede purchases with an equal exchange timestamp. An assignment
    # from bot units requires an unambiguous timestamp and a unique owner.
    for event, change in sorted(trades, key=lambda item: (item[0].occurred_at, item[1] >= 0)):
        remaining += change
        if remaining < 0:
            shortage = -remaining
            if other_owned or ties[event.occurred_at] != 1 or shortage > remaining_owned:
                return None
            assigned.append((event, shortage))
            remaining_owned -= shortage
            remaining = Decimal(0)
    return (before, remaining, assigned) if remaining == after + owned - remaining_owned else None


def review_spot_bot_allocation(vault: RealVault, db: Session, row: RealSpotBot, config: dict,
                               ledger: SpotBotLedger, ledger_revision: str, book_revision: str) -> tuple[str, dict]:
    events = bot_external_book_events(db, row.account_id, row.id)
    current = ownership_fingerprint(events)
    previous = config.get('ownership_baseline')
    if ledger.position.units == 0 or current == previous:
        return current, {'state': 'UNCHANGED'}
    if not previous:
        return current, {'state': 'ASSET_REVIEW_REQUIRED'}

    # Remove whole recording cohorts from the end and reproduce the previous
    # fingerprint before interpreting any newly discovered movement.
    chronological = sorted(events, key=lambda item: item.recorded_at, reverse=True)
    changed = []
    base, quote = config['source']['symbol'].split('/')
    for _, batch in groupby(chronological, key=lambda item: item.recorded_at):
        cohort = list(batch)
        if len(changed) + len(cohort) > _MAX_MOVEMENTS or any(item.quote_currency != quote or (
                item.kind not in (*_CASH_IN, *_CASH_OUT) and (item.kind not in _MANUAL_TRADES or item.asset != base)
        ) for item in cohort):
            return current, {'state': 'ASSET_REVIEW_REQUIRED'}
        changed.extend(cohort)
        if ownership_fingerprint(chronological[len(changed):]) == previous:
            break
    else:
        return current, {'state': 'ASSET_REVIEW_REQUIRED'}

    proofs = {}
    trades = []
    outside = None
    with localcontext() as context:
        context.prec = 300
        delta = purchased = sold = Decimal(0)
        for event in changed:
            if event.kind in _CASH_IN:
                delta += event.gross_amount
            elif event.kind in _CASH_OUT:
                delta -= event.gross_amount + event.fee
            else:
                proof = _manual_trade_proof(db, row, event, base, quote)
                if proof is None:
                    return current, {'state': 'ASSET_REVIEW_REQUIRED'}
                proofs[event.id] = proof
                base_fee = event.fee if event.fee_asset == base else Decimal(0)
                quote_fee = event.fee - base_fee
                if event.kind == 'BUY':
                    change = event.quantity - base_fee
                    purchased += change
                    delta -= event.gross_amount + quote_fee
                else:
                    change = -(event.quantity + base_fee)
                    sold -= change
                    delta += event.gross_amount - quote_fee
                trades.append((event, change))
        if sold:
            outside = _outside_inventory(vault, db, row, base, quote, ledger.position.units, trades)
            if outside is None:
                return current, {'state': 'ASSET_REVIEW_REQUIRED'}
    assigned = outside[2] if outside is not None else []
    review = {
        'state': ('MANUAL_BOT_DEBIT' if assigned else 'MANUAL_SALES' if outside is not None else
                  'MANUAL_PURCHASES' if proofs else 'CASH_ONLY'),
        'quote_currency': quote, 'cash_delta_quote': format(delta, 'f'),
        'movement_count': len(changed), 'owned_units': str(ledger.position.units),
        'owned_cost_quote': str(ledger.position.cost_quote),
    }
    if proofs:
        review.update(base_asset=base, manual_purchase_units=format(purchased, 'f'), manual_fill_count=len(proofs))
    if outside is not None:
        review.update(manual_sale_units=format(sold, 'f'), outside_units_before=format(outside[0], 'f'),
                      outside_units_after=format(outside[1], 'f'))
    if assigned:
        evidence = []
        projected = []
        with localcontext() as context:
            context.prec = 300
            for event, units in assigned:
                fill_id, fill_hash, observation_id, observation_hash = proofs[event.id]
                evidence.append({"event_id": event.id, "event_hash": event.payload_digest,
                                 "fill_id": fill_id, "fill_hash": fill_hash, "observation_id": observation_id,
                                 "observation_hash": observation_hash, "units": str(units)})
                projected.append(_sale_share(event, fill_id, units, base))
            after = fold_owned_spot_fills(projected, initial=ledger)
            review.update(assigned_sales=evidence, assigned_units=str(ledger.position.units - after.position.units),
                          owned_units_after=str(after.position.units), owned_cost_quote_after=str(after.position.cost_quote),
                          assigned_net_quote=str(after.net_cash_flow_quote - ledger.net_cash_flow_quote))
    digest = encoded({
        **review, 'bot_id': row.id, 'account_id': row.account_id,
        'config_digest': row.config_digest, 'state_digest': row.state_digest, 'ledger_revision': ledger_revision,
        'book_revision': book_revision, 'old_ownership': previous, 'new_ownership': current,
        'events': sorted((item.id, item.payload_digest) for item in changed),
        **({'manual_fills': sorted(proofs.values())} if proofs else {}),
    })[1]
    return current, {**review, 'review_digest': digest}


def spot_bot_allocation_review(vault: RealVault, account_id: str, bot_id: str) -> dict:
    with vault.session() as db:
        db.connection().exec_driver_sql('BEGIN')
        row, config, _ = bot_record(db, bot_id)
        if row.account_id != account_id:
            raise SpotBotStoreError('El bot no pertenece a esta cuenta')
        if row.status != 'PAUSED':
            raise SpotBotStoreError('Detén el bot antes de revisar su asignación')
        ledger, revision = owned_spot_bot_ledger(vault, bot_id, db=db)
        book = account_book_revision(db, account_id)
        return review_spot_bot_allocation(vault, db, row, config, ledger, revision, book)[1]
