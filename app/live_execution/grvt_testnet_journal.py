"""Diario cifrado de intentos GRVT testnet: nunca guarda claves ni firmas.

La marca POST_CLAIMED se confirma antes de abrir la red. No existe transición
de vuelta a PREPARED: un proceso caído exige consulta externa por ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from sqlalchemy import select

from app.live_execution.grvt_testnet_order import (
    GrvtTestnetLimits, GrvtTestnetOrderAck, GrvtTestnetOrderObservation,
    _validate_order,
)
from app.live_execution.grvt_testnet_preflight import (
    _verified as _verified_preflight, verified_preflight_for_claim,
)
from app.live_execution.grvt_testnet_evidence import _verified_payload
from app.live_execution.policy import _utc
from app.portfolio.models import (
    GrvtTestnetAuditEvidence, GrvtTestnetCancelAttempt,
    GrvtTestnetOrderIntent, GrvtTestnetPreflightEvidence, GrvtTestnetStop,
)
from app.portfolio.vault import RealVault


class GrvtTestnetJournalError(RuntimeError):
    """Estado o identidad testnet incompatibles; no revela la firma."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetRecord:
    client_order_id: str
    status: str
    external_order_id: str | None
    external_status: str | None
    traded_size: Decimal | None


@dataclass(frozen=True, slots=True)
class GrvtTestnetCancelRecord:
    client_order_id: str
    sub_account_id: str
    status: str
    external_status: str | None
    claimed_at: datetime


def _digest(order: dict) -> str:
    payload = json.dumps(order, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _record(row: GrvtTestnetOrderIntent) -> GrvtTestnetRecord:
    return GrvtTestnetRecord(row.client_order_id, row.status,
                             row.external_order_id, row.external_status,
                             row.external_traded_size)


def _same_request(row: GrvtTestnetOrderIntent, order: dict,
                  limits: GrvtTestnetLimits) -> bool:
    leg = order["legs"][0]
    return (row.request_digest == _digest(order)
            and row.sub_account_id == limits.sub_account_id
            and row.instrument == limits.instrument
            and row.side == ("BUY" if leg["is_buying_asset"] else "SELL")
            and row.size == Decimal(leg["size"])
            and row.limit_price == Decimal(leg["limit_price"])
            and row.max_order_notional == limits.max_order_notional)


def require_new_testnet_submission(vault: RealVault, sub_account_id: str,
                                   client_order_id: str) -> None:
    """Filtro local previo al login; la reserva transaccional sigue siendo final.

    No autoriza un POST: sólo evita usar credenciales o consultar GRVT si ya
    sabemos que la subcuenta está parada o hay un intento pendiente.
    """
    if (not isinstance(sub_account_id, str)
            or not re.fullmatch(r"[0-9]{1,30}", sub_account_id)
            or not isinstance(client_order_id, str)
            or not client_order_id.isdecimal()
            or not 2**63 <= int(client_order_id) <= 2**64 - 1):
        raise GrvtTestnetJournalError("Identidad testnet inválida")
    with vault.session() as db:
        _require_reconciled(db, sub_account_id, client_order_id)
        stop = db.get(GrvtTestnetStop, sub_account_id)
        if stop is not None and stop.stopped:
            raise GrvtTestnetJournalError("Subcuenta testnet detenida")
        if db.get(GrvtTestnetOrderIntent, client_order_id) is not None:
            raise GrvtTestnetJournalError("Señal testnet ya archivada; recupera su ID")
        pending = db.scalar(select(GrvtTestnetOrderIntent.client_order_id).where(
            GrvtTestnetOrderIntent.sub_account_id == sub_account_id,
            GrvtTestnetOrderIntent.status.in_(
                ("PREPARED", "POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED")),
        ).limit(1))
        if pending is not None:
            raise GrvtTestnetJournalError("Otra orden testnet sigue sin conciliar")


def prepare_testnet_order(vault: RealVault, order: dict,
                          limits: GrvtTestnetLimits, *, now: datetime,
                          now_ns: int) -> GrvtTestnetRecord:
    """Reserva un ID globalmente único en la bóveda, todavía sin enviar."""
    now = _utc(now, "now")
    client_id = _validate_order(order, limits, now_ns)
    leg = order["legs"][0]
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOrderIntent, client_id)
            if row is not None:
                if not _same_request(row, order, limits):
                    raise GrvtTestnetJournalError("ID testnet reutilizado con otra orden")
                result = _record(row)
                db.commit()
                return result
            stopped = db.get(GrvtTestnetStop, limits.sub_account_id)
            _require_reconciled(db, limits.sub_account_id, client_id)
            if stopped is not None and stopped.stopped:
                raise GrvtTestnetJournalError("Subcuenta testnet detenida")
            pending = db.scalar(select(GrvtTestnetOrderIntent.client_order_id).where(
                GrvtTestnetOrderIntent.sub_account_id == limits.sub_account_id,
                GrvtTestnetOrderIntent.status.in_(
                    ("PREPARED", "POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED")),
            ).limit(1))
            if pending is not None:
                raise GrvtTestnetJournalError("Otra orden testnet sigue sin conciliar")
            row = GrvtTestnetOrderIntent(
                client_order_id=client_id,
                sub_account_id=limits.sub_account_id,
                instrument=limits.instrument,
                side="BUY" if leg["is_buying_asset"] else "SELL",
                size=Decimal(leg["size"]),
                limit_price=Decimal(leg["limit_price"]),
                max_order_notional=limits.max_order_notional,
                request_digest=_digest(order),
                status="PREPARED", external_order_id=None, external_status=None,
                external_update_ns=None, external_traded_size=None,
                created_at=now, updated_at=now,
            )
            db.add(row)
            db.commit()
            return _record(row)
        except Exception:
            db.rollback()
            raise


