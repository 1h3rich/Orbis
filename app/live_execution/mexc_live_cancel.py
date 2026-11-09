"""Cancelación conservadora de una orden propia MEXC tras parada REAL.

El supervisor y la solicitud puntual comparten la misma reclamación durable.
El acuse deja la reserva pendiente de observación final de orden, fills y saldo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, TypedDict
import re
from uuid import UUID

from sqlalchemy import select

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.live_execution.journal import LiveJournalError, intent_request_digest, recover_spot_intent
from app.live_execution.mexc_live_order import (
    MexcCancelAcknowledgement, MexcOrderSendError, MexcSpotLiveClient,
)
from app.live_execution.mexc_order_observer import (
    MexcOrderObservationError, archive_observed_spot_order, observe_spot_intent,
)
from app.live_execution.mexc_order_lifecycle import (
    MexcLifecycleError, MexcOrderSynchronization, synchronize_spot_order,
)
from app.live_execution.mexc_uid_guard import MexcUidGuardError, assert_known_mexc_uid
from app.live_execution.policy import LiveSpotIntent, _utc
from app.portfolio.models import RealLiveOrderObservation, RealLiveSpotIntent, RealLiveStop, RealMexcAccountLink
from app.portfolio.vault import RealVault


@dataclass(frozen=True, slots=True)
class MexcStoppedCancelStep:
    status: str
    intent_id: str | None
    unresolved_intents: int
    external_status: str | None = None


@dataclass(frozen=True, slots=True)
class MexcStoppedObservationStep:
    status: str
    intent_id: str | None
    unresolved_intents: int
    external_status: str | None = None
    new_snapshot: bool = False


@dataclass(frozen=True, slots=True)
class MexcStoppedSupervisionStep:
    status: str
    intent_id: str | None
    unresolved_intents: int
    external_status: str | None = None


def _assert_stable_view_identity(view: MexcReadOnlyClient, account_ref: str,
                                 uid: str) -> None:
    """Revalida View después de leer orden y fills, antes de archivarlos."""
    try:
        same_identity = (view.account_reference() == account_ref
                         and view.account_uid() == uid)
    except Exception:
        # Los errores externos pueden contener UID o fragmentos de credenciales.
        raise MexcOrderObservationError("La identidad View MEXC cambió durante la lectura") from None
    if not same_identity:
        raise MexcOrderObservationError("La identidad View MEXC cambió durante la lectura")


def _assert_view_reference(view: MexcReadOnlyClient, account_ref: str) -> None:
    try:
        same_reference = view.account_reference() == account_ref
    except Exception:
        raise MexcOrderObservationError("Referencia View MEXC no verificable") from None
    if not same_reference:
        raise MexcOrderObservationError("La clave View no pertenece a la cuenta MEXC detenida")


def observe_stopped_spot_limit(
    vault: RealVault, view: MexcReadOnlyClient, intent_id: str,
    *, clock: Callable[[], datetime] | None = None, require_known_uid: bool = False,
) -> MexcStoppedObservationStep:
    """Consulta por ID una intención detenida; no cancela ni libera reservas.

    La ausencia/error de MEXC no se interpreta como orden inexistente. La
    clave View debe ser la misma referencia local y su UID, si se archivó,
    debe seguir coincidiendo.
    """
    try:
        intent_id = str(UUID(intent_id))
    except (TypeError, ValueError, AttributeError):
        raise MexcOrderObservationError("Identificador de intención inválido") from None
    now = clock or (lambda: datetime.now(timezone.utc))
    checked_at = _utc(now(), "now")
    with vault.session() as db:
        row = db.get(RealLiveSpotIntent, intent_id)
        if row is None or row.status != "SUBMISSION_UNKNOWN":
            raise MexcOrderObservationError("La intención MEXC no está pendiente")
        stop = db.get(RealLiveStop, row.account_ref)
        if stop is None or not stop.stopped:
            raise MexcOrderObservationError("La cuenta MEXC no está detenida")
        account_ref = row.account_ref
    _assert_view_reference(view, account_ref)
    try:
        uid = view.account_uid()
    except Exception:
        raise MexcOrderObservationError("UID View MEXC no verificable") from None
    try:
        if not assert_known_mexc_uid(vault, account_ref, uid) and require_known_uid:
            raise MexcOrderObservationError("La orden requiere un vínculo MEXC con UID verificado")
    except MexcUidGuardError as exc:
        raise MexcOrderObservationError(str(exc)) from None
    intent = recover_spot_intent(vault, intent_id)
    observed = observe_spot_intent(view, intent)
    _assert_stable_view_identity(view, account_ref, uid)
    archive = archive_observed_spot_order(vault, intent, observed, now=checked_at)
    with vault.session() as db:
        unresolved = db.scalars(select(RealLiveSpotIntent).where(
            RealLiveSpotIntent.account_ref == account_ref,
            RealLiveSpotIntent.status == "SUBMISSION_UNKNOWN",
        )).all()
    return MexcStoppedObservationStep(
        "FINAL_ARCHIVED_NEEDS_SETTLEMENT" if observed.final else "OPEN_ARCHIVED_NEEDS_RECHECK",
        intent_id, len(unresolved), observed.status, archive["new_snapshot"],
    )


def observe_next_stopped_spot_limit_once(
    vault: RealVault, view: MexcReadOnlyClient, account_ref: str,
    *, clock: Callable[[], datetime] | None = None,
) -> MexcStoppedObservationStep:
    """Una lectura View de una orden detenida aún sin final, nunca un DELETE.

    Se priorizan órdenes sin observación; una orden abierta recién consultada
    no consume cada ciclo de supervisión. Los intentos de cancelación inciertos
    tienen su propia ruta de reobservación y nunca vuelven a la cola de DELETE.
    """
    if not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise MexcOrderObservationError("Cuenta MEXC detenida inválida")
    _assert_view_reference(view, account_ref)
    now = clock or (lambda: datetime.now(timezone.utc))
    checked_at = _utc(now(), "now")
    with vault.session() as db:
        stop = db.get(RealLiveStop, account_ref)
        if stop is None or not stop.stopped:
            raise MexcOrderObservationError("La cuenta MEXC no está detenida")
        rows = db.scalars(select(RealLiveSpotIntent).where(
            RealLiveSpotIntent.account_ref == account_ref,
            RealLiveSpotIntent.status == "SUBMISSION_UNKNOWN",
        ).order_by(RealLiveSpotIntent.created_at, RealLiveSpotIntent.id)).all()
        candidate_id = None
        stale_candidate_id = None
        for row in rows:
            if row.reason not in ("SUBMISSION_OUTCOME_UNKNOWN",
                                  "POST_ATTEMPTED_OUTCOME_UNKNOWN", "POST_ACKNOWLEDGED"):
                continue
            observed = db.scalars(select(RealLiveOrderObservation).where(
                RealLiveOrderObservation.intent_id == row.id,
            ).order_by(RealLiveOrderObservation.observed_at.desc(),
                       RealLiveOrderObservation.id.desc()).limit(1)).first()
            if observed is None:
                candidate_id = row.id
                break
            if (observed.status in ("NEW", "PARTIALLY_FILLED")
                    and observed.observed_at <= checked_at
                    and (checked_at - observed.observed_at).total_seconds() > 30
                    and stale_candidate_id is None):
                stale_candidate_id = row.id
        unresolved = len(rows)
        candidate_id = candidate_id or stale_candidate_id
    if candidate_id is None:
        return MexcStoppedObservationStep("NO_OBSERVATION_DUE", None, unresolved)
    return observe_stopped_spot_limit(vault, view, candidate_id, clock=now)


def reobserve_next_stopped_spot_limit_once(
    vault: RealVault, view: MexcReadOnlyClient, account_ref: str,
    *, clock: Callable[[], datetime] | None = None,
) -> MexcStoppedObservationStep:
    """Reconsulta una cancelación incierta, sin DELETE ni cierre de reserva.

    Un acuse puede llegar antes que la ejecución de fills. Sólo la consulta
    actual de orden + fills queda archivada; contabilidad y saldo se verifican
    mediante pasos posteriores, nunca por este acuse.
    """
    if not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise MexcOrderObservationError("Cuenta MEXC detenida inválida")
    _assert_view_reference(view, account_ref)
    now = clock or (lambda: datetime.now(timezone.utc))
    checked_at = _utc(now(), "now")
    with vault.session() as db:
        stop = db.get(RealLiveStop, account_ref)
        if stop is None or not stop.stopped:
            raise MexcOrderObservationError("La cuenta MEXC no está detenida")
        rows = db.scalars(select(RealLiveSpotIntent).where(
            RealLiveSpotIntent.account_ref == account_ref,
            RealLiveSpotIntent.status == "SUBMISSION_UNKNOWN",
        ).order_by(RealLiveSpotIntent.created_at, RealLiveSpotIntent.id)).all()
        candidate: tuple[datetime, str] | None = None
        for row in rows:
            if row.reason not in ("CANCEL_ATTEMPTED_OUTCOME_UNKNOWN",
                                  "CANCEL_ACKNOWLEDGED_REOBSERVE"):
                continue
            observed = db.scalars(select(RealLiveOrderObservation).where(
                RealLiveOrderObservation.intent_id == row.id,
            ).order_by(RealLiveOrderObservation.observed_at.desc(),
                       RealLiveOrderObservation.id.desc()).limit(1)).first()
            if observed is not None and observed.status in ("NEW", "PARTIALLY_FILLED"):
                key = (observed.observed_at, row.id)
                if candidate is None or key < candidate:
                    candidate = key
        unresolved = len(rows)
        candidate_id = candidate[1] if candidate is not None else None
    if candidate_id is None:
        return MexcStoppedObservationStep("NO_CANCEL_OUTCOME_TO_REOBSERVE", None, unresolved)
    try:
        view_uid = view.account_uid()
    except Exception:
        raise MexcOrderObservationError("UID View MEXC no verificable") from None
    try:
        assert_known_mexc_uid(vault, account_ref, view_uid)
    except MexcUidGuardError as exc:
        raise MexcOrderObservationError(str(exc)) from None
    intent = recover_spot_intent(vault, candidate_id)
    observed = observe_spot_intent(view, intent)
    _assert_stable_view_identity(view, account_ref, view_uid)
    archive = archive_observed_spot_order(vault, intent, observed, now=checked_at)
    status = "FINAL_ARCHIVED_NEEDS_SETTLEMENT" if observed.final else "STILL_OPEN_RECHECK"
    return MexcStoppedObservationStep(status, candidate_id, unresolved,
                                      observed.status, archive["new_snapshot"])


def cancel_next_stopped_spot_limit_once(
    vault: RealVault, trade: MexcSpotLiveClient, view: MexcReadOnlyClient,
    account_ref: str, *, clock: Callable[[], datetime] | None = None,
) -> MexcStoppedCancelStep:
    """Un único candidato verificable por llamada, nunca un DELETE masivo.

    La selección es orientativa: la función de cancelación reclama de nuevo
    parada, identidad, intención exacta y orden propia abierta reciente bajo
    transacción. Sin evidencia reciente no hay llamada de red.
    """
    if not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise MexcOrderSendError("Cuenta MEXC de parada inválida")
    if view.account_reference() != account_ref:
        raise MexcOrderSendError("La clave View no pertenece a la cuenta MEXC detenida")
    now = clock or (lambda: datetime.now(timezone.utc))
    checked_at = _utc(now(), "now")
    with vault.session() as db:
        stop = db.get(RealLiveStop, account_ref)
        if stop is None or not stop.stopped:
            raise MexcOrderSendError("La cuenta MEXC no está detenida")
        rows = db.scalars(select(RealLiveSpotIntent).where(
            RealLiveSpotIntent.account_ref == account_ref,
            RealLiveSpotIntent.status == "SUBMISSION_UNKNOWN",
        ).order_by(RealLiveSpotIntent.created_at, RealLiveSpotIntent.id)).all()
        candidate_id = None
        for row in rows:
            if (row.reason not in (
                    "SUBMISSION_OUTCOME_UNKNOWN", "POST_ATTEMPTED_OUTCOME_UNKNOWN",
                    "POST_ACKNOWLEDGED") or row.request_json is None):
                continue
            observed = db.scalars(select(RealLiveOrderObservation).where(
                RealLiveOrderObservation.intent_id == row.id,
            ).order_by(RealLiveOrderObservation.observed_at.desc(),
                       RealLiveOrderObservation.id.desc()).limit(1)).first()
            if (observed is not None and observed.account_ref == account_ref
                    and observed.symbol == row.symbol
                    and observed.status in ("NEW", "PARTIALLY_FILLED")
                    and observed.observed_at <= checked_at
                    and (checked_at - observed.observed_at).total_seconds() <= 30):
                candidate_id = row.id
                break
        unresolved = len(rows)
    if candidate_id is None:
        return MexcStoppedCancelStep("NO_FRESH_OWN_OPEN_ORDER", None, unresolved)
    intent = recover_spot_intent(vault, candidate_id)
    ack = cancel_stopped_spot_limit_once(vault, trade, view, intent, clock=now)
    return MexcStoppedCancelStep("CANCEL_ACKNOWLEDGED_REOBSERVE", candidate_id,
                                 unresolved, ack.external_status)


def cancel_stopped_spot_limit_once(
    vault: RealVault, trade: MexcSpotLiveClient, view: MexcReadOnlyClient,
    intent: LiveSpotIntent, *, clock: Callable[[], datetime] | None = None,
    require_known_uid: bool = False,
) -> MexcCancelAcknowledgement:
    """Un DELETE dirigido, nunca cancelación masiva ni reintento automático."""
    if not isinstance(intent, LiveSpotIntent):
        raise MexcOrderSendError("La intención de cancelación no es válida")
    now = clock or (lambda: datetime.now(timezone.utc))
    if view.account_reference() != intent.account_ref:
        raise MexcOrderSendError("La clave View no pertenece a esta intención MEXC")
    try:
        view_uid = view.account_uid()
    except MexcReadOnlyError:
        raise MexcOrderSendError("No se pudo comprobar el UID de la clave View") from None
    trade.pair_view_account(view_uid)
    try:
        if not assert_known_mexc_uid(vault, intent.account_ref, view_uid) and require_known_uid:
            raise MexcOrderSendError("La orden requiere un vínculo MEXC con UID verificado")
    except MexcUidGuardError as exc:
        raise MexcOrderSendError(str(exc)) from None
    claim_at = _utc(now(), "now")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(RealLiveSpotIntent, intent.intent_id)
            stop = db.get(RealLiveStop, intent.account_ref)
            observed = db.scalars(select(RealLiveOrderObservation).where(
                RealLiveOrderObservation.intent_id == intent.intent_id,
            ).order_by(
                RealLiveOrderObservation.observed_at.desc(),
                RealLiveOrderObservation.id.desc(),
            )).first()
            if (row is None or row.status != "SUBMISSION_UNKNOWN"
                    or row.reason not in (
                        "SUBMISSION_OUTCOME_UNKNOWN", "POST_ATTEMPTED_OUTCOME_UNKNOWN",
                        "POST_ACKNOWLEDGED",
                    )
                    or row.request_digest != intent_request_digest(intent)
                    or row.account_ref != intent.account_ref
                    or row.symbol != intent.symbol or row.side != intent.side
                    or stop is None or not stop.stopped):
                raise MexcOrderSendError("La parada no autoriza cancelar esta intención")
            if (observed is None or observed.account_ref != intent.account_ref
                    or observed.symbol != intent.symbol
                    or observed.status not in ("NEW", "PARTIALLY_FILLED")
                    or observed.observed_at > claim_at
                    or (claim_at - observed.observed_at).total_seconds() > 30):
                raise MexcOrderSendError("Falta observación reciente de una orden propia abierta")
            external_order_id = observed.external_order_id
            row.reason = "CANCEL_ATTEMPTED_OUTCOME_UNKNOWN"
            row.updated_at = claim_at
            db.commit()
        except Exception:
            db.rollback()
            raise

    # El commit anterior impide repetir el DELETE incluso tras una caída.
    acknowledgement = trade.cancel_limit_order(intent)
    if acknowledgement.external_order_id != external_order_id:
        raise MexcOrderSendError("Cancelación MEXC no coincide con la orden observada")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(RealLiveSpotIntent, intent.intent_id)
            if (row is None or row.status != "SUBMISSION_UNKNOWN"
                    or row.reason != "CANCEL_ATTEMPTED_OUTCOME_UNKNOWN"
                    or row.request_digest != intent_request_digest(intent)):
                raise MexcOrderSendError("El acuse de cancelación exige reconciliación")
            row.reason = "CANCEL_ACKNOWLEDGED_REOBSERVE"
            row.updated_at = _utc(now(), "now")
            db.commit()
            return acknowledgement
        except Exception:
            db.rollback()
            raise


class MexcSelectedCancellation(TypedDict):
    intent_id: str
    request_status: Literal["NOT_NEEDED", "ACKNOWLEDGED", "OUTCOME_UNKNOWN"]
    synchronization: MexcOrderSynchronization | None


def cancel_selected_stopped_spot_order(
    vault: RealVault, view: MexcReadOnlyClient, trade: MexcSpotLiveClient,
    intent_id: str, account_id: str, *, assert_active: Callable[[], None],
    clock: Callable[[], datetime] | None = None,
) -> MexcSelectedCancellation:
    """Cancela sólo el resto abierto de la orden seleccionada y concilia por View."""
    assert_active()
    generation = vault.session_generation
    if generation is None:
        raise MexcOrderSendError("La bóveda REAL está bloqueada")
    with vault.guard_session_generation(generation):
        intent = recover_spot_intent(vault, intent_id)
        _assert_view_reference(view, intent.account_ref)
        with vault.session() as db:
            link = db.get(RealMexcAccountLink, intent.account_ref)
            stop = db.get(RealLiveStop, intent.account_ref)
            row = db.get(RealLiveSpotIntent, intent_id)
            if link is None or link.account_id != account_id:
                raise MexcOrderSendError("La orden no pertenece a esta cuenta REAL vinculada")
            if stop is None or not stop.stopped:
                raise MexcOrderSendError("Detén la cuenta MEXC antes de cancelar")
            if row is None or row.status not in ("SUBMISSION_UNKNOWN", "RESERVATION_CLOSED"):
                raise MexcOrderSendError("La orden todavía no consta como enviada")
            already_closed, reason = row.status == "RESERVATION_CLOSED", row.reason

        attempted = "CANCEL_ATTEMPTED_OUTCOME_UNKNOWN"
        acknowledged = "CANCEL_ACKNOWLEDGED_REOBSERVE"
        request_status: Literal["NOT_NEEDED", "ACKNOWLEDGED", "OUTCOME_UNKNOWN"] = "NOT_NEEDED"
        if not already_closed and reason not in (attempted, acknowledged):
            fresh = observe_stopped_spot_limit(
                vault, view, intent_id, clock=clock, require_known_uid=True)
            if fresh.status == "OPEN_ARCHIVED_NEEDS_RECHECK":
                assert_active()
                try:
                    cancel_stopped_spot_limit_once(
                        vault, trade, view, intent, clock=clock, require_known_uid=True)
                    reason = acknowledged
                except MexcOrderSendError:
                    # El supervisor también puede haber reclamado esta misma orden.
                    with vault.session() as db:
                        row = db.get(RealLiveSpotIntent, intent_id)
                        if row is None or (row.reason not in (attempted, acknowledged)
                                           and row.status != "RESERVATION_CLOSED"):
                            raise
                        reason = row.reason
        if reason == attempted:
            request_status = "OUTCOME_UNKNOWN"
        elif reason == acknowledged:
            request_status = "ACKNOWLEDGED"
        try:
            synchronized = synchronize_spot_order(
                vault, view, intent_id, account_id, clock=clock, assert_active=assert_active)
        except (MexcLifecycleError, MexcOrderObservationError, LiveJournalError):
            if request_status == "NOT_NEEDED":
                raise
            synchronized = None
        return {"intent_id": intent_id, "request_status": request_status, "synchronization": synchronized}


def supervise_stopped_spot_account_once(
    vault: RealVault, trade: MexcSpotLiveClient, view: MexcReadOnlyClient,
    account_ref: str, *, clock: Callable[[], datetime] | None = None,
) -> MexcStoppedSupervisionStep:
    """Un ciclo interno: reobservar resultado incierto o cancelar una orden propia.

    Se envía como máximo un DELETE por ciclo, siempre tras una observación
    View reciente de la misma orden. Una cancelación ya intentada sólo se
    reobserva; jamás se repite. No asienta fills ni cierra reservas.
    """
    outcome = reobserve_next_stopped_spot_limit_once(
        vault, view, account_ref, clock=clock)
    if outcome.intent_id is not None:
        return MexcStoppedSupervisionStep(
            outcome.status, outcome.intent_id, outcome.unresolved_intents,
            outcome.external_status)
    observation = observe_next_stopped_spot_limit_once(
        vault, view, account_ref, clock=clock)
    if observation.intent_id is not None and observation.status == "FINAL_ARCHIVED_NEEDS_SETTLEMENT":
        return MexcStoppedSupervisionStep(
            observation.status, observation.intent_id, observation.unresolved_intents,
            observation.external_status)
    cancellation = cancel_next_stopped_spot_limit_once(
        vault, trade, view, account_ref, clock=clock)
    if cancellation.intent_id is not None:
        return MexcStoppedSupervisionStep(
            cancellation.status, cancellation.intent_id,
            cancellation.unresolved_intents, cancellation.external_status)
    return MexcStoppedSupervisionStep(
        "NO_ACTIONABLE_OWN_ORDER", None, cancellation.unresolved_intents)
