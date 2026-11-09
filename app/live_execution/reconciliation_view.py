"""Vista de solo lectura del ciclo MEXC REAL; nunca libera reservas."""

from __future__ import annotations

from decimal import Decimal, localcontext
import json

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.mexc_order_observer import observed_order_digest_matches
from app.live_execution.journal import LiveJournalError, parse_archived_spot_signal_source, recover_spot_intent_in_session
from app.portfolio.mexc_observations import observed_fill_digest_matches
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent,
    RealMexcAccountLink, RealMexcFillBooking,
    RealLiveSpotSettlement, RealLiveStop,
)
from app.portfolio.vault import RealVault


PENDING_SPOT_STATUSES = ("RESERVED", "SUBMISSION_UNKNOWN")
_FINAL = frozenset(("FILLED", "CANCELED", "PARTIALLY_CANCELED"))


def _project_fills(intent: RealLiveSpotIntent,
                   observed: RealLiveOrderObservation | None,
                   fills: list[MexcObservedFill]) -> dict:
    """Variación observada por activo; jamás equivale a un asiento REAL."""
    if observed is None:
        return {"status": "ORDER_NOT_OBSERVED", "deltas": None, "fee_by_asset": None,
                "fill_count": 0, "fee_model_compatible": False,
                "quote_fee_model_compatible": False}
    if (len(fills) != observed.fill_count
            or any(fill.account_ref != intent.account_ref or fill.symbol != intent.symbol
                   or fill.order_id != observed.external_order_id or fill.side != intent.side
                   for fill in fills)):
        return {"status": "FILL_ARCHIVE_INCOMPLETE", "deltas": None, "fee_by_asset": None,
                "fill_count": len(fills), "fee_model_compatible": False,
                "quote_fee_model_compatible": False}
    if any(not observed_fill_digest_matches(fill) for fill in fills):
        return {"status": "FILL_ARCHIVE_TAMPERED", "deltas": None, "fee_by_asset": None,
                "fill_count": len(fills), "fee_model_compatible": False,
                "quote_fee_model_compatible": False}
    if not observed_order_digest_matches(observed, fills):
        return {"status": "ORDER_ARCHIVE_TAMPERED", "deltas": None, "fee_by_asset": None,
                "fill_count": len(fills), "fee_model_compatible": False,
                "quote_fee_model_compatible": False}
    with localcontext() as context:
        context.prec = 300
        base = sum((fill.quantity for fill in fills), Decimal(0))
        quote = sum((fill.quote_quantity for fill in fills), Decimal(0))
        fees: dict[str, Decimal] = {}
        for fill in fills:
            fees[fill.commission_asset] = fees.get(fill.commission_asset, Decimal(0)) + fill.commission
        archived_fees = {asset: Decimal(amount) for asset, amount in json.loads(observed.fee_json)}
        if base != observed.executed_base or quote != observed.cumulative_quote or fees != archived_fees:
            return {"status": "FILL_TOTAL_MISMATCH", "deltas": None, "fee_by_asset": None,
                    "fill_count": len(fills), "fee_model_compatible": False,
                    "quote_fee_model_compatible": False}
        if observed.status not in _FINAL:
            return {"status": "ORDER_NOT_FINAL", "deltas": None, "fee_by_asset": None,
                    "fill_count": observed.fill_count, "fee_model_compatible": False,
                    "quote_fee_model_compatible": False}
        if not base and not fills:
            return {"status": "FINAL_WITHOUT_EXECUTION", "deltas": {}, "fee_by_asset": {},
                    "fill_count": 0, "fee_model_compatible": False,
                    "quote_fee_model_compatible": False}
        direction = Decimal(1) if intent.side == "BUY" else Decimal(-1)
        deltas = {intent.base_asset: direction * base,
                  intent.quote_asset: -direction * quote}
        for asset, amount in fees.items():
            deltas[asset] = deltas.get(asset, Decimal(0)) - amount
        if any(asset not in (intent.quote_asset, intent.base_asset) and amount > 0 for asset, amount in fees.items()):
            status = "MULTI_ASSET_FEE_UNSUPPORTED"
        elif (intent.side == "BUY" and any(
                fill.commission_asset == intent.base_asset and fill.commission >= fill.quantity
                for fill in fills)):
            status = "BASE_FEE_EXCEEDS_FILL"
        else:
            status = "NEEDS_ACCOUNT_LINK_AND_BOOKING"
        return {"status": status,
                "deltas": {asset: format(amount, "f") for asset, amount in sorted(deltas.items())},
                "fee_by_asset": {asset: format(amount, "f") for asset, amount in sorted(fees.items())},
                "fill_count": len(fills),
                "fee_model_compatible": status == "NEEDS_ACCOUNT_LINK_AND_BOOKING",
                "quote_fee_model_compatible": (status == "NEEDS_ACCOUNT_LINK_AND_BOOKING"
                                               and all(fill.commission_asset == intent.quote_asset
                                                       or fill.commission == 0 for fill in fills))}