def claim_testnet_post(vault: RealVault, order: dict,
                       limits: GrvtTestnetLimits, *, now: datetime,
                       now_ns: int) -> GrvtTestnetRecord:
    """Punto irreversible de no-reintento, duradero ANTES del POST externo."""
    now = _utc(now, "now")
    client_id = _validate_order(order, limits, now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOrderIntent, client_id)
            if row is None or row.status != "PREPARED" or not _same_request(row, order, limits):
                raise GrvtTestnetJournalError("Orden testnet no preparada para primer envío")
            _require_reconciled(db, limits.sub_account_id, client_id)
            require_submission_stop(db,row,now_ns=now_ns,order=order)
            if now < row.created_at or (now - row.created_at).total_seconds() > 120:
                raise GrvtTestnetJournalError("Preparación testnet caducada")
            verified_preflight_for_claim(db, row, now_ns=now_ns)
            row.status = "POST_CLAIMED"
            row.updated_at = now
            db.commit()
            return _record(row)
        except Exception:
            db.rollback()
            raise


def require_submission_stop(db,row,*,now_ns,order) -> None:
    """STOP sólo admite una concesión de reducción de emergencia verificable."""
    from app.live_execution.grvt_testnet_bracket_emergency import has_emergency_table,verified_emergency_scope
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.portfolio.models import GrvtTestnetBracketEmergency
    try:
        permit=db.get(GrvtTestnetBracketEmergency,row.client_order_id) if has_emergency_table(db) else None
        stop=db.get(GrvtTestnetStop,row.sub_account_id)
        if permit is not None:
            verified_emergency_scope(db,row,now_ns=now_ns,order=order)
        elif stop is not None and stop.stopped:
            raise GrvtTestnetJournalError("Subcuenta testnet detenida")
    except GrvtTestnetBracketError:
        raise GrvtTestnetJournalError('Concesión de emergencia testnet no verificable') from None


