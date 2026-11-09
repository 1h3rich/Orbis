"""Única trayectoria interna de envío MEXC Spot LIMIT real.

La reutilizan los coordinadores manual y de bot. Un intento de POST se marca
antes de abrir red y jamás se repite, incluso si no se recibe respuesta.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.live_execution.journal import (
    LiveJournalError, begin_spot_submission, claim_spot_order_post,
    intent_request_digest, reserve_spot_intent,
    signal_source_request_digest, signal_source_request_json,
)
from app.live_execution.mexc_instrument import MexcSpotRules, preflight_limit_order
from app.live_execution.mexc_live_order import MexcOrderAcknowledgement, MexcOrderSendError, MexcSpotLiveClient
from app.live_execution.mexc_uid_guard import MexcUidGuardError, bind_uid_for_spot_submission
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState, _utc
from app.live_execution.spot_paper_signal import (SpotPaperSignalError,
                                                  verified_archived_paper_buy_source)
from app.portfolio.models import RealLiveSpotIntent, RealLiveStop
from app.portfolio.vault import RealVault


@dataclass(frozen=True, slots=True)
class MexcLiveInputs:
    """Nuevas observaciones independientes para la puerta final de envío."""

    state: LiveSpotState
    rules: MexcSpotRules
    last_price: Decimal
    last_price_at: datetime


def submit_spot_limit_once(
    vault: RealVault, trade: MexcSpotLiveClient, view: MexcReadOnlyClient,
    policy: LiveSpotPolicy, intent: LiveSpotIntent, state: LiveSpotState,
    rules: MexcSpotRules, *, last_price: Decimal, last_price_at: datetime,
    refresh_inputs: Callable[[], MexcLiveInputs],
    clock: Callable[[], datetime] | None = None,
    paper: SpotStrategyPaper | None = None,
    assert_authorized: Callable[[], None] | None = None,
    assert_authorized_in_transaction: Callable | None = None,
) -> MexcOrderAcknowledgement:
    """Reserva, prueba y envía como máximo una vez; nunca ofrece reintento.

    Las observaciones iniciales y la lectura nueva deberán proceder de
    fuentes internas verificadas. La autorización del coordinador se repite
    antes de la primera marca de envío y dentro del cerrojo del POST.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    authorized = assert_authorized or (lambda: None)
    authorized()
    first_at = _utc(now(), "now")

    def checked_clock(previous: datetime) -> datetime:
        current = _utc(now(), "now")
        if current < previous:
            raise MexcOrderSendError("Reloj local retrocedió durante el envío MEXC")
        return current

    preflight = preflight_limit_order(
        policy, intent, rules, last_price=last_price,
        last_price_at=last_price_at, now=first_at,
    )
    if not preflight.allowed:
        raise MexcOrderSendError(f"Preflight MEXC rechazado: {preflight.reason}")
    with vault.session() as db:
        prior = db.get(RealLiveSpotIntent, intent.intent_id)
        prior_source = bool(prior and (prior.signal_source_json is not None
                                       or prior.signal_source_digest is not None))
    if prior_source and paper is None:
        raise MexcOrderSendError("La señal PAPER archivada exige revalidación antes del envío")
    if paper is not None and not prior_source:
        raise MexcOrderSendError("La intención no tiene origen PAPER archivado")
    if view.account_reference() != intent.account_ref:
        raise MexcOrderSendError("La clave View no corresponde a la intención MEXC")
    try:
        view_uid = view.account_uid()
    except MexcReadOnlyError:
        raise MexcOrderSendError("No se pudo comprobar el UID de la clave View") from None
    trade.pair_view_account(view_uid)
    try:
        bind_uid_for_spot_submission(vault, intent.account_ref, view_uid, now=first_at)
    except MexcUidGuardError as exc:
        raise MexcOrderSendError(str(exc)) from None
    reserved_at = checked_clock(first_at)
    recorded = reserve_spot_intent(vault, intent, state, now=reserved_at)
    if recorded.status != "RESERVED":
        raise MexcOrderSendError(f"Reserva REAL no autorizada: {recorded.reason}")
    with vault.session() as db:
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        source_archived = bool(row and (row.signal_source_json is not None
                                        or row.signal_source_digest is not None))
    if source_archived and paper is None:
        raise MexcOrderSendError("La señal PAPER archivada exige revalidación antes del envío")
    if paper is not None and not source_archived:
        raise MexcOrderSendError("La intención no tiene origen PAPER archivado")
    guard = (verified_archived_paper_buy_source(
        vault, paper, policy, intent, now=checked_clock(reserved_at))
             if source_archived else nullcontext(None))
    try:
        with guard as source:
            trade.test_reserved_limit(intent, recorded, preflight)
            try:
                fresh = refresh_inputs()
                if not isinstance(fresh, MexcLiveInputs):
                    raise ValueError("invalid refresh result")
                if (not isinstance(fresh.state, LiveSpotState)
                        or not isinstance(fresh.rules, MexcSpotRules)
                        or _utc(fresh.state.observed_at, "observed_at") <= state.observed_at
                        or _utc(fresh.state.quote_at, "quote_at") <= state.quote_at
                        or _utc(fresh.rules.observed_at, "observed_at") <= rules.observed_at
                        or _utc(fresh.last_price_at, "last_price_at") <= _utc(last_price_at, "last_price_at")):
                    raise ValueError("observations have not advanced")
            except Exception:
                # El proveedor puede incluir datos privados en su excepción.
                raise MexcOrderSendError("No hay observaciones nuevas verificables; envío bloqueado") from None
            second_at = checked_clock(reserved_at)
            second_preflight = preflight_limit_order(
                policy, intent, fresh.rules, last_price=fresh.last_price,
                last_price_at=fresh.last_price_at, now=second_at,
            )
            if not second_preflight.allowed:
                raise MexcOrderSendError(f"Preflight MEXC ya no es válido: {second_preflight.reason}")
            try:
                # La prueba de orden y las lecturas nuevas consumen tiempo. Una
                # identidad/permisos válidos al principio no justifican abrir
                # el punto de no-reintento si cambiaron durante ese intervalo.
                if (view.account_reference() != intent.account_ref
                        or view.account_uid() != view_uid):
                    raise ValueError("View identity changed")
                trade.pair_view_account(view_uid)
            except Exception:
                # Los errores de proveedores pueden contener datos privados.
                # La reserva ya existe, pero aún no hay POST ni marca incierta.
                raise MexcOrderSendError(
                    "Identidad o permisos MEXC no verificables antes del envío; reserva retenida"
                ) from None
            authorized()
            begin_spot_submission(vault, intent, fresh.state, now=second_at)
            claimed_at = checked_clock(second_at)
            claim_spot_order_post(vault, intent, now=claimed_at)
            with vault.session() as db:
                try:
                    # La parada adquiere el mismo bloqueo de escritura. Si se confirma
                    # antes de éste, no hay POST. Si espera aquí, sólo confirma después
                    # de terminar el intento de red de un máximo de 5 s.
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    row = db.get(RealLiveSpotIntent, intent.intent_id)
                    stop = db.get(RealLiveStop, intent.account_ref)
                    if (row is None or row.status != "SUBMISSION_UNKNOWN"
                            or row.reason != "POST_ATTEMPTED_OUTCOME_UNKNOWN"
                            or row.request_digest != intent_request_digest(intent)
                            or source is not None and (
                                row.signal_source_json != signal_source_request_json(source)
                                or row.signal_source_digest != signal_source_request_digest(source))
                            or stop is not None and stop.stopped):
                        raise MexcOrderSendError("Parada o estado cambiado antes del POST; resultado incierto")
                    checked_clock(claimed_at)
                    if assert_authorized_in_transaction is not None:
                        assert_authorized_in_transaction(db)
                    else:
                        authorized()
                    acknowledgement = trade.place_limit_order(intent)
                    row.reason = "POST_ACKNOWLEDGED"
                    # Tras un POST aceptado no se pierde el acuse por un ajuste
                    # posterior del reloj; la cronología del journal no retrocede.
                    row.updated_at = max(claimed_at, _utc(now(), "now"))
                    db.commit()
                    return acknowledgement
                except Exception:
                    db.rollback()
                    raise
    except SpotPaperSignalError as exc:
        raise MexcOrderSendError(f"Señal PAPER no verificable: {exc}") from None
