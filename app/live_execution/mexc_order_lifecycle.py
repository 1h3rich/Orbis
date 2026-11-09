"""Seguimiento de una orden propia: observar, asentar y cerrar su reserva.

Sólo consume la conexión View. Los pasos son idempotentes y recuperables:
si MEXC falla después del asiento, éste permanece y el próximo seguimiento
revalida los mismos fills sin volver a comprar, vender ni duplicar asientos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal, NotRequired, TypedDict
from uuid import UUID

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.journal import LiveJournalError, recover_spot_intent
from app.live_execution.mexc_booking import book_observed_spot_order
from app.live_execution.mexc_order_observer import (
    archive_observed_spot_order, observe_spot_intent,
)
from app.live_execution.mexc_settlement import (
    MexcSettlementError, close_final_spot_reservation,
)
from app.live_execution.mexc_uid_guard import assert_known_mexc_uid, assert_rotated_mexc_uid
from app.live_execution.policy import _utc
from app.live_execution.reconciliation_view import list_spot_execution_intents
from app.portfolio.domain import PortfolioError
from app.portfolio.models import RealAccount, RealLiveSpotIntent, RealMexcAccountLink
from app.portfolio.repository import verify_account_event_digests
from app.portfolio.vault import RealVault


class MexcLifecycleError(RuntimeError):
    """El seguimiento no puede atribuirse a una cuenta y orden verificadas."""


class MexcOrderSynchronization(TypedDict):
    intent_id: str
    account_id: str
    stage: Literal["ORDER_OPEN", "BOOK_REVIEW_REQUIRED", "SETTLEMENT_REVIEW_REQUIRED",
                   "COMPLETED", "ALREADY_COMPLETED"]
    external_status: str
    fill_count: int
    booked: bool
    reservation_closed: bool
    book_inventory_matched: bool | None
    replayed_without_exchange: bool
    orders_enabled: bool
    new_snapshot: NotRequired[bool]
    balance_observed_at: NotRequired[str | None]
    detail: NotRequired[str]


def synchronize_spot_order(
    vault: RealVault, view: MexcReadOnlyClient, intent_id: str, account_id: str, *,
    clock: Callable[[], datetime] | None = None,
    assert_active: Callable[[], None] | None = None,
    renewed_view_ref: str | None = None,
) -> MexcOrderSynchronization:
    """Sincroniza una orden enviada, también si su POST terminó incierto.

    Una orden abierta sólo se observa. Una final registra todos sus fills y
    reconfirma orden, fills y saldo antes de liberar la reserva. Los errores
    de los últimos pasos se devuelven como revisión pendiente, conservando
    los asientos de hechos ya verificados. No habilita nuevas órdenes.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    active = assert_active or (lambda: None)
    active()
    try:
        account_id, intent_id = str(UUID(account_id)), str(UUID(intent_id))
        intent = recover_spot_intent(vault, intent_id)
    except (ValueError, TypeError, AttributeError, LiveJournalError):
        raise MexcLifecycleError("Intención MEXC no verificable") from None
    generation = vault.session_generation
    if generation is None:
        raise MexcLifecycleError("La bóveda REAL está bloqueada")
    with vault.guard_session_generation(generation):
        with vault.session() as db:
            db.connection().exec_driver_sql("BEGIN")
            row = db.get(RealLiveSpotIntent, intent_id)
            link = db.get(RealMexcAccountLink, intent.account_ref)
            if (row is None or db.get(RealAccount, account_id) is None
                    or link is None or link.account_id != account_id):
                raise MexcLifecycleError("La orden no pertenece a esta cuenta REAL vinculada")
            if row.status not in ("SUBMISSION_UNKNOWN", "RESERVATION_CLOSED"):
                raise MexcLifecycleError("La intención todavía no consta como enviada")
            try:
                verify_account_event_digests(db, account_id)
            except PortfolioError:
                raise MexcLifecycleError("El libro REAL no es verificable") from None
            already_closed = row.status == "RESERVATION_CLOSED"
        if already_closed:
            item = next((item for item in list_spot_execution_intents(vault)
                         if item["intent_id"] == intent_id), None)
            if item is None or item["stage"] != "FINAL_RESERVATION_CLOSED":
                raise MexcLifecycleError("El cierre archivado no es verificable")
            return {"intent_id": intent_id, "account_id": account_id,
                    "stage": "ALREADY_COMPLETED", "external_status": item["external_status"],
                    "fill_count": item["reconciliation"]["fill_count"],
                    "booked": item["booked_to_portfolio"], "reservation_closed": True,
                    "book_inventory_matched": None, "replayed_without_exchange": True,
                    "orders_enabled": False}

        def verify_view() -> None:
            try:
                if view.account_reference() != (renewed_view_ref or intent.account_ref):
                    raise ValueError("Unexpected credential reference")
                uid = view.account_uid()
                if renewed_view_ref is not None:
                    with vault.session() as db:
                        db.connection().exec_driver_sql("BEGIN")
                        assert_rotated_mexc_uid(db, intent.account_ref, renewed_view_ref, uid)
                elif not assert_known_mexc_uid(vault, intent.account_ref, uid):
                    raise ValueError("Unknown View identity")
            except Exception:
                message = ("La clave View renovada no corresponde inequívocamente a esta cuenta MEXC"
                           if renewed_view_ref is not None else "La identidad MEXC View no coincide con esta orden")
                raise MexcLifecycleError(message) from None

        verify_view()
        observed = observe_spot_intent(view, intent)
        verify_view()
        active()
        archived = archive_observed_spot_order(
            vault, intent, observed, now=_utc(now(), "now"),
        )
        result: MexcOrderSynchronization = {"intent_id": intent_id, "account_id": account_id,
                  "stage": "ORDER_OPEN",
                  "external_status": observed.status, "fill_count": len(observed.fills),
                  "new_snapshot": archived["new_snapshot"],
                  "booked": False, "reservation_closed": False,
                  "book_inventory_matched": False, "replayed_without_exchange": False,
                  "orders_enabled": False}
        if not observed.final:
            return {**result, "stage": "ORDER_OPEN"}
        active()
        if observed.fills:
            try:
                book_observed_spot_order(vault, intent_id, account_id, now=_utc(now(), "now"))
            except PortfolioError as exc:
                return {**result, "stage": "BOOK_REVIEW_REQUIRED", "detail": str(exc)}
            result["booked"] = True
        active()
        verify_view()
        try:
            # Usa el saldo de la misma reconfirmación bajo la transacción que
            # cierra la reserva, evitando una comprobación de saldo desconectada.
            settled = close_final_spot_reservation(
                vault, view, intent.account_ref, intent_id,
                matching_account_id=account_id, clock=now, renewed_view_ref=renewed_view_ref,
            )
        except MexcSettlementError as exc:
            return {**result, "stage": "SETTLEMENT_REVIEW_REQUIRED", "detail": str(exc)}
        return {**result, "stage": "COMPLETED", "reservation_closed": True,
                "book_inventory_matched": settled.get("book_inventory_matched"),
                "balance_observed_at": settled.get("balance_observed_at")}