def _require_reconciled(db, sub_account_id: str, client_order_id: str) -> None:
    from app.live_execution.grvt_testnet_oco import require_no_pending_oco, GrvtTestnetOcoError
    from app.portfolio.models import GrvtTestnetOcoLeg
    try:
        require_no_pending_oco(db, sub_account_id)
        from app.live_execution.grvt_testnet_bracket_journal import require_bracket_ids_unreserved
        from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
        try:
            require_bracket_ids_unreserved(db,(client_order_id,))
        except GrvtTestnetBracketError:
            raise GrvtTestnetOcoError('Identidad reservada para OTOCO') from None
        if db.get(GrvtTestnetOcoLeg, client_order_id) is not None:
            raise GrvtTestnetOcoError("Identidad reservada por OCO")
    except GrvtTestnetOcoError:
        raise GrvtTestnetJournalError("Protección OCO/OTOCO pendiente o no verificable") from None
    from app.live_execution.grvt_testnet_reconciliation import (
        require_reconciled_continuity, GrvtTestnetReconciliationError)
    try:
        require_reconciled_continuity(db, sub_account_id)
    except GrvtTestnetReconciliationError:
        raise GrvtTestnetJournalError("Conciliación testnet no verificable") from None


def abandon_testnet_prepared(vault: RealVault, sub_account_id: str,
                             client_order_id: str, *, now: datetime) -> GrvtTestnetRecord:
    """Libera sólo una preparación que jamás alcanzó el punto de POST.

    El ID permanece archivado y no puede reutilizarse. Un POST_CLAIMED,
    incluso tras timeout o parada, nunca se libera por esta vía.
    """
    now = _utc(now, "now")
    if (not isinstance(sub_account_id, str) or not re.fullmatch(r"[0-9]{1,30}", sub_account_id)
            or not isinstance(client_order_id, str) or not client_order_id.isdecimal()
            or not 2**63 <= int(client_order_id) <= 2**64 - 1):
        raise GrvtTestnetJournalError("Identidad testnet inválida")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOrderIntent, client_order_id)
            if row is None or row.sub_account_id != sub_account_id or row.status != "PREPARED":
                raise GrvtTestnetJournalError("Sólo una preparación sin POST puede descartarse")
            if now < row.created_at:
                raise GrvtTestnetJournalError("Hora de descarte testnet inválida")
            row.status = "ABANDONED"
            row.updated_at = now
            db.commit()
            return _record(row)
        except Exception:
            db.rollback()
            raise


