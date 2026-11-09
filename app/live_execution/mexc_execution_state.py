"""Estado REAL para una sesión MEXC Spot de un par y un saldo de apertura.

La paridad archivada fija un punto de partida revisado. Desde ese punto se
exigen fills propios asentados, inventario concordante y ausencia de traslados
o reservas pendientes. No pretende reconstruir la historia previa de MEXC ni
proteger frente a otra aplicación operando simultáneamente en la cuenta.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.mexc_live_observation import fetch_live_evidence
from app.live_execution.mexc_live_submit import MexcLiveInputs
from app.live_execution.mexc_trade_window_audit import audit_spot_trade_window
from app.live_execution.mexc_uid_guard import assert_known_mexc_uid
from app.live_execution.policy import LiveSpotPolicy, LiveSpotState, _utc
from app.portfolio.daily_book_risk import calculate_daily_book_risk
from app.portfolio.mexc_observations import _validate as validate_trades, observed_fill_digest_matches
from app.portfolio.mexc_parity_archive import read_observation
from app.portfolio.mexc_transfers import _validate as validate_transfers
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent, RealLiveSpotPolicy, RealLiveStop,
    RealMexcAccountLink, RealMexcFillBooking, RealMexcParityObservation,
)
from app.portfolio.reconciliation import compare_mexc_spot_inventory
from app.portfolio.repository import (
    account_book_revision, account_snapshot, list_events, verify_account_event_digests,
)
from app.portfolio.vault import RealVault


class MexcExecutionStateError(RuntimeError):
    """Una sesión no dispone de evidencia suficiente para otra orden."""


def _anchor(db: Session, policy: LiveSpotPolicy, now: datetime
            ) -> tuple[RealMexcParityObservation, RealMexcAccountLink, datetime]:
    if not policy.reconciliation_anchor_id:
        raise MexcExecutionStateError("Falta el saldo de apertura de la operativa REAL")
    anchor = db.get(RealMexcParityObservation, policy.reconciliation_anchor_id)
    if anchor is None or anchor.account_ref != policy.account_ref:
        raise MexcExecutionStateError("La apertura no corresponde a esta cuenta MEXC")
    from app.live_execution.mexc_continuity_review import opening_boundary
    boundary = opening_boundary(db,anchor)
    if (boundary > now or (now - boundary).total_seconds() > 86400
            or policy.expires_at > boundary + timedelta(days=1)):
        raise MexcExecutionStateError("La sesión REAL requiere una nueva apertura diaria")
    link = db.get(RealMexcAccountLink, policy.account_ref)
    if link is None or link.account_id != anchor.account_id:
        raise MexcExecutionStateError("El vínculo de la cuenta MEXC cambió")
    stop = db.get(RealLiveStop, policy.account_ref)
    if stop is not None and not stop.stopped:
        stored = db.get(RealLiveSpotPolicy, policy.policy_id)
        resumed_policy = (stored is not None and stored.created_at == stop.updated_at
                          and policy.resumed_after_stop_at is not None)
        if ((stored is not None and stored.created_at < stop.updated_at)
                or boundary < stop.updated_at and not resumed_policy):
            raise MexcExecutionStateError("Esta política es anterior a la reapertura; habilita una operativa nueva")
    verify_account_event_digests(db, anchor.account_id)
    if account_book_revision(db, anchor.account_id, recorded_through=boundary) != anchor.book_revision:
        raise MexcExecutionStateError("El libro anterior a la apertura cambió")
    return anchor, link, boundary


def _new_events_are_booked(db: Session, policy: LiveSpotPolicy,
                           anchor: RealMexcParityObservation, link: RealMexcAccountLink, *,
                           recorded_through: datetime | None = None,opening_at:datetime|None=None) -> None:
    anchor_at = opening_at if opening_at is not None else anchor.checked_at
    query = select(RealEvent).where(
        RealEvent.account_id == anchor.account_id,
        RealEvent.recorded_at > anchor_at)
    if recorded_through is not None:
        query = query.where(RealEvent.recorded_at <= recorded_through)
    events = db.scalars(query).all()
    for event in events:
        booking = db.scalar(select(RealMexcFillBooking).where(RealMexcFillBooking.event_id == event.id))
        fill = db.get(MexcObservedFill, booking.fill_id) if booking is not None else None
        intent_ids = (set(db.scalars(select(RealLiveOrderObservation.intent_id).where(
            RealLiveOrderObservation.account_ref == policy.account_ref,
            RealLiveOrderObservation.symbol == policy.symbol,
            RealLiveOrderObservation.external_order_id == fill.order_id)).all())
            if fill is not None else set())
        intent = db.get(RealLiveSpotIntent, next(iter(intent_ids))) if len(intent_ids) == 1 else None
        if (fill is None or fill.account_ref != policy.account_ref
                or fill.symbol != policy.symbol or not observed_fill_digest_matches(fill)
                or fill.time_ms < int(anchor_at.timestamp() * 1000) or intent is None
                or intent.account_ref != policy.account_ref or intent.side != fill.side
                or intent.status != "RESERVATION_CLOSED"
                or not booked_event_matches(fill, intent, booking, event, link)):
            raise MexcExecutionStateError("Hay movimientos nuevos sin conciliar con una orden propia")


def fetch_spot_execution_inputs(
    vault: RealVault, view: MexcReadOnlyClient, policy: LiveSpotPolicy, *,
    current_intent_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable | None = None,
    rules_fetch: Callable | None = None,
    review_stopped: bool = False,
    audit_receipt: dict | None = None,
) -> MexcLiveInputs:
    """Relee cuenta y actividad; sólo admite un par, sin otras órdenes abiertas.

    `current_intent_id` excluye únicamente la reserva aún no enviada de la
    misma petición durante su segunda comprobación. Nunca excluye una orden
    enviada o incierta. Las reservas locales se añaden en el journal.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    if audit_receipt is not None and (type(audit_receipt) is not dict or audit_receipt):
        raise MexcExecutionStateError("El recibo de revisión debe estar vacío")
    started = _utc(now(), "now")
    if not isinstance(policy, LiveSpotPolicy) or not policy.enabled or started >= policy.expires_at:
        raise MexcExecutionStateError("La política de operativa REAL no está activa o ha caducado")
    try:
        with vault.session() as db:
            db.connection().exec_driver_sql("BEGIN")
            anchor, link, anchor_at = _anchor(db, policy, started)
            anchor_id, account_id = anchor.id, anchor.account_id
            from app.portfolio.repository import account_book_identity
            revision, method = account_book_identity(db, account_id)
            _, opening_method = account_book_identity(db, account_id, recorded_through=anchor_at)
            if method != opening_method:
                raise MexcExecutionStateError('La conciliación contable cambió; revisa la apertura MEXC')
            _new_events_are_booked(db, policy, anchor, link,opening_at=anchor_at)
            pending = db.scalars(select(RealLiveSpotIntent).where(
                RealLiveSpotIntent.account_ref == policy.account_ref,
                RealLiveSpotIntent.status.in_(("RESERVED", "SUBMISSION_UNKNOWN")))).all()
            if any(row.id != current_intent_id or row.status != "RESERVED"
                   or row.policy_id != policy.policy_id for row in pending):
                raise MexcExecutionStateError("Sincroniza la orden pendiente antes de enviar otra")
            stop = db.get(RealLiveStop, policy.account_ref)
            stopped = bool(stop is not None and stop.stopped)
            stop_at = stop.updated_at if stop is not None else None
            if stopped and not review_stopped:
                raise MexcExecutionStateError("La cuenta MEXC está detenida")
        options = {"clock": now}
        if quote_get is not None:
            options["quote_get"] = quote_get
        if rules_fetch is not None:
            options["rules_fetch"] = rules_fetch
        evidence = fetch_live_evidence(view, policy, **options)
        if not assert_known_mexc_uid(vault, policy.account_ref, evidence.view_uid):
            raise MexcExecutionStateError("La identidad MEXC no está verificada")
        if any(balance.locked > 0 for balance in evidence.balances):
            raise MexcExecutionStateError("La sesión requiere fondos libres en toda la cuenta MEXC")
        survey = view.open_orders_unfiltered()
        trades = view.trades_window(policy.symbol, days=1)
        transfers = view.transfers_recent(days=1)
        if ("MX" not in (policy.base_asset, policy.quote_asset)
                and any(balance.asset == "MX" and balance.total > 0 for balance in evidence.balances)):
            try:
                mx_deduct_enabled = view.mx_deduct_enabled()
            except MexcReadOnlyError:
                raise MexcExecutionStateError("MEXC no confirmó el ajuste de comisiones en MX") from None
            if mx_deduct_enabled is True:
                raise MexcExecutionStateError("Desactiva el pago de comisiones con MX para operar este par")
            if mx_deduct_enabled is not False:
                raise MexcExecutionStateError("MEXC no confirmó el ajuste de comisiones en MX")
        if (view.account_reference() != policy.account_ref
                or view.account_uid() != evidence.view_uid):
            raise MexcExecutionStateError("La identidad MEXC cambió durante la lectura")
        checked_at = _utc(now(), "now")
        survey_at = _utc(datetime.fromisoformat(survey["observed_at"]), "observed_at")
        if (survey.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
                or survey.get("accounting") != "OBSERVED_NOT_IMPORTED"
                or survey.get("coverage") != "UNFILTERED_COVERAGE_UNPROVEN"
                or survey.get("symbol") is not None or survey.get("orders") != []
                or evidence.open_orders):
            raise MexcExecutionStateError("MEXC tiene órdenes abiertas o no verificables")
        symbol, trades_at, _, trade_rows, trade_start, trade_end, trade_pages, coverage = validate_trades(policy.account_ref, trades)
        transfer_at, transfer_start, transfer_end, deposits, withdrawals, _ = validate_transfers(policy.account_ref, transfers)
        anchor_ms = int(anchor_at.timestamp() * 1000)
        if (symbol != policy.symbol or coverage != "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT"
                or trade_start is None or trade_end is None
                or trade_start > anchor_ms or transfer_start > anchor_ms
                or trade_end < anchor_ms or transfer_end < anchor_ms):
            raise MexcExecutionStateError("La actividad MEXC no cubre la apertura de la sesión")
        for stamp in (survey_at, trades_at, transfer_at, evidence.account_at,
                      evidence.quote_at, evidence.rules.observed_at):
            if stamp > checked_at or (checked_at - stamp).total_seconds() > policy.max_data_age_seconds:
                raise MexcExecutionStateError("La evidencia MEXC caducó durante la lectura")
        checked_ms = int(checked_at.timestamp() * 1000)
        if any(end > checked_ms or checked_ms - end > policy.max_data_age_seconds * 1000
               for end in (trade_end, transfer_end)):
            raise MexcExecutionStateError("La actividad MEXC no llega hasta el estado actual")
        if any(row["time_ms"] >= anchor_ms or row["status"] != final
               for rows, final in ((deposits, 5), (withdrawals, 7)) for row in rows):
            raise MexcExecutionStateError("Hay ingresos o retiradas que requieren revisar la apertura")
        # Una consulta completa permite derivar su subintervalo exacto. No
        # convertimos una página parcial ni una ausencia de red en cobertura.
        scoped_trades = {**trades, "start_ms": anchor_ms,
                         "trades": [row for row in trades["trades"] if row["time_ms"] >= anchor_ms]}
        audit = audit_spot_trade_window(vault, policy, scoped_trades, now=checked_at)
        if not audit.consistent_for_window:
            raise MexcExecutionStateError("Ejecuciones MEXC pendientes: " + audit.reason)
        with vault.session() as db:
            db.connection().exec_driver_sql("BEGIN")
            anchor, link, checked_boundary = _anchor(db, policy, checked_at)
            current_stop = db.get(RealLiveStop, policy.account_ref)
            if ((bool(current_stop is not None and current_stop.stopped),
                 current_stop.updated_at if current_stop is not None else None) != (stopped, stop_at)):
                raise MexcExecutionStateError("La parada de la cuenta cambió durante la revisión")
            if anchor.id != anchor_id or checked_boundary != anchor_at or account_book_revision(db, account_id) != revision:
                raise MexcExecutionStateError("El libro REAL cambió durante la lectura MEXC")
            _new_events_are_booked(db, policy, anchor, link,opening_at=anchor_at)
            book = account_snapshot(db, account_id)
            events = list_events(db, account_id)
            if any(event.occurred_at > checked_at for event in events):
                raise MexcExecutionStateError("El libro REAL contiene movimientos futuros")
            balances = {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                        "observed_at": evidence.account_at.isoformat(), "balances": [
                            {"asset": row.asset, "free": str(row.free), "locked": str(row.locked),
                             "total": str(row.total)} for row in evidence.balances]}
            comparison = compare_mexc_spot_inventory(book, balances, now=checked_at)
            pair_position = book.positions.get((policy.base_asset, policy.quote_asset))
            pair_quantity = pair_position.quantity if pair_position is not None else Decimal(0)
            # Other assets stay in the full parity check, outside this pair's spending and exposure.
            if (not comparison["all_quantities_equal"] or comparison["negative_book_cash_assets"]
                    or book.cash.get(policy.quote_asset, Decimal(0)) != evidence.free_quote
                    or pair_quantity != evidence.free_base):
                raise MexcExecutionStateError("El saldo MEXC no coincide con el libro REAL o los fondos del par")
            risk = calculate_daily_book_risk(account_id, events, policy.quote_asset,
                                             now=checked_at, base_asset=policy.base_asset)
        with localcontext() as context:
            context.prec = 300
            exposure = (evidence.free_base + evidence.locked_base) * evidence.bid
            unrealized_loss = max(Decimal(0), risk.position_cost_basis_quote - exposure)
        state = LiveSpotState(
            account_ref=policy.account_ref, symbol=policy.symbol,
            reconciled=True, stopped=stopped, account_verified=True, instrument_verified=True,
            observed_at=min(evidence.account_at, survey_at, trades_at, transfer_at),
            quote_at=evidence.quote_at, best_bid=evidence.bid, best_ask=evidence.ask,
            free_base=evidence.free_base, free_quote=evidence.free_quote,
            pending_base=Decimal(0), pending_quote=Decimal(0),
            daily_turnover_quote=risk.turnover_quote, pending_turnover_quote=Decimal(0),
            exposure_quote=exposure, daily_realized_pnl_quote=risk.realized_pnl_quote,
            average_cost_quote_per_base=risk.average_cost_quote_per_base,
            unrealized_loss_quote=unrealized_loss)
        if audit_receipt is not None:
            from app.live_execution.journal import _canonical
            # Sólo campos ya validados: nunca respuestas crudas, UID, URLs
            # firmadas, direcciones ni columnas nuevas de un proveedor.
            audit_receipt.update({
                "started_at":started.isoformat(), "checked_at":checked_at.isoformat(),
                "opening_id":anchor_id, "book_revision":revision,
                "balances":balances, "open_orders_at":evidence.open_orders_at.isoformat(),
                "survey":{key:survey[key] for key in (
                    "source","accounting","coverage","symbol","observed_at","orders")},
                "trades":{"source":"MEXC_SPOT_PRIVATE_READONLY","accounting":"OBSERVED_NOT_IMPORTED",
                    "symbol":symbol,"limit":1000,"pages":trade_pages,"coverage":coverage,
                    "observed_at":trades_at.isoformat(),"start_ms":anchor_ms,"end_ms":trade_end,
                    "trades":[{"id":row["trade_id"],
                        **{key:row[key] for key in ("order_id","side","time_ms","commission_asset")},
                        **{key:format(row[key],"f") for key in ("price","quantity","quote_quantity","commission")}}
                        for row in trade_rows if row["time_ms"] >= anchor_ms]},
                "transfers":{"source":"MEXC_SPOT_PRIVATE_READONLY","accounting":"OBSERVED_NOT_IMPORTED",
                    "coverage":"RECENT_WINDOW_BELOW_PAGE_LIMIT","observed_at":transfer_at.isoformat(),
                    "start_ms":transfer_start,"end_ms":transfer_end,
                    "deposits":deposits,"withdrawals":withdrawals,"requests":transfers.get("requests",2)},
                "risk":json.loads(_canonical(risk)),
            })
        return MexcLiveInputs(state, evidence.rules, evidence.last, evidence.quote_at)
    except MexcExecutionStateError:
        raise
    except Exception:
        # Ningún error de proveedor ni de bóveda puede filtrar claves o UID.
        raise MexcExecutionStateError("No se pudo verificar la cuenta y la actividad MEXC") from None
