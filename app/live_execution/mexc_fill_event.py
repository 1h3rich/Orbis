"""Contrato exacto entre un fill MEXC observado y su asiento REAL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import localcontext
from uuid import UUID, uuid5

from app.portfolio.domain import EventInput, PortfolioError
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveSpotIntent,
    RealMexcAccountLink, RealMexcFillBooking,
)
from app.portfolio.repository import _digest


REQUEST_NAMESPACE = UUID("315509db-44aa-495d-916c-1889f6dfd437")


def fill_event_input(fill: MexcObservedFill, intent: RealLiveSpotIntent) -> EventInput:
    with localcontext() as context:
        context.prec = 300
        gross_override = (fill.quote_quantity if fill.quote_quantity != fill.price * fill.quantity
                          else None)
    return EventInput(
        kind=intent.side, asset=intent.base_asset,
        quote_currency=intent.quote_asset, quantity=fill.quantity,
        unit_price=fill.price, gross_override=gross_override,
        fee=fill.commission,
        fee_asset=(fill.commission_asset if fill.commission_asset == intent.base_asset
                   and fill.commission > 0 else None),
        occurred_at=datetime(1970, 1, 1, tzinfo=timezone.utc)
        + timedelta(milliseconds=fill.time_ms),
        note="MEXC Spot · fill verificado",
    )


def booked_event_matches(fill: MexcObservedFill, intent: RealLiveSpotIntent,
                         booking: RealMexcFillBooking | None,
                         event: RealEvent | None,
                         link: RealMexcAccountLink | None) -> bool:
    if (booking is None or event is None or link is None
            or booking.fill_id != fill.id or booking.event_id != event.id
            or booking.account_id != event.account_id
            or link.account_id != booking.account_id):
        return False
    try:
        expected = fill_event_input(fill, intent)
    except (PortfolioError, TypeError, ValueError, ArithmeticError):
        return False
    return (event.request_id == str(uuid5(REQUEST_NAMESPACE, fill.id))
            and event.payload_digest == _digest(expected)
            and event.kind == expected.kind and event.asset == expected.asset
            and event.quote_currency == expected.quote_currency
            and event.quantity == expected.quantity
            and event.unit_price == expected.unit_price
            and event.gross_amount == expected.gross_amount
            and event.fee == expected.fee and event.fee_asset == expected.fee_asset
            and event.occurred_at == expected.occurred_at
            and event.note == expected.note)