def close_rejected_testnet_zero_fill(vault: RealVault, sub_account_id: str,
                                     client_order_id: str, *, now: datetime
                                     ) -> GrvtTestnetRecord:
    """Cierra sólo un rechazo testnet de apertura sin ejecución ni cambios.

    Dos auditorías posteriores, separadas al menos cinco segundos, deben
    observar la misma orden terminal, cero fills/actividad y la cuenta igual
    al preflight. Es una transición interna de testnet, nunca una prueba de
    seguridad para mainnet o una autorización para enviar otra orden.
    """
    now = _utc(now, "now")
    if (not isinstance(sub_account_id, str)
            or not re.fullmatch(r"[0-9]{1,30}", sub_account_id)
            or not isinstance(client_order_id, str)
            or not client_order_id.isdecimal()
            or not 2**63 <= int(client_order_id) <= 2**64 - 1):
        raise GrvtTestnetJournalError("Identidad testnet inválida")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            intent = db.get(GrvtTestnetOrderIntent, client_order_id)
            if (intent is None or intent.sub_account_id != sub_account_id
                    or intent.status != "OBSERVED"
                    or intent.external_status != "REJECTED"
                    or intent.external_traded_size != 0
                    or intent.external_order_id is None
                    or intent.external_update_ns is None):
                raise GrvtTestnetJournalError("Rechazo testnet sin ejecución no verificable")
            preflight_row = db.get(GrvtTestnetPreflightEvidence, client_order_id)
            if preflight_row is None or preflight_row.sub_account_id != sub_account_id:
                raise GrvtTestnetJournalError("Preflight testnet ausente")
            preflight = _verified_preflight(preflight_row)
            prior = preflight.get("state")
            if (preflight.get("request_digest") != intent.request_digest
                    or preflight.get("mode") != "OPEN"
                    or not isinstance(prior, dict)
                    or prior.get("positions") != []
                    or prior.get("open_order_count") != 0
                    or prior.get("unrealized_pnl") != "0"):
                raise GrvtTestnetJournalError("Apertura testnet previa no verificable")
            audits = db.scalars(select(GrvtTestnetAuditEvidence).where(
                GrvtTestnetAuditEvidence.client_order_id == client_order_id,
            ).order_by(GrvtTestnetAuditEvidence.observed_at.desc(),
                       GrvtTestnetAuditEvidence.id.desc())).all()
            if (len(audits) < 2
                    or (audits[0].observed_at - audits[1].observed_at).total_seconds() < 5
                    or audits[0].observed_at > now
                    or (now - audits[0].observed_at).total_seconds() > 30):
                raise GrvtTestnetJournalError("Faltan dos auditorías testnet recientes")
            for audit in audits:
                if audit.sub_account_id != sub_account_id:
                    raise GrvtTestnetJournalError("Auditoría testnet ajena a la subcuenta")
                payload = _verified_payload(audit)
                observed = payload.get("order")
                fills = payload.get("fills")
                account = payload.get("account")
                comparison = payload.get("comparison")
                if (not isinstance(observed, dict) or not isinstance(fills, dict)
                        or not isinstance(account, dict) or not isinstance(comparison, dict)
                        or observed.get("client_order_id") != client_order_id
                        or observed.get("sub_account_id") != sub_account_id
                        or observed.get("order_id") != intent.external_order_id
                        or observed.get("status") != "REJECTED"
                        or observed.get("update_time_ns") != intent.external_update_ns
                        or observed.get("traded_size") != "0"
                        or fills.get("rows") != [] or fills.get("external_rows") != []
                        or fills.get("count") != 0
                        or fills.get("external_fill_count") != 0
                        or fills.get("external_activity_detected") is not False
                        or fills.get("sizes_match") is not True
                        or fills.get("gross_notional") != "0"
                        or fills.get("realized_pnl") != "0"
                        or fills.get("builder_fee") != "0"
                        or fills.get("fees_by_currency") != []
                        or type(fills.get("pages_drained")) is not int
                        or fills["pages_drained"] < 1
                        or comparison.get("available") is not True
                        or comparison.get("preflight_digest") != preflight_row.payload_digest
                        or comparison.get("position_delta_matches") is not True
                        or comparison.get("position_delta") != "0"
                        or comparison.get("expected_fill_delta") != "0"
                        or account.get("settle_currency") != prior.get("settle_currency")
                        or account.get("settle_balance") != prior.get("settle_balance")
                        or account.get("total_equity") != prior.get("total_equity")
                        or account.get("initial_margin") != prior.get("initial_margin")
                        or account.get("maintenance_margin") != prior.get("maintenance_margin")
                        or account.get("positions") != []
                        or account.get("open_order_count") != 0):
                    raise GrvtTestnetJournalError("Rechazo testnet no conciliado")
                for name, count in (
                    ("funding", "payment_count"), ("transfers", "transfer_count"),
                    ("withdrawals", "withdrawal_count"), ("deposits", "deposit_count"),
                    ("interest", "payment_count"),
                ):
                    section = payload.get(name)
                    if (not isinstance(section, dict) or section.get("rows") != []
                            or section.get(count) != 0
                            or name in ("transfers", "withdrawals")
                            and section.get("unrelated_row_count") != 0
                            or type(section.get("pages_drained")) is not int
                            or section["pages_drained"] < 1):
                        raise GrvtTestnetJournalError("Actividad testnet no conciliada")
                event_ns = account.get("event_time_ns")
                audit_ns = (int(audit.observed_at.timestamp()) * 1_000_000_000
                            + audit.observed_at.microsecond * 1000)
                if (audit_ns < int(intent.external_update_ns)
                        or type(event_ns) is not int or event_ns < int(intent.external_update_ns)
                        or event_ns > audit_ns + 2_000_000_000
                        or audit_ns - event_ns > 30_000_000_000):
                    raise GrvtTestnetJournalError("Cuenta testnet posterior no reciente")
            intent.status = "REJECTED_CLOSED"
            intent.updated_at = now
            db.commit()
            return _record(intent)
        except Exception:
            db.rollback()
            raise


