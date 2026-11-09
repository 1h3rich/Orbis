"""Asiento explícito de fills MEXC verificados en una cuenta REAL.

La observación externa no implica contabilidad automática. La operación completa
se registra en una única transacción y cada fill conserva un vínculo único.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.mexc_fill_event import (
    REQUEST_NAMESPACE, booked_event_matches, fill_event_input,
)
from app.live_execution.reconciliation_view import _project_fills
from app.portfolio.domain import PortfolioError, PortfolioEvent
from app.portfolio.models import (
    MexcObservedFill, RealAccount, RealEvent, RealLiveOrderObservation,
    RealLiveSpotIntent, RealMexcAccountLink, RealMexcFillBooking,
)
from app.portfolio.repository import _digest, _validate_proposed_events
from app.portfolio.vault import RealVault


class MexcBookingError(PortfolioError):
    """La orden observada aún no puede asentarse en el libro elegido."""


def _id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MexcBookingError("Se requiere un identificador válido") from exc


def book_observed_spot_order(vault: RealVault, intent_id: str, account_id: str,
                             *, now: datetime | None = None) -> dict:
    """Importa todos los fills compatibles o ninguno; no libera reservas ni envía órdenes."""
    intent_id, account_id = _id(intent_id), _id(account_id)
    booked_at = now or datetime.now(timezone.utc)
    if booked_at.tzinfo is None:
        raise MexcBookingError("La hora de asiento debe incluir zona horaria")
    booked_at = booked_at.astimezone(timezone.utc)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            result = book_spot_order_in_transaction(db, intent_id, account_id, booked_at=booked_at)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def book_spot_order_in_transaction(db: Session, intent_id: str, account_id: str, *, booked_at: datetime) -> dict:
    """Use the caller's immediate transaction without committing."""
    account = db.get(RealAccount, account_id)
    intent = db.get(RealLiveSpotIntent, intent_id)
    if account is None or intent is None:
        raise MexcBookingError("Cuenta u orden local inexistente")
    if intent.status not in ("SUBMISSION_UNKNOWN", "RESERVATION_CLOSED"):
        raise MexcBookingError("La intención no consta como enviada")
    observed = db.scalars(select(RealLiveOrderObservation).where(
        RealLiveOrderObservation.intent_id == intent_id,
    ).order_by(RealLiveOrderObservation.observed_at.desc(),
               RealLiveOrderObservation.id.desc()).limit(1)).first()
    if observed is None:
        raise MexcBookingError("La orden externa aún no se ha observado")
    fills = db.scalars(select(MexcObservedFill).where(
        MexcObservedFill.account_ref == intent.account_ref,
        MexcObservedFill.symbol == intent.symbol,
        MexcObservedFill.order_id == observed.external_order_id,
    ).order_by(MexcObservedFill.time_ms, MexcObservedFill.trade_id)).all()
    projection = _project_fills(intent, observed, fills)
    if projection["status"] != "NEEDS_ACCOUNT_LINK_AND_BOOKING":
        raise MexcBookingError(f"La orden no es asentable: {projection['status']}")
    if not fills:
        raise MexcBookingError("No hay fills para importar")

    link = db.get(RealMexcAccountLink, intent.account_ref)
    if link is not None and link.account_id != account_id:
        raise MexcBookingError("Esta cuenta MEXC ya está vinculada a otra cartera REAL")
    existing = db.scalars(select(RealMexcFillBooking).where(
        RealMexcFillBooking.fill_id.in_([fill.id for fill in fills]),
    )).all()
    if existing:
        if (len(existing) != len(fills)
                or any(row.account_id != account_id for row in existing)):
            raise MexcBookingError("Los fills ya están asentados parcialmente o en otra cuenta")
        events = {row.id: row for row in db.scalars(select(RealEvent).where(
            RealEvent.id.in_([row.event_id for row in existing]),
        )).all()}
        by_fill = {row.fill_id: row for row in existing}
        if any(not booked_event_matches(fill, intent, by_fill.get(fill.id),
                                         events.get(by_fill[fill.id].event_id)
                                         if fill.id in by_fill else None, link)
               for fill in fills):
            raise MexcBookingError("Un fill contabilizado ya no coincide con el libro REAL")
        return {"status": "ALREADY_BOOKED", "intent_id": intent_id,
                "account_id": account_id, "fill_count": len(fills),
                "event_ids": [row.event_id for row in sorted(existing, key=lambda row: row.fill_id)]}
    if intent.status == "RESERVATION_CLOSED":
        raise MexcBookingError("La reserva ya está cerrada y no admite un asiento nuevo")

    proposed: list[tuple[MexcObservedFill, PortfolioEvent]] = []
    for fill in fills:
        item = fill_event_input(fill, intent)
        proposed.append((fill, PortfolioEvent(
            id=str(uuid4()), account_id=account_id,
            request_id=str(uuid5(REQUEST_NAMESPACE, fill.id)),
            recorded_at=booked_at, input=item,
        )))
    # El libro exige inventario para ventas. Para compras exigimos además
    # que el efectivo ya registrado cubra el gasto: no inventamos fondos.
    combined = _validate_proposed_events(db, account_id, [event for _, event in proposed])
    if intent.side == "BUY":
        new_ids = {event.id for _, event in proposed}
        cash = Decimal(0)
        ordered = sorted(combined, key=lambda event: event.sort_key)
        with localcontext() as context:
            context.prec = 300
            for event in ordered:
                item = event.financial_input
                if item.quote_currency != intent.quote_asset:
                    continue
                if item.kind == "CONTRIBUTION":
                    cash += item.amount
                elif item.kind == "WITHDRAWAL":
                    cash -= item.amount
                elif item.kind == "BUY":
                    cash -= item.gross_amount + (
                        item.fee if item.effective_fee_asset == item.quote_currency else Decimal(0))
                elif item.kind == "SELL":
                    cash += item.gross_amount - (
                        item.fee if item.effective_fee_asset == item.quote_currency else Decimal(0))
                if event.id in new_ids and cash < 0:
                    raise MexcBookingError(
                        "Registra primero el efectivo MEXC disponible en la fecha de compra"
                    )

    if link is None:
        db.add(RealMexcAccountLink(account_ref=intent.account_ref,
                                   account_id=account_id, created_at=booked_at))
    for fill, event in proposed:
        item = event.input
        db.add(RealEvent(
            id=event.id, account_id=account_id, request_id=event.request_id,
            payload_digest=_digest(item), kind=item.kind, asset=item.asset,
            quote_currency=item.quote_currency, quantity=item.quantity,
            unit_price=item.unit_price, gross_amount=item.gross_amount,
            fee=item.fee, fee_asset=item.fee_asset, occurred_at=item.occurred_at,
            recorded_at=booked_at, note=item.note,
        ))
        db.add(RealMexcFillBooking(fill_id=fill.id, account_id=account_id,
                                   event_id=event.id, booked_at=booked_at))
    return {"status": "BOOKED", "intent_id": intent_id,
            "account_id": account_id, "fill_count": len(fills),
            "event_ids": [event.id for _, event in proposed]}
