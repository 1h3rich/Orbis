"""Contrasta una ventana completa de fills Spot con el diario local.

Un resultado consistente sólo cubre un par y el intervalo consultado. No
demuestra coste histórico, P/L diario ni ausencia de actividad en otros pares.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.mexc_uid_guard import MexcUidGuardError, assert_known_mexc_uid
from app.live_execution.policy import LiveSpotPolicy, _utc
from app.portfolio.domain import PortfolioError
from app.portfolio.mexc_observations import _validate, observed_fill_digest_matches
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent,
    RealMexcAccountLink, RealMexcFillBooking,
)
from app.portfolio.vault import RealVault


@dataclass(frozen=True, slots=True)
class MexcTradeWindowAudit:
    consistent_for_window: bool
    reason: str
    start_ms: int | None
    end_ms: int | None
    fill_count: int


def audit_spot_trade_window(vault: RealVault | None, policy: LiveSpotPolicy,
                            snapshot: dict, *, now: datetime,
                            db: Session | None = None) -> MexcTradeWindowAudit:
    """Comprueba cobertura, identidad, contenido, orden propia y asiento.

    Nunca construye ``LiveSpotState`` ni transforma un fill observado en P/L.
    Un coordinador con transacción propia puede pasarla: no se abre, confirma
    ni revierte otra sesión SQLCipher dentro de su commit de evidencia.
    """
    now = _utc(now, "now")
    if vault is None and db is None:
        raise ValueError("El auditor necesita bóveda o transacción del coordinador")

    def deny(reason: str, start: int | None = None, end: int | None = None,
             count: int = 0) -> MexcTradeWindowAudit:
        return MexcTradeWindowAudit(False, reason, start, end, count)

    if not isinstance(policy, LiveSpotPolicy):
        return deny("POLICY_INVALID")
    try:
        symbol, observed_at, _limit, rows, start, end, _pages, coverage = _validate(
            policy.account_ref, snapshot,
        )
    except (PortfolioError, TypeError, ValueError):
        return deny("TRADE_WINDOW_INVALID")
    if symbol != policy.symbol or coverage != "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT":
        return deny("TRADE_WINDOW_INCOMPLETE")
    if start is None or end is None:
        return deny("TRADE_WINDOW_INVALID")
    now_ms = int(now.timestamp() * 1000)
    if (observed_at > now or (now - observed_at).total_seconds() > policy.max_data_age_seconds
            or end > now_ms or (now_ms - end) > policy.max_data_age_seconds * 1000
            or observed_at.timestamp() * 1000 < end):
        return deny("TRADE_WINDOW_STALE", start, end, len(rows))
    owned_session = db is None
    with (vault.session() if owned_session else nullcontext(db)) as db:
        # En SQLite legacy varios SELECT no comparten necesariamente snapshot
        # DBAPI. Fijarlo antes del primer read evita mezclar fills, órdenes y
        # asientos de revisiones distintas si otro proceso escribe entre ellos.
        if owned_session:
            db.connection().exec_driver_sql("BEGIN")
        archived = db.scalars(select(MexcObservedFill).where(
            MexcObservedFill.account_ref == policy.account_ref,
            MexcObservedFill.symbol == policy.symbol,
            MexcObservedFill.time_ms >= start,
            MexcObservedFill.time_ms <= end,
        )).all()
        by_trade = {fill.trade_id: fill for fill in archived}
        if len(by_trade) != len(archived):
            return deny("ARCHIVE_DUPLICATE", start, end, len(rows))
        if any(not observed_fill_digest_matches(fill) for fill in archived):
            return deny("ARCHIVE_INTEGRITY_FAILED", start, end, len(rows))
        seen = {row["trade_id"] for row in rows}
        if any(row["trade_id"] not in by_trade for row in rows):
            return deny("FILL_NOT_ARCHIVED", start, end, len(rows))
        if any(fill.trade_id not in seen for fill in archived):
            return deny("ARCHIVED_FILL_MISSING", start, end, len(rows))
        observations = db.scalars(select(RealLiveOrderObservation).where(
            RealLiveOrderObservation.account_ref == policy.account_ref,
            RealLiveOrderObservation.symbol == policy.symbol,
        )).all()
        by_order: dict[str, set[str]] = {}
        for observed in observations:
            by_order.setdefault(observed.external_order_id, set()).add(observed.intent_id)
        intent_ids = {next(iter(ids)) for ids in by_order.values() if len(ids) == 1}
        intents = {item.id: item for item in db.scalars(select(RealLiveSpotIntent).where(
            RealLiveSpotIntent.id.in_(intent_ids),
        )).all()}
        bookings = db.scalars(select(RealMexcFillBooking).where(
            RealMexcFillBooking.fill_id.in_([fill.id for fill in archived]),
        )).all()
        booked = {row.fill_id: row for row in bookings}
        link = db.get(RealMexcAccountLink, policy.account_ref)
        events = {row.id: row for row in db.scalars(select(RealEvent).where(
            RealEvent.id.in_([row.event_id for row in bookings]),
        )).all()}
        for row in rows:
            fill = by_trade[row["trade_id"]]
            if (fill.payload_digest != row["payload_digest"]
                    or fill.order_id != row["order_id"] or fill.side != row["side"]
                    or fill.time_ms != row["time_ms"]):
                return deny("FILL_CONTENT_CHANGED", start, end, len(rows))
            ids = by_order.get(fill.order_id, set())
            if len(ids) != 1:
                return deny("FILL_ORDER_NOT_OWNED", start, end, len(rows))
            intent = intents.get(next(iter(ids)))
            if (intent is None or intent.account_ref != policy.account_ref
                    or intent.symbol != policy.symbol or intent.side != fill.side
                    or intent.status not in ("SUBMISSION_UNKNOWN", "RESERVATION_CLOSED")):
                return deny("FILL_ORDER_NOT_OWNED", start, end, len(rows))
            booking = booked.get(fill.id)
            if booking is None:
                return deny("FILL_BOOKING_PENDING", start, end, len(rows))
            event = events.get(booking.event_id)
            if not booked_event_matches(fill, intent, booking, event, link):
                return deny("FILL_BOOKING_MISMATCH", start, end, len(rows))
    return MexcTradeWindowAudit(True, "WINDOW_ARCHIVE_AND_BOOKING_MATCH", start, end, len(rows))


def observe_and_audit_spot_trade_window(vault: RealVault, view: MexcReadOnlyClient,
                                        policy: LiveSpotPolicy, *, days: int,
                                        now: datetime | None = None) -> MexcTradeWindowAudit:
    """Consulta View sin mutar la bóveda; errores de red nunca son ausencia de fills."""
    if not isinstance(policy, LiveSpotPolicy):
        return MexcTradeWindowAudit(False, "POLICY_INVALID", None, None, 0)
    try:
        account_ref = view.account_reference()
    except Exception:
        return MexcTradeWindowAudit(False, "VIEW_UNAVAILABLE", None, None, 0)
    if account_ref != policy.account_ref:
        return MexcTradeWindowAudit(False, "VIEW_ACCOUNT_MISMATCH", None, None, 0)
    try:
        uid_before = view.account_uid()
    except Exception:
        return MexcTradeWindowAudit(False, "VIEW_UNAVAILABLE", None, None, 0)
    try:
        known = assert_known_mexc_uid(vault, policy.account_ref, uid_before)
    except MexcUidGuardError:
        known = False
    if not known:
        return MexcTradeWindowAudit(False, "VIEW_UID_UNVERIFIED", None, None, 0)
    try:
        snapshot = view.trades_window(policy.symbol, days=days)
        uid_after = view.account_uid()
    except Exception:
        return MexcTradeWindowAudit(False, "VIEW_UNAVAILABLE", None, None, 0)
    if uid_after != uid_before:
        return MexcTradeWindowAudit(False, "VIEW_UID_CHANGED", None, None, 0)
    try:
        known = assert_known_mexc_uid(vault, policy.account_ref, uid_after)
    except MexcUidGuardError:
        known = False
    if not known:
        return MexcTradeWindowAudit(False, "VIEW_UID_UNVERIFIED", None, None, 0)
    return audit_spot_trade_window(vault, policy, snapshot,
                                   now=now or datetime.now(timezone.utc))