def record_testnet_ack(vault: RealVault, ack: GrvtTestnetOrderAck,
                       *, now: datetime) -> GrvtTestnetRecord:
    now = _utc(now, "now")
    if (not ack.accepted_by_testnet or not ack.execution_unverified
            or not isinstance(ack.order_id, str)
            or not re.fullmatch(r"[A-Za-z0-9x]{1,80}", ack.order_id)):
        raise GrvtTestnetJournalError("Acuse testnet no verificable")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOrderIntent, ack.client_order_id)
            if (row is None or row.status != "POST_CLAIMED"
                    or row.sub_account_id != ack.sub_account_id):
                raise GrvtTestnetJournalError("Acuse testnet sin intento pendiente")
            row.status = "ACKNOWLEDGED"
            row.external_order_id = ack.order_id
            row.updated_at = now
            db.commit()
            return _record(row)
        except Exception:
            db.rollback()
            raise


def record_testnet_observation(vault: RealVault,
                               observation: GrvtTestnetOrderObservation,
                               *, now: datetime) -> GrvtTestnetRecord:
    """Registra estado puntual; nunca asienta un fill o libera riesgo."""
    now = _utc(now, "now")
    try:
        seen_ns = int(observation.update_time_ns)
        observed_size = Decimal(observation.size)
        traded_size = Decimal(observation.traded_size)
        observed_price = Decimal(observation.limit_price or "0")
    except (ValueError, TypeError, InvalidOperation):
        raise GrvtTestnetJournalError("Observación testnet malformada") from None
    if (seen_ns <= 0 or observed_size <= 0 or observed_price <= 0
            or not observed_size.is_finite() or not observed_price.is_finite()
            or not traded_size.is_finite() or not 0 <= traded_size <= observed_size
            or observation.status not in ("PENDING", "OPEN", "FILLED", "REJECTED", "CANCELLED")
            or observation.status == "FILLED" and traded_size != observed_size
            or not isinstance(observation.order_id, str)
            or not re.fullmatch(r"[A-Za-z0-9x]{1,80}", observation.order_id)):
        raise GrvtTestnetJournalError("Observación testnet malformada")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOrderIntent, observation.client_order_id)
            if (row is None or row.status not in ("POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED")
                    or row.sub_account_id != observation.sub_account_id
                    or row.instrument != observation.instrument
                    or row.side != observation.side
                    or row.size != observed_size
                    or row.limit_price != observed_price
                    or row.external_order_id not in (None, observation.order_id)):
                raise GrvtTestnetJournalError("Observación testnet ajena a la intención")
            if row.external_update_ns is not None and int(row.external_update_ns) > seen_ns:
                raise GrvtTestnetJournalError("Observación testnet anterior a la guardada")
            if row.external_traded_size is not None and traded_size < row.external_traded_size:
                raise GrvtTestnetJournalError("Ejecución testnet retrocedió")
            if row.external_status in ("FILLED", "REJECTED", "CANCELLED") and row.external_status != observation.status:
                raise GrvtTestnetJournalError("Orden testnet terminal cambió de estado")
            if row.external_status == "OPEN" and observation.status == "PENDING":
                raise GrvtTestnetJournalError("Orden testnet retrocedió de estado")
            row.status = "OBSERVED"
            row.external_order_id = observation.order_id
            row.external_status = observation.status
            row.external_update_ns = str(seen_ns)
            row.external_traded_size = traded_size
            row.updated_at = now
            db.commit()
            return _record(row)
        except Exception:
            db.rollback()
            raise


