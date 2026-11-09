"""Procedencia del coste local de un par MEXC, no conciliación de cuenta.

Un coste declarado puede ser útil para la cartera, pero sólo un asiento que
coincida con un fill propio archivado prueba su origen en la evidencia local.
Ni siquiera una cadena local íntegra demuestra ausencia de fills externos.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.mexc_fill_event import booked_event_matches
from app.portfolio.domain import _symbol, utc_datetime
from app.portfolio.mexc_observations import observed_fill_digest_matches
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent,
    RealMexcAccountLink, RealMexcFillBooking,
)
from app.portfolio.repository import _digest, _from_row


@dataclass(frozen=True, slots=True)
class MexcCostProvenance:
    status: str
    asset_event_count: int
    verified_fill_count: int
    source: str = "LOCAL_BOOK_AND_ARCHIVED_FILLS_ONLY"
    reconciled_with_venue: bool = False
    execution_ready: bool = False


def audit_mexc_cost_provenance(db: Session, account_id: str, account_ref: str,
                               base_asset: str, quote_asset: str, *,
                               now: datetime) -> MexcCostProvenance:
    """Exige que cada cambio de cantidad/coste proceda de un fill propio exacto."""
    current = utc_datetime(now)
    base, quote = _symbol(base_asset, "base asset"), _symbol(quote_asset, "quote asset")
    if base == quote:
        raise ValueError("Cost provenance requires distinct assets")
    link = db.get(RealMexcAccountLink, account_ref)
    if link is None or link.account_id != account_id:
        return MexcCostProvenance("ACCOUNT_LINK_MISSING", 0, 0)
    events = db.scalars(select(RealEvent).where(
        RealEvent.account_id == account_id, RealEvent.asset == base,
        RealEvent.quote_currency == quote, RealEvent.occurred_at <= current,
    ).order_by(RealEvent.occurred_at, RealEvent.recorded_at, RealEvent.id)).all()
    for event in events:
        try:
            if event.payload_digest != _digest(_from_row(event).input):
                return MexcCostProvenance("BOOK_EVENT_CHANGED", len(events), 0)
        except (TypeError, ValueError, ArithmeticError):
            return MexcCostProvenance("BOOK_EVENT_CHANGED", len(events), 0)
    effective = [row for row in events if row.kind != "TRANSFER"]
    if not effective:
        return MexcCostProvenance("NO_POSITION_HISTORY", 0, 0)
    verified = 0
    for event in effective:
        if event.kind not in ("BUY", "SELL"):
            return MexcCostProvenance("DECLARED_OR_TRANSFERRED_COST", len(effective), verified)
        booking = db.scalar(select(RealMexcFillBooking).where(
            RealMexcFillBooking.account_id == account_id,
            RealMexcFillBooking.event_id == event.id,
        ))
        fill = db.get(MexcObservedFill, booking.fill_id) if booking else None
        if (fill is None or not observed_fill_digest_matches(fill)
                or fill.account_ref != account_ref
                or fill.symbol != base + quote):
            return MexcCostProvenance("UNLINKED_OR_CHANGED_TRADE", len(effective), verified)
        observations = db.scalars(select(RealLiveOrderObservation).where(
            RealLiveOrderObservation.account_ref == account_ref,
            RealLiveOrderObservation.symbol == fill.symbol,
            RealLiveOrderObservation.external_order_id == fill.order_id,
        )).all()
        intent_ids = {row.intent_id for row in observations}
        if len(intent_ids) != 1:
            return MexcCostProvenance("UNLINKED_OR_CHANGED_TRADE", len(effective), verified)
        intent = db.get(RealLiveSpotIntent, next(iter(intent_ids)))
        if (intent is None or intent.account_ref != account_ref
                or intent.symbol != fill.symbol or intent.side != fill.side
                or not booked_event_matches(fill, intent, booking, event, link)):
            return MexcCostProvenance("UNLINKED_OR_CHANGED_TRADE", len(effective), verified)
        verified += 1
    return MexcCostProvenance("LOCAL_FILL_CHAIN_MATCHES", len(effective), verified)