def _order_progress(db: Session, intent: RealLiveSpotIntent, observed: RealLiveOrderObservation | None,
                    projection: dict, stage: str) -> dict[str, str] | None:
    if (observed is None or stage == "RECONCILIATION_INCONSISTENT"
            or projection["status"] in ("FILL_ARCHIVE_INCOMPLETE", "FILL_TOTAL_MISMATCH")):
        return None
    try:
        requested = recover_spot_intent_in_session(db, intent.id).quantity_base
    except LiveJournalError:
        return None
    executed = observed.executed_base
    if not 0 <= executed <= requested:
        return None
    with localcontext() as context:
        context.prec = 300
        unfilled = requested - executed
    return {"requested_base": format(requested, "f"), "executed_base": format(executed, "f"),
            "unfilled_base": format(unfilled, "f"),
            "unfilled_state": "NONE" if not unfilled else "CANCELLED" if observed.status in _FINAL else "OPEN"}


def list_spot_execution_intents(vault: RealVault, *, pending_link: tuple[str, str] | None = None) -> list[dict]:
    """Distingue intención local, orden observada y contabilidad pendiente.

    Los saldos retenidos son reservas locales, no balances confirmados por MEXC.
    La vista no consulta red ni modifica la bóveda.
    """
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN")
        query = select(RealLiveSpotIntent)
        observed_query = select(RealLiveOrderObservation)
        booking_query = select(RealMexcFillBooking)
        link_query = select(RealMexcAccountLink)
        settlement_query = select(RealLiveSpotSettlement)
        stop_query = select(RealLiveStop.account_ref).where(RealLiveStop.stopped.is_(True))
        if pending_link is not None:
            account_id, ref = pending_link
            query = query.join(RealMexcAccountLink, RealMexcAccountLink.account_ref == RealLiveSpotIntent.account_ref).where(
                RealLiveSpotIntent.account_ref == ref, RealMexcAccountLink.account_id == account_id,
                RealLiveSpotIntent.status.in_(PENDING_SPOT_STATUSES),
            )
            pending_ids = query.with_only_columns(RealLiveSpotIntent.id)
            observed_query = observed_query.where(RealLiveOrderObservation.intent_id.in_(pending_ids))
            settlement_query = settlement_query.where(RealLiveSpotSettlement.intent_id.in_(pending_ids))
            pending_fills = select(MexcObservedFill.id).join(RealLiveOrderObservation, and_(
                RealLiveOrderObservation.account_ref == MexcObservedFill.account_ref,
                RealLiveOrderObservation.symbol == MexcObservedFill.symbol,
                RealLiveOrderObservation.external_order_id == MexcObservedFill.order_id,
            )).where(RealLiveOrderObservation.intent_id.in_(pending_ids))
            booking_query = booking_query.where(RealMexcFillBooking.fill_id.in_(pending_fills))
            link_query = link_query.where(RealMexcAccountLink.account_ref == ref, RealMexcAccountLink.account_id == account_id)
            stop_query = stop_query.where(RealLiveStop.account_ref == ref)
        intents = db.scalars(query.order_by(RealLiveSpotIntent.created_at.desc(), RealLiveSpotIntent.id.desc())).all()
        if not intents:
            return []
        observations = db.scalars(observed_query.order_by(
            RealLiveOrderObservation.observed_at.desc(), RealLiveOrderObservation.id.desc(),
        )).all()
        latest = {}
        for item in observations:
            latest.setdefault(item.intent_id, item)
        bookings = {item.fill_id: item for item in db.scalars(booking_query).all()}
        events = {item.id: item for item in db.scalars(select(RealEvent).where(
            RealEvent.id.in_([item.event_id for item in bookings.values()]),
        )).all()}
        links = {item.account_ref: item for item in db.scalars(link_query).all()}
        settlements = {item.intent_id: item for item in db.scalars(settlement_query).all()}
        stopped_accounts = set(db.scalars(stop_query).all())
        result = []
        for intent in intents:
            try:
                source = parse_archived_spot_signal_source(intent)
                origin = ({"status": ("ARCHIVED_PAPER" if source.observation_digest is not None
                                      else "LEGACY_PAPER_UNPINNED"),
                           "strategy_version_id": source.strategy_version_id,
                           "paper_session_id": source.paper_session_id,
                           "dataset_id": source.dataset_id, "dataset_hash": source.dataset_hash,
                           "timeframe": source.timeframe, "candle_t": source.candle_t,
                           "signal_at": source.signal_at.isoformat()}
                          if source is not None else None)
            except LiveJournalError:
                origin = {"status": "INVALID_SOURCE"}
            observed = latest.get(intent.id)
            fills = (db.scalars(select(MexcObservedFill).where(
                MexcObservedFill.account_ref == intent.account_ref,
                MexcObservedFill.symbol == intent.symbol,
                MexcObservedFill.order_id == observed.external_order_id,
            )).all() if observed is not None else [])
            projection = _project_fills(intent, observed, fills)
            has_booking = any(fill.id in bookings for fill in fills)
            booked = bool(fills) and all(booked_event_matches(
                fill, intent, bookings.get(fill.id),
                events.get(bookings[fill.id].event_id) if fill.id in bookings else None,
                links.get(intent.account_ref),
            ) for fill in fills)
            settled = intent.id in settlements
            archive_consistent = projection["status"] in (
                "FINAL_WITHOUT_EXECUTION", "NEEDS_ACCOUNT_LINK_AND_BOOKING")
            if (projection["status"] in ("FILL_ARCHIVE_TAMPERED", "ORDER_ARCHIVE_TAMPERED")
                    or (intent.status == "RESERVATION_CLOSED") != settled
                    or (has_booking and not booked)
                    or (booked and not archive_consistent)
                    or (settled and (not archive_consistent
                                    or bool(fills) != booked))):
                stage = "RECONCILIATION_INCONSISTENT"
            elif intent.status == "RESERVATION_CLOSED":
                stage = "FINAL_RESERVATION_CLOSED"
            elif intent.status == "SUBMISSION_UNKNOWN":
                if observed is None:
                    stage = "ORDER_UNVERIFIED"
                elif observed.status in _FINAL:
                    stage = ("FINAL_BOOKED_RESERVED" if booked else
                             "FINAL_NO_EXECUTION" if projection["status"] == "FINAL_WITHOUT_EXECUTION"
                             else "FINAL_OBSERVED_UNBOOKED")
                else:
                    stage = "ORDER_OBSERVED_OPEN"
            elif intent.status == "RESERVED":
                stage = "RESERVED_NOT_SUBMITTED"
            elif intent.status == "STOPPED":
                stage = "STOPPED_BEFORE_SUBMISSION"
            else:
                stage = "REJECTED_BEFORE_SUBMISSION"
            cancel_state = "NOT_ATTEMPTED"
            if intent.reason in ("CANCEL_ATTEMPTED_OUTCOME_UNKNOWN", "CANCEL_ACKNOWLEDGED_REOBSERVE"):
                if observed is not None and observed.observed_at > intent.updated_at:
                    cancel_state = ("FINAL_OBSERVED_AFTER_CANCEL" if observed.status in _FINAL
                                    else "OPEN_OBSERVED_AFTER_CANCEL")
                elif intent.reason == "CANCEL_ACKNOWLEDGED_REOBSERVE":
                    cancel_state = "ACKNOWLEDGED_NEEDS_REOBSERVATION"
                else:
                    cancel_state = "ATTEMPT_OUTCOME_UNKNOWN"
            result.append({
                "intent_id": intent.id,
                "account_id": (links[intent.account_ref].account_id
                               if intent.account_ref in links else None),
                "policy_id": intent.policy_id,
                "origin": origin,
                "account_ref_hint": intent.account_ref[:8] + "…" + intent.account_ref[-8:],
                "symbol": intent.symbol,
                "side": intent.side,
                "created_at": intent.created_at.isoformat(),
                "journal_status": intent.status,
                "journal_reason": intent.reason,
                "cancel_state": cancel_state,
                "account_stopped": intent.account_ref in stopped_accounts,
                "stage": stage,
                "reserved_quote": format(intent.reserved_quote, "f"),
                "reserved_base": format(intent.reserved_base, "f"),
                "quote_asset": intent.quote_asset,
                "base_asset": intent.base_asset,
                "external_order_observed": observed is not None,
                "external_status": observed.status if observed is not None else None,
                "executed_base_observed": format(observed.executed_base, "f") if observed is not None else None,
                "cumulative_quote_observed": format(observed.cumulative_quote, "f") if observed is not None else None,
                "observed_at": observed.observed_at.isoformat() if observed is not None else None,
                "progress": _order_progress(db, intent, observed, projection, stage),
                "booked_to_portfolio": booked,
                "reservation_closed": settled,
                "balance_observed_at": (settlements[intent.id].balance_observed_at.isoformat()
                                        if intent.id in settlements else None),
                "reconciliation": projection,
            })
        return result