def stop_testnet_subaccount(vault: RealVault, sub_account_id: str,
                            *, now: datetime, reason='MANUAL', parent_client_order_id: str | None=None) -> int:
    """Detiene nuevos POST; los ya reclamados continúan inciertos."""
    now = _utc(now, "now")
    if not isinstance(sub_account_id, str) or not sub_account_id.isdecimal() or not 1 <= len(sub_account_id) <= 30:
        raise GrvtTestnetJournalError("Subcuenta testnet inválida")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            from app.live_execution.grvt_stop_control import record_testnet_stop
            record_testnet_stop(db,sub_account_id,now=now,reason=reason,parent_client_order_id=parent_client_order_id)
            rows = db.scalars(select(GrvtTestnetOrderIntent).where(
                GrvtTestnetOrderIntent.sub_account_id == sub_account_id,
                GrvtTestnetOrderIntent.status == "PREPARED",
            )).all()
            for row in rows:
                row.status = "STOPPED"
                row.updated_at = now
            db.commit()
            return len(rows)
        except Exception:
            db.rollback()
            raise


def _cancel_record(row: GrvtTestnetCancelAttempt) -> GrvtTestnetCancelRecord:
    return GrvtTestnetCancelRecord(row.client_order_id, row.sub_account_id,
                                   row.status, row.external_status,
                                   row.claimed_at)


def require_testnet_cancel(vault: RealVault, sub_account_id: str,
                           client_order_id: str) -> GrvtTestnetCancelRecord:
    """Exige una cancelación ya reclamada antes de consultar datos externos."""
    with vault.session() as db:
        row = db.get(GrvtTestnetCancelAttempt, client_order_id)
        stop = db.get(GrvtTestnetStop, sub_account_id)
        if (row is None or row.sub_account_id != sub_account_id
                or stop is None or not stop.stopped):
            raise GrvtTestnetJournalError("Cancelación testnet no reclamada")
        return _cancel_record(row)


def claim_testnet_cancel(vault: RealVault, sub_account_id: str,
                         client_order_id: str, *, now: datetime,
                         ) -> GrvtTestnetCancelRecord:
    """Marca en disco el único intento antes de cancelar por ID cliente.

    El POST original puede seguir en tránsito. Una cancelación con TTL finito
    no prueba que nunca llegará; por ello el intento original sigue bloqueado.
    """
    now = _utc(now, "now")
    if (not isinstance(sub_account_id, str) or not re.fullmatch(r"[0-9]{1,30}", sub_account_id)
            or not isinstance(client_order_id, str) or not client_order_id.isdecimal()
            or not 2**63 <= int(client_order_id) <= 2**64 - 1):
        raise GrvtTestnetJournalError("Identidad de cancelación testnet inválida")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            stop = db.get(GrvtTestnetStop, sub_account_id)
            intent = db.get(GrvtTestnetOrderIntent, client_order_id)
            previous = db.get(GrvtTestnetCancelAttempt, client_order_id)
            if stop is None or not stop.stopped:
                raise GrvtTestnetJournalError("Subcuenta testnet no detenida")
            if previous is not None:
                if previous.sub_account_id != sub_account_id:
                    raise GrvtTestnetJournalError("Cancelación testnet ajena a la subcuenta")
                raise GrvtTestnetJournalError("Cancelación testnet ya reclamada; consulta por ID")
            if (intent is None or intent.sub_account_id != sub_account_id
                    or intent.status not in ("POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED")
                    or intent.external_status in ("FILLED", "REJECTED", "CANCELLED")):
                raise GrvtTestnetJournalError("Orden testnet no cancelable")
            row = GrvtTestnetCancelAttempt(
                client_order_id=client_order_id, sub_account_id=sub_account_id,
                status="CLAIMED", external_status=None,
                external_update_ns=None, claimed_at=now, updated_at=now,
            )
            db.add(row)
            db.commit()
            return _cancel_record(row)
        except Exception:
            db.rollback()
            raise


