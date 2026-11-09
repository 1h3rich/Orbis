"""Conciliación acotada de un circuito aislado TESTNET, nunca asientos REAL.

Dos auditorías independientes deben explicar exactamente saldo y posición con
fills propios. No prueba que el proveedor no omita datos ni reconcilia actividad
externa. Una discrepancia mantiene la intención pendiente; jamás borra STOP.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re

from sqlalchemy import select

from app.live_execution.grvt_testnet_evidence import _verified_payload
from app.live_execution.grvt_testnet_funding import linked_testnet_funding
from app.live_execution.grvt_testnet_preflight import verified_archived_preflight
from app.live_execution.policy import _utc
from app.portfolio.models import (GrvtTestnetAuditEvidence, GrvtTestnetCancelAttempt,
    GrvtTestnetOrderIntent, GrvtTestnetPreflightEvidence, GrvtTestnetReconciliation)
from app.portfolio.vault import RealVault


class GrvtTestnetReconciliationError(RuntimeError):
    """El circuito testnet no cuadra o su recibo perdió integridad."""


def _ns(value: datetime) -> int:
    delta = _utc(value, "timestamp") - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _number(raw: object) -> Decimal:
    if not isinstance(raw, str) or not re.fullmatch(r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?", raw):
        raise ValueError("amount")
    return Decimal(raw)


def _positions(rows: object) -> dict[str, Decimal]:
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("positions")
    result = {}
    for row in rows:
        if (not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
                or not re.fullmatch(r"[A-Z0-9]+_[A-Z0-9]+_Perp", row[0]) or row[0] in result):
            raise ValueError("position")
        result[row[0]] = _number(row[1])
    return {name: size for name, size in result.items() if size != 0}


def _encode(payload: dict) -> tuple[str, str]:
    value = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return value, hashlib.sha256(value.encode("ascii")).hexdigest()


def _isolated(payload: dict, start_ns: int, *, strict: bool) -> None:
    fills = payload["fills"]
    if (fills.get("external_rows") != [] or fills.get("external_fill_count") != 0
            or fills.get("external_activity_detected") is not False):
        raise ValueError("external fills")
    for name, count in (("funding", "payment_count"), ("transfers", "transfer_count"),
                        ("withdrawals", "withdrawal_count"), ("deposits", "deposit_count"),
                        ("interest", "payment_count")):
        section = payload[name]
        if ((name != "funding" and section.get("rows") != [])
                or type(section.get(count)) is not int or (name != "funding" and section[count] != 0)
                or name in ("transfers", "withdrawals") and section.get("unrelated_row_count") != 0):
            raise ValueError("external activity")
        if strict:
            until = section.get("observed_until_ns")
            event = payload["account"]["event_time_ns"]
            if (section.get("history_start_ns") != start_ns or type(until) is not int
                    or not int(payload["order"]["update_time_ns"]) <= until <= event + 2_000_000_000
                    or event - until > 5_000_000_000
                    or type(section.get("pages_drained")) is not int
                    or not 1 <= section["pages_drained"] <= 20):
                raise ValueError("coverage")
    if strict and (fills.get("history_start_ns") != start_ns
            or type(fills.get("pages_drained")) is not int or not 1 <= fills["pages_drained"] <= 20):
        raise ValueError("fill coverage")


def _calculate(db, intent: GrvtTestnetOrderIntent, *, at_ns: int) -> dict:
    """Se reejecuta al leer el recibo: el SHA por sí solo no verifica importes."""
    from app.live_execution.grvt_testnet_recovery import _fill_summary

    try:
        if intent.status not in ("OBSERVED", "RECONCILED") or intent.external_status not in ("FILLED", "CANCELLED", "REJECTED"):
            raise ValueError("not terminal")
        pre_row = db.get(GrvtTestnetPreflightEvidence, intent.client_order_id)
        if pre_row is None or pre_row.sub_account_id != intent.sub_account_id:
            raise ValueError("preflight missing")
        pre = verified_archived_preflight(db,pre_row)
        if pre["request_digest"] != intent.request_digest:
            raise ValueError("preflight foreign")
        state = pre["state"]
        if (state["margin_type"] != "SIMPLE_CROSS_MARGIN" or state["sub_account_mode"] != "SINGLE_ASSET_MODE"
                or state["settle_currency"] != "USDT" or not intent.instrument.endswith("_USDT_Perp")):
            raise ValueError("unsupported scope")
        audits = db.scalars(select(GrvtTestnetAuditEvidence).where(
            GrvtTestnetAuditEvidence.client_order_id == intent.client_order_id).order_by(
                GrvtTestnetAuditEvidence.observed_at, GrvtTestnetAuditEvidence.id)).all()
        if len(audits) < 2:
            raise GrvtTestnetReconciliationError("Se requieren dos auditorías independientes separadas al menos 5 s")
        first, last = audits[-2:]
        if (not 5_000_000_000 <= _ns(last.observed_at) - _ns(first.observed_at)
                or not 0 <= at_ns - _ns(last.observed_at) <= 30_000_000_000):
            raise GrvtTestnetReconciliationError("Audita de nuevo: separación mínima 5 s y última lectura de hasta 30 s")
        snapshots = []
        previous_fills = {}
        previous_funding = {}
        for audit in audits:
            if audit.sub_account_id != intent.sub_account_id:
                raise ValueError("foreign audit")
            payload = _verified_payload(audit)
            _isolated(payload, pre["observed_at_ns"], strict=audit in (first, last))
            summary = _fill_summary(payload)
            if summary is None:
                raise ValueError("legacy fills")
            funding = linked_testnet_funding(payload["funding"],payload["fills"]["rows"],
                instrument=intent.instrument,start_ns=pre["observed_at_ns"],
                end_ns=min(payload["funding"].get("observed_until_ns",0),payload["account"]["event_time_ns"]))
            if any(_number(row["amount_paid"]) != 0 for row in funding["rows"]):
                before_size = _positions(state["positions"]).get(intent.instrument,Decimal(0))
                filled_delta = _number(summary["filled_size"]) * (1 if intent.side == "BUY" else -1)
                if before_size * filled_delta >= 0 or abs(filled_delta) > abs(before_size):
                    raise ValueError("funding without owned reduction")
            current_funding = {(row["tx_id"],row["event_time_ns"],row["instrument"],row["currency"]):row
                               for row in funding["rows"]}
            if any(current_funding.get(key) != value for key,value in previous_funding.items()):
                raise ValueError("funding changed")
            previous_funding = current_funding
            current_fills = {row["trade_id"]:row for row in payload["fills"]["rows"]}
            if any(current_fills.get(key) != value for key, value in previous_fills.items()):
                raise ValueError("fill changed")
            previous_fills = current_fills
            if audit not in (first, last):
                continue
            observed = payload["order"]
            if (observed.get("order_id") != intent.external_order_id
                    or observed.get("instrument") != intent.instrument or observed.get("side") != intent.side
                    or _number(observed.get("size")) != intent.size
                    or observed.get("status") != intent.external_status
                    or observed.get("update_time_ns") != intent.external_update_ns
                    or _number(observed.get("traded_size")) != intent.external_traded_size
                    or summary["sizes_match_order"] is not True or _number(summary["builder_fee"]) != 0):
                raise ValueError("order mismatch")
            account, scope = payload["account"], payload["account_scope"]
            event = account["event_time_ns"]
            if (type(event) is not int or not int(intent.external_update_ns) <= event
                    or not -2_000_000_000 <= _ns(audit.observed_at) - event <= 5_000_000_000
                    or account["settle_currency"] != "USDT" or type(account["open_order_count"]) is not int
                    or account["open_order_count"] != 0
                    or scope["margin_type"] != "SIMPLE_CROSS_MARGIN" or scope["sub_account_mode"] != "SINGLE_ASSET_MODE"
                    or not isinstance(scope["other_balances"], list)
                    or any(_number(row[1]) != 0 for row in scope["other_balances"])):
                raise ValueError("account scope")
            balance = _number(account["settle_balance"])
            if _number(account["initial_margin"]) < 0 or _number(account["maintenance_margin"]) < 0:
                raise ValueError("negative margin")
            if _number(account["total_equity"]) != balance + _number(scope["unrealized_pnl"]):
                raise ValueError("equity mismatch")
            before = _positions(state["positions"])
            positions = _positions(account["positions"])
            delta = _number(summary["filled_size"]) * (1 if intent.side == "BUY" else -1)
            expected_positions = dict(before)
            expected_positions[intent.instrument] = before.get(intent.instrument, Decimal(0)) + delta
            expected_positions = {key:value for key,value in expected_positions.items() if value}
            if positions != expected_positions:
                raise ValueError("position mismatch")
            fees = Decimal(0)
            for currency, amount in summary["fees_by_currency"]:
                if currency != "USDT":
                    raise ValueError("fee currency")
                fees += _number(amount)
            reported_pnl = _number(summary["realized_pnl_observed"])
            if before.get(intent.instrument, Decimal(0)) * delta >= 0 and reported_pnl != 0:
                raise ValueError("pnl on increase")
            if any((intent.side == "BUY" and _number(row["price"]) > intent.limit_price) or
                   (intent.side == "SELL" and _number(row["price"]) < intent.limit_price)
                   for row in payload["fills"]["rows"]):
                raise ValueError("fill beyond limit")
            funding_paid = _number(funding["paid_usdt"])
            expected_balance = _number(state["settle_balance"]) + reported_pnl - fees - funding_paid
            if balance != expected_balance:
                raise ValueError("balance mismatch")
            if not positions and (_number(account["initial_margin"]) != 0 or _number(account["maintenance_margin"]) != 0
                                  or _number(scope["unrealized_pnl"]) != 0):
                raise ValueError("flat account margin")
            snapshot = {"settle_balance":format(balance,"f"), "positions":[[name,format(size,"f")] for name,size in sorted(positions.items())],
                        "fees_usdt":format(fees,"f"), "reported_realized_pnl_usdt":format(reported_pnl,"f"),
                        "filled_size":summary["filled_size"]}
            if funding["rows"]:
                snapshot["funding_paid_usdt"] = funding["paid_usdt"]
                snapshot["net_realized_pnl_usdt"] = format(reported_pnl-fees-funding_paid,"f")
            snapshots.append(snapshot)
        if (snapshots[0] != snapshots[1] or json.loads(first.payload_json)["fills"] != json.loads(last.payload_json)["fills"]
                or json.loads(first.payload_json)["funding"]["rows"] != json.loads(last.payload_json)["funding"]["rows"]):
            raise ValueError("unstable audit")
        cancel = db.get(GrvtTestnetCancelAttempt, intent.client_order_id)
        if cancel is not None and (cancel.status != "AUDITED" or cancel.external_status != intent.external_status
                                   or cancel.external_update_ns != intent.external_update_ns):
            raise ValueError("cancel pending")
        return {"schema":("orbis.grvt.testnet.reconciliation.v2" if "funding_paid_usdt" in snapshots[1]
                          else "orbis.grvt.testnet.reconciliation.v1"), "scope":"ISOLATED_TESTNET_BALANCE_POSITION_MATCH",
                "client_order_id":intent.client_order_id,"sub_account_id":intent.sub_account_id,
                "request_digest":intent.request_digest, "preflight_digest":pre_row.payload_digest,
                "audits":[[row.id,row.payload_digest] for row in (first,last)],
                "account_event_time_ns":json.loads(last.payload_json)["account"]["event_time_ns"],
                "result":snapshots[1], "signal_source":pre.get("signal_source"),
                "causal_proof":False, "mainnet_order_sent":False, "real_book_events_created":False}
    except GrvtTestnetReconciliationError:
        raise
    except Exception:
        raise GrvtTestnetReconciliationError("Saldo, posición, fills o cobertura testnet no conciliables; conserva el bloqueo") from None


def verified_reconciliation(db, intent: GrvtTestnetOrderIntent) -> dict:
    row = db.get(GrvtTestnetReconciliation, intent.client_order_id)
    if (row is None or intent.status != "RECONCILED" or row.sub_account_id != intent.sub_account_id
            or intent.updated_at != row.reconciled_at):
        raise GrvtTestnetReconciliationError("Recibo testnet ausente o ajeno")
    with localcontext() as context:
        context.prec = 100
        payload = _calculate(db, intent, at_ns=_ns(row.reconciled_at))
    encoded, digest = _encode(payload)
    if row.payload_json != encoded or row.payload_digest != digest:
        raise GrvtTestnetReconciliationError("Recibo testnet alterado")
    return payload


def require_reconciled_continuity(db, sub_account_id: str, state: dict | None = None,
                                 *, now_ns: int | None = None,historical: bool = False) -> None:
    for receipt in db.scalars(select(GrvtTestnetReconciliation).where(
            GrvtTestnetReconciliation.sub_account_id == sub_account_id)).all():
        original = db.get(GrvtTestnetOrderIntent,receipt.client_order_id)
        if original is None or original.status != "RECONCILED" or original.sub_account_id != sub_account_id:
            raise GrvtTestnetReconciliationError("Recibo testnet huérfano o modificado")
    rows = db.scalars(select(GrvtTestnetOrderIntent).where(
        GrvtTestnetOrderIntent.sub_account_id == sub_account_id,
        GrvtTestnetOrderIntent.status == "RECONCILED").order_by(GrvtTestnetOrderIntent.updated_at,
                                                               GrvtTestnetOrderIntent.client_order_id)).all()
    payload = None
    if historical:
        if now_ns is None: raise ValueError('historical boundary required')
        rows=[row for row in rows if _ns(row.updated_at)<=now_ns]
    for row in rows:
        payload = verified_reconciliation(db, row)
        if now_ns is not None and now_ns < _ns(row.updated_at):
            raise GrvtTestnetReconciliationError("Reloj anterior a la conciliación testnet")
    from app.live_execution.grvt_testnet_oco_continuity import oco_receipts
    from app.live_execution.grvt_testnet_oco_finance import verified_oco_reconciliation
    from app.live_execution.grvt_testnet_oco import _verified as verified_oco_plan, GrvtTestnetOcoError
    try:
        newest = _ns(rows[-1].updated_at) if rows else 0
        for receipt,group in oco_receipts(db,sub_account_id):
            if historical and int(receipt.reconciled_at_ns)>now_ns: continue
            native = verified_oco_reconciliation(db,group,verified_oco_plan(db,group))
            stamp = int(receipt.reconciled_at_ns)
            if now_ns is not None and now_ns < stamp:
                raise GrvtTestnetReconciliationError('Reloj anterior al recibo OCO')
            if stamp > newest:
                payload,newest = native,stamp
    except GrvtTestnetOcoError:
        raise GrvtTestnetReconciliationError('Conciliación OCO no verificable') from None
    from app.live_execution.grvt_testnet_bracket_continuity import bracket_receipts
    from app.live_execution.grvt_testnet_bracket_finance import verified_bracket_reconciliation
    from app.live_execution.grvt_testnet_bracket_journal import _verified as bracket_plan
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    try:
        for receipt,group in bracket_receipts(db,sub_account_id):
            stamp=int(receipt.reconciled_at_ns)
            if historical and stamp>now_ns: continue
            native=verified_bracket_reconciliation(db,group,bracket_plan(db,group))
            if now_ns is not None and now_ns<stamp:
                raise GrvtTestnetReconciliationError('Reloj anterior al recibo OTOCO')
            if stamp>newest: payload,newest=native,stamp
    except GrvtTestnetBracketError:
        raise GrvtTestnetReconciliationError('Conciliación OTOCO no verificable') from None
    if state is not None and payload is not None:
        if (state.get("settle_currency") != "USDT"
                or _positions(state["positions"]) != _positions(payload["result"]["positions"])
                or _number(state["settle_balance"]) != _number(payload["result"]["settle_balance"])):
            raise GrvtTestnetReconciliationError("Saldo o posición cambiaron desde la conciliación testnet")


def reconcile_testnet_order(vault: RealVault, sub_account_id: str, client_order_id: str, *, now: datetime) -> dict:
    now = _utc(now,"now")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            intent = db.get(GrvtTestnetOrderIntent, client_order_id)
            if intent is None or intent.sub_account_id != sub_account_id:
                raise GrvtTestnetReconciliationError("Intento testnet no encontrado")
            if intent.status == "RECONCILED":
                return verified_reconciliation(db, intent)
            with localcontext() as context:
                context.prec = 100
                payload = _calculate(db,intent,at_ns=_ns(now))
            encoded,digest = _encode(payload)
            db.add(GrvtTestnetReconciliation(client_order_id=client_order_id,sub_account_id=sub_account_id,
                reconciled_at=now,payload_json=encoded,payload_digest=digest))
            intent.status = "RECONCILED"
            intent.updated_at = now
            db.commit()
            return payload
        except Exception:
            db.rollback()
            raise
