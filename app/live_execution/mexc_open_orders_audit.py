"""Compara órdenes Spot MEXC abiertas con el diario cifrado de ORBIS.

La auditoría por símbolo tiene cobertura limitada. La sonda sin símbolo
detecta conflictos en otros pares, pero tampoco prueba cobertura global ni
autoriza el envío real. Un resultado incierto bloquea.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import re
from uuid import UUID

from sqlalchemy import select

from app.live_execution.mexc_live_observation import MexcLiveEvidence
from app.live_execution.policy import LiveSpotPolicy, _utc
from app.portfolio.models import RealLiveOrderObservation, RealLiveSpotIntent
from app.portfolio.vault import RealVault


_ACCOUNT_REF = re.compile(r"[0-9a-f]{64}\Z")


def _open_order_archive_mismatch(order: dict, intent: RealLiveSpotIntent,
                                 archived: RealLiveOrderObservation,
                                 snapshot_at: datetime) -> str | None:
    """Una orden abierta actual debe tener un snapshot de fills no anterior."""
    if archived.observed_at < snapshot_at:
        return "OPEN_ORDER_ARCHIVE_STALE"
    if archived.status != order.get("status"):
        return "OPEN_ORDER_PROGRESS_MISMATCH"
    try:
        raw = [order[name] for name in ("price", "executed_quantity", "quantity",
                                       "remaining_quantity")]
        if any(not isinstance(value, str) or not 1 <= len(value) <= 80 for value in raw):
            return "OPEN_ORDER_SNAPSHOT_INVALID"
        price, executed, quantity, remaining = (Decimal(value) for value in raw)
    except (KeyError, TypeError, InvalidOperation):
        return "OPEN_ORDER_SNAPSHOT_INVALID"
    if not all(value.is_finite() and value >= 0
               and len(value.as_tuple().digits) <= 40
               and abs(value.as_tuple().exponent) <= 100
               and (value == 0 or value.adjusted() <= 40)
               for value in (price, executed, quantity, remaining)):
        return "OPEN_ORDER_SNAPSHOT_INVALID"
    with localcontext() as context:
        # Los valores admiten 40 dígitos significativos y exponentes hasta
        # ±100. La suma por defecto (28 dígitos) puede ocultar un resto real.
        context.prec = 300
        if (price <= 0 or quantity <= 0 or executed + remaining != quantity
                or executed != archived.executed_base):
            return "OPEN_ORDER_PROGRESS_MISMATCH"
        notional = price * quantity
    if (notional != intent.notional_quote
            or intent.limit_price_quote is not None and price != intent.limit_price_quote
            or intent.quantity_base is not None and quantity != intent.quantity_base
            or intent.side == "SELL" and intent.quantity_base is None
            and quantity != intent.reserved_base):
        return "OPEN_ORDER_TERMS_MISMATCH"
    return None


@dataclass(frozen=True, slots=True)
class MexcOpenOrderAudit:
    clear_for_symbol: bool
    reason: str
    matched_open_orders: int


@dataclass(frozen=True, slots=True)
class MexcAccountOpenOrderScan:
    clear_for_account: bool
    reason: str
    observed_open_orders: int
    matched_open_orders: int


def scan_unfiltered_spot_open_orders(vault: RealVault, account_ref: str,
                                     snapshot: dict, *, now: datetime,
                                     max_age_seconds: int) -> MexcAccountOpenOrderScan:
    """Detecta conflictos de varios pares; nunca certifica cuenta completa.

    MEXC no garantiza que una consulta sin `symbol` sea exhaustiva. No se
    puede construir `LiveSpotState.reconciled=True` a partir de esta sonda.
    """
    now = _utc(now, "now")

    def deny(reason: str, observed: int = 0, matched: int = 0) -> MexcAccountOpenOrderScan:
        return MexcAccountOpenOrderScan(False, reason, observed, matched)

    if (not isinstance(account_ref, str) or not _ACCOUNT_REF.fullmatch(account_ref)
            or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300
            or not isinstance(snapshot, dict)
            or snapshot.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or snapshot.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or snapshot.get("coverage") != "UNFILTERED_COVERAGE_UNPROVEN"
            or snapshot.get("symbol") is not None
            or not isinstance(snapshot.get("orders"), list)):
        return deny("UNFILTERED_SNAPSHOT_INVALID")
    try:
        observed_at = _utc(datetime.fromisoformat(snapshot["observed_at"]), "observed_at")
    except (KeyError, TypeError, ValueError):
        return deny("UNFILTERED_SNAPSHOT_INVALID")
    if observed_at > now or (now - observed_at).total_seconds() > max_age_seconds:
        return deny("UNFILTERED_SNAPSHOT_STALE")
    orders = snapshot["orders"]
    if len(orders) >= 1000:
        return deny("UNFILTERED_SNAPSHOT_LIMIT", len(orders))
    seen_clients: set[str] = set()
    seen_orders: set[str] = set()
    for order in orders:
        if not isinstance(order, dict):
            return deny("UNFILTERED_SNAPSHOT_INVALID", len(orders))
        client_id = order.get("client_order_id")
        order_id = order.get("order_id")
        if (not isinstance(client_id, str) or not client_id
                or not isinstance(order_id, str) or not order_id
                or client_id in seen_clients or order_id in seen_orders):
            return deny("UNFILTERED_SNAPSHOT_INVALID", len(orders))
        seen_clients.add(client_id)
        seen_orders.add(order_id)
    with vault.session() as db:
        # SQLite no abre una transacción de lectura en el primer SELECT bajo
        # el controlador habitual: fijamos la misma fotografía para ambas tablas.
        db.connection().exec_driver_sql("BEGIN")
        intents = db.scalars(select(RealLiveSpotIntent).where(
            RealLiveSpotIntent.account_ref == account_ref,
        )).all()
        by_client_id = {UUID(item.id).hex: item for item in intents}
        observations = db.scalars(select(RealLiveOrderObservation).where(
            RealLiveOrderObservation.account_ref == account_ref,
        ).order_by(RealLiveOrderObservation.observed_at.desc(),
                   RealLiveOrderObservation.id.desc())).all()
        latest = {}
        for item in observations:
            latest.setdefault(item.intent_id, item)
        matched = 0
        for order in orders:
            client_id = order.get("client_order_id")
            order_id = order.get("order_id")
            row = by_client_id.get(client_id)
            if row is None:
                return deny("EXTERNAL_OPEN_ORDER_OTHER_PAIR", len(orders), matched)
            if (row.symbol != order.get("symbol") or row.side != order.get("side")
                    or row.status != "SUBMISSION_UNKNOWN" or order.get("type") != "LIMIT"
                    or order.get("status") not in ("NEW", "PARTIALLY_FILLED")):
                return deny("OPEN_ORDER_JOURNAL_MISMATCH", len(orders), matched)
            archived = latest.get(row.id)
            if (archived is None or archived.observed_at > now
                    or archived.account_ref != row.account_ref
                    or archived.symbol != row.symbol
                    or archived.external_order_id != order_id
                    or archived.status not in ("NEW", "PARTIALLY_FILLED")):
                return deny("OPEN_ORDER_NOT_ARCHIVED", len(orders), matched)
            mismatch = _open_order_archive_mismatch(order, row, archived, observed_at)
            if mismatch:
                return deny(mismatch, len(orders), matched)
            matched += 1
        for row in intents:
            if row.status == "SUBMISSION_UNKNOWN" and UUID(row.id).hex not in seen_clients:
                archived = latest.get(row.id)
                return deny("FINAL_RESERVATION_NOT_CLOSED" if archived is not None
                            and archived.status in ("FILLED", "CANCELED", "PARTIALLY_CANCELED")
                            else "SUBMISSION_OUTCOME_UNKNOWN", len(orders), matched)
    return deny("UNFILTERED_COVERAGE_UNPROVEN", len(orders), matched)


def audit_spot_open_orders(vault: RealVault, policy: LiveSpotPolicy,
                           evidence: MexcLiveEvidence, *, now: datetime
                           ) -> MexcOpenOrderAudit:
    """Falla cerrado ante órdenes externas, archivado ausente o estado incierto."""
    now = _utc(now, "now")

    def deny(reason: str, count: int = 0) -> MexcOpenOrderAudit:
        return MexcOpenOrderAudit(False, reason, count)

    if (not isinstance(policy, LiveSpotPolicy) or not isinstance(evidence, MexcLiveEvidence)
            or evidence.account_ref != policy.account_ref
            or evidence.symbol != policy.symbol):
        return deny("ACCOUNT_OR_SYMBOL_MISMATCH")
    if (evidence.open_orders_at > now
            or (now - evidence.open_orders_at).total_seconds() > policy.max_data_age_seconds):
        return deny("OPEN_ORDERS_STALE")
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN")
        intents = db.scalars(select(RealLiveSpotIntent).where(
            RealLiveSpotIntent.account_ref == policy.account_ref,
            RealLiveSpotIntent.symbol == policy.symbol,
        )).all()
        by_client_id = {UUID(item.id).hex: item for item in intents}
        observations = db.scalars(select(RealLiveOrderObservation).where(
            RealLiveOrderObservation.account_ref == policy.account_ref,
            RealLiveOrderObservation.symbol == policy.symbol,
        ).order_by(RealLiveOrderObservation.observed_at.desc(),
                   RealLiveOrderObservation.id.desc())).all()
        latest = {}
        for item in observations:
            latest.setdefault(item.intent_id, item)
        seen = set()
        seen_clients = set()
        seen_orders = set()
        matched = 0
        for order in evidence.open_orders:
            if not isinstance(order, dict):
                return deny("OPEN_ORDER_SNAPSHOT_INVALID", matched)
            client_id = order.get("client_order_id")
            order_id = order.get("order_id")
            if (not isinstance(client_id, str) or not client_id
                    or not isinstance(order_id, str) or not order_id
                    or client_id in seen_clients or order_id in seen_orders):
                return deny("OPEN_ORDER_SNAPSHOT_INVALID", matched)
            seen_clients.add(client_id)
            seen_orders.add(order_id)
            row = by_client_id.get(client_id)
            if row is None:
                return deny("EXTERNAL_OPEN_ORDER", matched)
            if (row.status != "SUBMISSION_UNKNOWN" or row.side != order.get("side")
                    or order.get("symbol") != policy.symbol
                    or order.get("type") != "LIMIT"):
                return deny("OPEN_ORDER_JOURNAL_MISMATCH", matched)
            archived = latest.get(row.id)
            if (archived is None or archived.observed_at > now
                    or archived.account_ref != row.account_ref
                    or archived.symbol != row.symbol
                    or archived.external_order_id != order.get("order_id")
                    or archived.status not in ("NEW", "PARTIALLY_FILLED")):
                return deny("OPEN_ORDER_NOT_ARCHIVED", matched)
            mismatch = _open_order_archive_mismatch(order, row, archived, evidence.open_orders_at)
            if mismatch:
                return deny(mismatch, matched)
            seen.add(row.id)
            matched += 1
        for row in intents:
            if row.status == "SUBMISSION_UNKNOWN" and row.id not in seen:
                archived = latest.get(row.id)
                return deny("FINAL_RESERVATION_NOT_CLOSED" if archived is not None
                            and archived.status in ("FILLED", "CANCELED", "PARTIALLY_CANCELED")
                            else "SUBMISSION_OUTCOME_UNKNOWN", matched)
        return MexcOpenOrderAudit(True, "SYMBOL_OPEN_ORDERS_MATCH_JOURNAL", matched)