def record_testnet_cancel_observation(vault: RealVault,
                                       observation: GrvtTestnetOrderObservation,
                                       *, now: datetime,
                                       ) -> GrvtTestnetCancelRecord:
    """Registra respuesta o consulta; no libera la intención ni autoriza reenvío."""
    now = _utc(now, "now")
    if (not isinstance(observation, GrvtTestnetOrderObservation)
            or observation.status not in ("PENDING", "OPEN", "FILLED", "REJECTED", "CANCELLED")):
        raise GrvtTestnetJournalError("Observación de cancelación testnet inválida")
    try:
        observed_size = Decimal(observation.size)
    except (InvalidOperation, TypeError, ValueError):
        raise GrvtTestnetJournalError("Tamaño de cancelación testnet inválido") from None
    if not observed_size.is_finite() or observed_size <= 0:
        raise GrvtTestnetJournalError("Tamaño de cancelación testnet inválido")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetCancelAttempt,
                         observation.client_order_id)
            intent = db.get(GrvtTestnetOrderIntent,
                            observation.client_order_id)
            if (row is None or intent is None or row.sub_account_id != observation.sub_account_id
                    or intent.sub_account_id != observation.sub_account_id
                    or intent.instrument != observation.instrument
                    or intent.side != observation.side
                    or intent.size != observed_size
                    or intent.external_order_id is not None
                    and intent.external_order_id != observation.order_id
                    or row.status not in ("CLAIMED", "RESPONSE_OBSERVED", "AUDITED")):
                raise GrvtTestnetJournalError("Cancelación testnet ajena al intento")
            try:
                update_ns = int(observation.update_time_ns)
            except (TypeError, ValueError):
                raise GrvtTestnetJournalError("Hora de cancelación testnet inválida") from None
            if update_ns <= 0 or (row.external_update_ns is not None
                                  and update_ns < int(row.external_update_ns)):
                raise GrvtTestnetJournalError("Cancelación testnet retrocedió")
            if (row.external_status in ("FILLED", "REJECTED", "CANCELLED")
                    and row.external_status != observation.status):
                raise GrvtTestnetJournalError("Cancelación testnet terminal cambió")
            unchanged_audited = (row.status == "AUDITED"
                                 and row.external_status == observation.status
                                 and row.external_update_ns == observation.update_time_ns)
            row.status = "AUDITED" if unchanged_audited else "RESPONSE_OBSERVED"
            row.external_status = observation.status
            row.external_update_ns = observation.update_time_ns
            row.updated_at = now
            db.commit()
            return _cancel_record(row)
        except Exception:
            db.rollback()
            raise


def mark_testnet_cancel_audited(vault: RealVault,
                                observation: GrvtTestnetOrderObservation,
                                evidence_id: str, *, now: datetime,
                                ) -> GrvtTestnetCancelRecord:
    """Marca la revisión posterior completa; jamás levanta la parada."""
    now = _utc(now, "now")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetCancelAttempt, observation.client_order_id)
            intent = db.get(GrvtTestnetOrderIntent, observation.client_order_id)
            evidence = db.get(GrvtTestnetAuditEvidence, evidence_id)
            stop = db.get(GrvtTestnetStop, observation.sub_account_id)
            if (row is None or intent is None or evidence is None
                    or stop is None or not stop.stopped
                    or row.sub_account_id != observation.sub_account_id
                    or intent.sub_account_id != observation.sub_account_id
                    or evidence.sub_account_id != observation.sub_account_id
                    or evidence.client_order_id != observation.client_order_id
                    or row.status not in ("RESPONSE_OBSERVED", "AUDITED")
                    or intent.status != "OBSERVED"
                    or row.external_status != observation.status
                    or row.external_update_ns != observation.update_time_ns
                    or intent.external_status != observation.status
                    or intent.external_update_ns != observation.update_time_ns):
                raise GrvtTestnetJournalError("Auditoría de cancelación testnet ajena")
            payload = _verified_payload(evidence)
            archived = payload["order"]
            if (archived.get("order_id") != observation.order_id
                    or archived.get("status") != observation.status
                    or archived.get("update_time_ns") != observation.update_time_ns
                    or archived.get("traded_size") != observation.traded_size):
                raise GrvtTestnetJournalError("Auditoría de cancelación testnet incoherente")
            row.status = "AUDITED"
            row.updated_at = now
            db.commit()
            return _cancel_record(row)
        except Exception:
            db.rollback()
            raise
