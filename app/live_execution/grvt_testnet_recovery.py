"""Resumen local de recuperación GRVT testnet después de un reinicio.

Sólo lee la bóveda cifrada. No consulta GRVT, no reenvía POST y nunca concede
permiso para operar: una evidencia observacional no es conciliación causal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import re
import time

from sqlalchemy import select

from app.live_execution.grvt_testnet_evidence import GrvtTestnetEvidenceError, _verified_payload
from app.live_execution.grvt_testnet_funding import GrvtTestnetFundingError, linked_testnet_funding
from app.live_execution.grvt_testnet_journal import GrvtTestnetJournalError
from app.live_execution.grvt_testnet_preflight import (
    GrvtTestnetPreflightError, verified_archived_preflight,
)
from app.portfolio.models import (
    GrvtTestnetAuditEvidence, GrvtTestnetCancelAttempt,
    GrvtTestnetOrderIntent, GrvtTestnetPreflightEvidence, GrvtTestnetStop,
)
from app.portfolio.vault import RealVault


@dataclass(frozen=True, slots=True)
class GrvtTestnetRecoveryItem:
    client_order_id: str
    instrument: str
    side: str
    intended_size: Decimal
    journal_status: str
    external_status: str | None
    external_traded_size: Decimal | None
    cancel_status: str | None
    cancel_external_status: str | None
    verified_audit_count: int
    current_state_audited: bool
    current_audit_findings: tuple[str, ...]
    next_check: str
    signal_source: dict | None = None
    price_guard: dict | None = None
    fill_summary: dict | None = None
    reconciliation: dict | None = None
    can_reconcile: bool = False
    history_continuity: dict | None = None
    funding_summary: dict | None = None


@dataclass(frozen=True, slots=True)
class GrvtTestnetRecoveryReport:
    sub_account_id: str
    stopped: bool
    items: tuple[GrvtTestnetRecoveryItem, ...]
    can_submit: bool = False


def _next_check(intent: GrvtTestnetOrderIntent,
                cancel: GrvtTestnetCancelAttempt | None,
                current_state_audited: bool,
                findings: tuple[str, ...]) -> str:
    review = ("REVIEW_AUDIT_FINDINGS_NO_RELEASE" if findings
              else "REVIEW_AUDIT_NO_RELEASE")
    if intent.status == "RECONCILED":
        return "ISOLATED_TESTNET_RECONCILED"
    if cancel is not None and cancel.status == "CLAIMED":
        return "LOOKUP_CANCEL_AND_AUDIT"
    if cancel is not None and cancel.status == "RESPONSE_OBSERVED":
        return "AUDIT_AFTER_CANCEL"
    if cancel is not None and cancel.status == "AUDITED":
        return (review if current_state_audited
                else "AUDIT_AFTER_CANCEL")
    if intent.status == "POST_CLAIMED":
        return "LOOKUP_ORDER_BY_CLIENT_ID"
    if intent.status in ("ACKNOWLEDGED", "OBSERVED"):
        return (review if current_state_audited
                else "AUDIT_ORDER_FILLS_ACCOUNT")
    if intent.status == "PREPARED":
        return "PREFLIGHT_OR_ABANDON_UNSENT"
    if intent.status == "REJECTED_CLOSED":
        return "REJECTED_ZERO_FILL_ARCHIVED"
    return "ARCHIVED_NO_POST"


def _findings(payload: dict) -> tuple[str, ...]:
    """Diagnóstico, nunca prueba causal ni permiso para reanudar una orden."""
    fills = payload.get("fills", {})
    comparison = payload.get("comparison", {})
    flags = []
    if fills.get("sizes_match") is not True:
        flags.append("FILL_TOTAL_MISMATCH")
    if comparison.get("available") is not True:
        flags.append("PREFLIGHT_MISSING")
    elif comparison.get("position_delta_matches") is not True:
        flags.append("POSITION_DELTA_MISMATCH")
    if fills.get("external_activity_detected") is True:
        flags.append("EXTERNAL_FILLS")
    for section, field, flag in (
        ("funding", "payment_count", "FUNDING_ACTIVITY"),
        ("transfers", "transfer_count", "TRANSFER_ACTIVITY"),
        ("withdrawals", "withdrawal_count", "WITHDRAWAL_ACTIVITY"),
        ("deposits", "deposit_count", "DEPOSIT_CONTEXT"),
        ("interest", "payment_count", "INTEREST_ACTIVITY"),
    ):
        if payload.get(section, {}).get(field, 0):
            flags.append(flag)
    return tuple(flags)


def _funding_summary(payload: dict) -> dict | None:
    """Importe reportado, no permiso ni estimación desde tasas públicas."""
    section = payload.get("funding")
    if (isinstance(section,dict) and type(section.get("payment_count")) is int
            and section["payment_count"] == 0 and section.get("rows") == []):
        return None
    try:
        linked = linked_testnet_funding(section,payload["fills"]["rows"],
            instrument=payload["order"]["instrument"],start_ns=section["history_start_ns"],
            end_ns=min(section["observed_until_ns"],payload["account"]["event_time_ns"]))
        return {"payment_count":linked["payment_count"],"paid_usdt":linked["paid_usdt"],
                "linked_to_own_fills":True,"complete_for_execution":False}
    except (GrvtTestnetFundingError,KeyError,TypeError,ValueError):
        return {"payment_count":None,"paid_usdt":None,
                "linked_to_own_fills":False,"complete_for_execution":False}


def _fill_summary(payload: dict) -> dict | None:
    """Recalcula importes desde fills propios; nunca certifica saldo o P/L final.

    Auditorías antiguas sin contador completo siguen legibles, pero no se les
    atribuye un precio de ejecución. Una auditoría con resumen parcial o
    contradictorio se rechaza incluso si alguien recalculó su SHA-256.
    """
    fills = payload.get("fills", {})
    if "count" not in fills:
        return None

    def amount(raw: object, *, signed: bool = False, positive: bool = False) -> Decimal:
        pattern = r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?" if signed else r"[0-9]{1,30}(?:\.[0-9]{1,18})?"
        if not isinstance(raw, str) or not re.fullmatch(pattern, raw):
            raise GrvtTestnetEvidenceError("Importe de fills testnet incoherente")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise GrvtTestnetEvidenceError("Importe de fills testnet incoherente") from None
        if not value.is_finite() or positive and value <= 0:
            raise GrvtTestnetEvidenceError("Importe de fills testnet incoherente")
        return value

    rows = fills.get("rows")
    if (type(fills.get("count")) is not int or not isinstance(rows, list)
            or len(rows) > 10_000 or fills["count"] != len(rows)
            or type(fills.get("sizes_match")) is not bool):
        raise GrvtTestnetEvidenceError("Resumen de fills testnet incoherente")
    seen: set[str] = set()
    fees: dict[str, Decimal] = {}
    size = notional = pnl = builder = Decimal(0)
    last_ns: int | None = None
    with localcontext() as context:
        context.prec = 100
        for row in rows:
            if not isinstance(row, dict):
                raise GrvtTestnetEvidenceError("Fill testnet archivado ilegible")
            trade_id, event_ns = row.get("trade_id"), row.get("event_time_ns")
            currency = row.get("fee_currency")
            if (not isinstance(trade_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_:\-]{1,100}", trade_id)
                    or trade_id in seen or type(event_ns) is not int or event_ns <= 0
                    or not isinstance(currency, str)
                    or not re.fullmatch(r"[A-Z0-9_]{2,16}", currency)
                    or type(row.get("is_taker")) is not bool):
                raise GrvtTestnetEvidenceError("Fill testnet archivado ilegible")
            seen.add(trade_id)
            row_size = amount(row.get("size"), positive=True)
            row_price = amount(row.get("price"), positive=True)
            size += row_size
            notional += row_size * row_price
            pnl += amount(row.get("realized_pnl"), signed=True)
            builder += amount(row.get("builder_fee"))
            fees[currency] = fees.get(currency, Decimal(0)) + amount(row.get("fee"), signed=True)
            last_ns = max(last_ns or 0, event_ns)
        fee_rows = fills.get("fees_by_currency")
        if (not isinstance(fee_rows, list)
                or any(not isinstance(pair, list) or len(pair) != 2
                       or not isinstance(pair[0], str) for pair in fee_rows)
                or len({pair[0] for pair in fee_rows}) != len(fee_rows)):
            raise GrvtTestnetEvidenceError("Comisiones testnet incoherentes")
        archived_fees = {code: amount(value, signed=True) for code, value in fee_rows}
        order_traded = amount(payload["order"].get("traded_size"))
        if (fills["sizes_match"] is not (size == order_traded)
                or notional != amount(fills.get("gross_notional"))
                or pnl != amount(fills.get("realized_pnl"), signed=True)
                or builder != amount(fills.get("builder_fee"))
                or fees != archived_fees):
            raise GrvtTestnetEvidenceError("Resumen de fills testnet incoherente")
        average = (notional / size).quantize(
            Decimal("0.000000000000000001"), rounding=ROUND_HALF_EVEN,
        ) if size else None
    return {"fill_count": len(rows), "filled_size": format(size, "f"),
            "sizes_match_order": size == order_traded,
            "weighted_average_price": format(average, "f") if average is not None else None,
            "gross_notional": format(notional, "f"),
            "fees_by_currency": [[code, format(value, "f")]
                                 for code, value in sorted(fees.items())],
            "realized_pnl_observed": format(pnl, "f"),
            "builder_fee": format(builder, "f"),
            "last_fill_time_ns": last_ns, "causal_proof": False}


def read_testnet_recovery(vault: RealVault,
                          sub_account_id: str) -> GrvtTestnetRecoveryReport:
    """Recompone un estado verificable; un informe jamás libera un nuevo POST."""
    if (not isinstance(sub_account_id, str)
            or not re.fullmatch(r"[0-9]{1,30}", sub_account_id)):
        raise GrvtTestnetJournalError("Subcuenta testnet inválida")
    with vault.session() as db:
        stop = db.get(GrvtTestnetStop, sub_account_id)
        intents = db.scalars(select(GrvtTestnetOrderIntent).where(
            GrvtTestnetOrderIntent.sub_account_id == sub_account_id,
        ).order_by(GrvtTestnetOrderIntent.created_at,
                   GrvtTestnetOrderIntent.client_order_id)).all()
        items: list[GrvtTestnetRecoveryItem] = []
        for intent in intents:
            preflight = db.get(GrvtTestnetPreflightEvidence, intent.client_order_id)
            if preflight is not None and preflight.sub_account_id != sub_account_id:
                raise GrvtTestnetJournalError("Preflight testnet ajeno a la subcuenta")
            preflight_payload = verified_archived_preflight(db,preflight) if preflight is not None else None
            if (preflight_payload is not None
                    and preflight_payload.get("request_digest") != intent.request_digest):
                raise GrvtTestnetPreflightError("Preflight testnet ajeno al intento")
            signal_source = (preflight_payload.get("signal_source")
                             if preflight_payload is not None else None)
            price_guard = (preflight_payload.get("price_guard")
                           if preflight_payload is not None else None)
            cancel = db.get(GrvtTestnetCancelAttempt, intent.client_order_id)
            if cancel is not None and cancel.sub_account_id != sub_account_id:
                raise GrvtTestnetJournalError("Cancelación testnet ajena a la subcuenta")
            audits = db.scalars(select(GrvtTestnetAuditEvidence).where(
                GrvtTestnetAuditEvidence.client_order_id == intent.client_order_id,
            ).order_by(GrvtTestnetAuditEvidence.observed_at,
                       GrvtTestnetAuditEvidence.id)).all()
            current_state_audited = False
            current_findings: tuple[str, ...] = ()
            fill_summary: dict | None = None
            funding_summary: dict | None = None
            for audit in audits:
                if audit.sub_account_id != sub_account_id:
                    raise GrvtTestnetJournalError("Auditoría testnet ajena a la subcuenta")
                payload = _verified_payload(audit)
                observed = payload["order"]
                try:
                    traded = Decimal(observed["traded_size"])
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    raise GrvtTestnetJournalError("Auditoría testnet ilegible") from None
                # Sólo la auditoría más reciente representa el estado
                # archivado actual. Una anterior coincidente no debe tapar
                # otra posterior que lo contradiga.
                current_state_audited = (
                    observed.get("order_id") == intent.external_order_id
                    and observed.get("status") == intent.external_status
                    and observed.get("update_time_ns") == intent.external_update_ns
                    and intent.external_traded_size is not None
                    and traded == intent.external_traded_size
                    and (cancel is None or cancel.status == "AUDITED"
                         and cancel.external_status == intent.external_status
                         and cancel.external_update_ns == intent.external_update_ns)
                )
                current_findings = _findings(payload) if current_state_audited else ()
                fill_summary = _fill_summary(payload) if current_state_audited else None
                funding_summary = _funding_summary(payload) if current_state_audited else None
            reconciliation = None
            can_reconcile = False
            from app.live_execution.grvt_testnet_reconciliation import (
                verified_reconciliation, _calculate, GrvtTestnetReconciliationError)
            if intent.status == "RECONCILED":
                try:
                    reconciliation = verified_reconciliation(db,intent)
                except GrvtTestnetReconciliationError:
                    raise GrvtTestnetJournalError("Conciliación testnet no verificable") from None
            elif intent.status == "OBSERVED" and current_state_audited and len(audits) >= 2:
                try:
                    with localcontext() as context:
                        context.prec = 100
                        _calculate(db,intent,at_ns=time.time_ns())
                    can_reconcile = True
                except GrvtTestnetReconciliationError:
                    pass
            if ((reconciliation is not None or can_reconcile) and funding_summary is not None
                    and funding_summary["linked_to_own_fills"]):
                current_findings = tuple(code for code in current_findings if code != "FUNDING_ACTIVITY")
            items.append(GrvtTestnetRecoveryItem(
                intent.client_order_id, intent.instrument, intent.side,
                intent.size, intent.status, intent.external_status,
                intent.external_traded_size,
                cancel.status if cancel is not None else None,
                cancel.external_status if cancel is not None else None,
                len(audits), current_state_audited,
                current_findings,
                _next_check(intent, cancel, current_state_audited,
                            current_findings),
                signal_source,
                price_guard,
                fill_summary,
                reconciliation,
                can_reconcile,
                preflight_payload.get("history_continuity") if preflight_payload is not None else None,
                funding_summary,
            ))
        return GrvtTestnetRecoveryReport(
            sub_account_id, bool(stop and stop.stopped), tuple(items),
        )


def list_testnet_recovery_subaccounts(vault: RealVault) -> tuple[str, ...]:
    """IDs locales conocidos por el diario; no consulta GRVT ni lee claves."""
    with vault.session() as db:
        known = set(db.scalars(select(GrvtTestnetOrderIntent.sub_account_id)).all())
        known.update(db.scalars(select(GrvtTestnetStop.sub_account_id)).all())
        from app.live_execution.grvt_testnet_bracket_journal import _has_table
        from app.portfolio.models import GrvtTestnetBracketGroup
        if _has_table(db): known.update(db.scalars(select(GrvtTestnetBracketGroup.sub_account_id)).all())
        # Un bot que espera su primera señal también debe poder recuperarse.
        from sqlalchemy import inspect
        from app.portfolio.models import GrvtTestnetBotRun
        if inspect(db.connection()).has_table(GrvtTestnetBotRun.__tablename__):
            known.update(db.scalars(select(GrvtTestnetBotRun.sub_account_id)).all())
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,30}", value)
           for value in known):
        raise GrvtTestnetJournalError("Subcuenta testnet archivada inválida")
    return tuple(sorted(known, key=lambda value: (int(value), value)))
