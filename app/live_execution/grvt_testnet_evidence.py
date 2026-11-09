"""Evidencia cifrada GRVT testnet sin asientos ni cierre de reserva."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select

from app.live_execution.grvt_testnet_account import GrvtTestnetAccountSnapshot
from app.live_execution.grvt_testnet_fills import GrvtTestnetFillAudit
from app.live_execution.grvt_testnet_funding import GrvtTestnetFundingAudit
from app.live_execution.grvt_testnet_transfers import GrvtTestnetTransferAudit
from app.live_execution.grvt_testnet_withdrawals import GrvtTestnetWithdrawalAudit
from app.live_execution.grvt_testnet_deposits import GrvtTestnetDepositAudit
from app.live_execution.grvt_testnet_interest import GrvtTestnetInterestAudit
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderObservation
from app.live_execution.grvt_testnet_preflight import _verified as _verified_preflight
from app.live_execution.policy import _utc
from app.portfolio.models import (
    GrvtTestnetAuditEvidence, GrvtTestnetOrderIntent,
    GrvtTestnetPreflightEvidence,
)
from app.portfolio.vault import RealVault


class GrvtTestnetEvidenceError(RuntimeError):
    """Evidencia incompleta, alterada o fuera de la intención."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetEvidenceRecord:
    id: str
    client_order_id: str
    payload_digest: str
    observed_at: datetime
    complete_for_execution: bool = False


def _decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise GrvtTestnetEvidenceError("Importe testnet no verificable")
    return format(value, "f")


def _payload(order: GrvtTestnetOrderObservation,
             fills: GrvtTestnetFillAudit,
             funding: GrvtTestnetFundingAudit,
             transfers: GrvtTestnetTransferAudit,
             withdrawals: GrvtTestnetWithdrawalAudit,
             deposits: GrvtTestnetDepositAudit,
             interest: GrvtTestnetInterestAudit,
             account: GrvtTestnetAccountSnapshot) -> dict:
    if (not isinstance(order, GrvtTestnetOrderObservation)
            or not isinstance(fills, GrvtTestnetFillAudit)
            or not isinstance(funding, GrvtTestnetFundingAudit)
            or not isinstance(transfers, GrvtTestnetTransferAudit)
            or not isinstance(withdrawals, GrvtTestnetWithdrawalAudit)
            or not isinstance(deposits, GrvtTestnetDepositAudit)
            or not isinstance(interest, GrvtTestnetInterestAudit)
            or not isinstance(account, GrvtTestnetAccountSnapshot)
            or fills.client_order_id != order.client_order_id
            or fills.order_id != order.order_id
            or account.sub_account_id != order.sub_account_id
            or funding.sub_account_id != order.sub_account_id
            or transfers.sub_account_id != order.sub_account_id
            or withdrawals.sub_account_id != order.sub_account_id
            or deposits.sub_account_id != order.sub_account_id
            or interest.sub_account_id != order.sub_account_id
            or funding.history_start_ns != fills.history_start_ns
            or transfers.history_start_ns != fills.history_start_ns
            or withdrawals.history_start_ns != fills.history_start_ns
            or deposits.history_start_ns != fills.history_start_ns
            or interest.history_start_ns != fills.history_start_ns
            or deposits.attributed_to_sub_account
            or fills.fill_count != len(fills.fills)
            or fills.complete_for_execution or account.complete_for_execution):
        raise GrvtTestnetEvidenceError("Auditoría testnet no vinculada")
    fill_rows = [
        {"trade_id": fill.trade_id, "event_time_ns": fill.event_time_ns,
         "size": _decimal(fill.size), "price": _decimal(fill.price),
         "fee": _decimal(fill.fee), "fee_currency": fill.fee_currency,
         "realized_pnl": _decimal(fill.realized_pnl),
         "builder_fee": _decimal(fill.builder_fee), "is_taker": fill.is_taker}
        for fill in fills.fills
    ]
    external_rows = [
        {"trade_id": fill.trade_id, "order_id": fill.order_id,
         "client_order_id": fill.client_order_id,
         "event_time_ns": fill.event_time_ns,
         "instrument": fill.instrument, "is_buyer": fill.is_buyer,
         "size": _decimal(fill.size), "price": _decimal(fill.price),
         "fee": _decimal(fill.fee), "fee_currency": fill.fee_currency,
         "realized_pnl": _decimal(fill.realized_pnl),
         "builder_fee": _decimal(fill.builder_fee),
         "is_liquidation": fill.is_liquidation}
        for fill in fills.other_fills
    ]
    funding_rows = [
        {"tx_id": payment.tx_id, "event_time_ns": payment.event_time_ns,
         "instrument": payment.instrument, "currency": payment.currency,
         "amount_paid": _decimal(payment.amount_paid)}
        for payment in funding.payments
    ]
    transfer_rows = [
        {"tx_id": transfer.tx_id, "event_time_ns": transfer.event_time_ns,
         "currency": transfer.currency, "amount": _decimal(transfer.amount),
         "direction": transfer.direction}
        for transfer in transfers.transfers
    ]
    withdrawal_rows = [
        {"tx_id": withdrawal.tx_id,
         "event_time_ns": withdrawal.event_time_ns,
         "currency": withdrawal.currency,
         "amount": _decimal(withdrawal.amount),
         "status": withdrawal.status}
        for withdrawal in withdrawals.withdrawals
    ]
    deposit_rows = [
        {"deposit_ref": deposit.deposit_ref,
         "initiated_time_ns": deposit.initiated_time_ns,
         "confirmed_time_ns": deposit.confirmed_time_ns,
         "currency": deposit.currency,
         "amount": _decimal(deposit.amount),
         "status": deposit.status}
        for deposit in deposits.deposits
    ]
    interest_rows = [
        {"event_time_ns": payment.event_time_ns,
         "charge_time_ns": payment.charge_time_ns,
         "currency": payment.currency,
         "amount_reported": _decimal(payment.amount_reported),
         "borrowed_amount": _decimal(payment.borrowed_amount)}
        for payment in interest.payments
    ]
    if (len({row["trade_id"] for row in fill_rows}) != len(fill_rows)
            or len({row["trade_id"] for row in fill_rows + external_rows})
            != len(fill_rows) + len(external_rows)
            or sum((Decimal(row["size"]) for row in fill_rows), Decimal(0)) != fills.filled_size
            or (max((row["event_time_ns"] for row in fill_rows), default=None)
                != fills.latest_fill_time_ns)
            or fills.sizes_match != (fills.filled_size == fills.order_traded_size)):
        raise GrvtTestnetEvidenceError("Fills testnet incoherentes")
    if (len({(row["tx_id"], row["event_time_ns"], row["instrument"], row["currency"])
             for row in funding_rows}) != len(funding_rows)
            or max((row["event_time_ns"] for row in funding_rows), default=None)
            != funding.latest_payment_time_ns):
        raise GrvtTestnetEvidenceError("Financiación testnet incoherente")
    if (len({(row["tx_id"], row["event_time_ns"], row["currency"], row["direction"])
             for row in transfer_rows}) != len(transfer_rows)
            or max((row["event_time_ns"] for row in transfer_rows), default=None)
            != transfers.latest_transfer_time_ns):
        raise GrvtTestnetEvidenceError("Transferencias testnet incoherentes")
    if (len({row["tx_id"] for row in withdrawal_rows}) != len(withdrawal_rows)
            or max((row["event_time_ns"] for row in withdrawal_rows), default=None)
            != withdrawals.latest_withdrawal_time_ns
            or any(row["status"] not in ("PENDING", "FINALIZED")
                   for row in withdrawal_rows)):
        raise GrvtTestnetEvidenceError("Retiros testnet incoherentes")
    if (len({row["deposit_ref"] for row in deposit_rows}) != len(deposit_rows)
            or max((row["confirmed_time_ns"] or row["initiated_time_ns"]
                    for row in deposit_rows), default=None)
            != deposits.latest_deposit_time_ns
            or any((row["status"] == "PENDING") !=
                   (row["confirmed_time_ns"] is None) for row in deposit_rows)):
        raise GrvtTestnetEvidenceError("Depósitos testnet incoherentes")
    interest_totals: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = 100
        for row in interest_rows:
            code = row["currency"]
            interest_totals[code] = (interest_totals.get(code, Decimal(0))
                                     + Decimal(row["amount_reported"]))
    if (len({(row["event_time_ns"], row["charge_time_ns"], row["currency"])
             for row in interest_rows}) != len(interest_rows)
            or max((row["event_time_ns"] for row in interest_rows), default=None)
            != interest.latest_payment_time_ns
            or len(dict(interest.reported_by_currency)) != len(interest.reported_by_currency)
            or interest_totals != dict(interest.reported_by_currency)):
        raise GrvtTestnetEvidenceError("Interés testnet incoherente")
    return {
        "schema": "orbis.grvt.testnet.audit.v1",
        "order": {"sub_account_id": order.sub_account_id,
                  "client_order_id": order.client_order_id,
                  "order_id": order.order_id, "status": order.status,
                  "instrument": order.instrument, "side": order.side,
                  "size": order.size, "traded_size": order.traded_size,
                  "update_time_ns": order.update_time_ns},
        "fills": {"count": fills.fill_count,
                  "history_start_ns": fills.history_start_ns,
                  "external_fill_count": len(external_rows),
                  "external_activity_detected": bool(external_rows),
                  "sizes_match": fills.sizes_match,
                  "pages_drained": fills.pages_drained,
                  "gross_notional": _decimal(fills.gross_notional),
                  "realized_pnl": _decimal(fills.realized_pnl),
                  "builder_fee": _decimal(fills.builder_fee),
                  "fees_by_currency": [[code, _decimal(amount)]
                                       for code, amount in fills.fees_by_currency],
                  "rows": fill_rows,
                  "external_rows": external_rows},
        "funding": {"history_start_ns": funding.history_start_ns,
                    "observed_until_ns": funding.observed_until_ns,
                    "pages_drained": funding.pages_drained,
                    "payment_count": len(funding_rows),
                    "paid_by_currency": [[code, _decimal(amount)]
                                         for code, amount in funding.paid_by_currency],
                    "rows": funding_rows},
        "transfers": {"history_start_ns": transfers.history_start_ns,
                      "observed_until_ns": transfers.observed_until_ns,
                      "pages_drained": transfers.pages_drained,
                      "transfer_count": len(transfer_rows),
                      "unrelated_row_count": transfers.unrelated_row_count,
                      "net_by_currency": [[code, _decimal(amount)]
                                          for code, amount in transfers.net_by_currency],
                      "rows": transfer_rows},
        "withdrawals": {"history_start_ns": withdrawals.history_start_ns,
                        "observed_until_ns": withdrawals.observed_until_ns,
                        "pages_drained": withdrawals.pages_drained,
                        "withdrawal_count": len(withdrawal_rows),
                        "unrelated_row_count": withdrawals.unrelated_row_count,
                        "pending_count": sum(row["status"] == "PENDING"
                                             for row in withdrawal_rows),
                        "rows": withdrawal_rows},
        "deposits": {"history_start_ns": deposits.history_start_ns,
                     "observed_until_ns": deposits.observed_until_ns,
                     "pages_drained": deposits.pages_drained,
                     "deposit_count": len(deposit_rows),
                     "pending_count": sum(row["status"] == "PENDING"
                                          for row in deposit_rows),
                     "attributed_to_sub_account": False,
                     "rows": deposit_rows},
        "interest": {"history_start_ns": interest.history_start_ns,
                     "observed_until_ns": interest.observed_until_ns,
                     "pages_drained": interest.pages_drained,
                     "payment_count": len(interest_rows),
                     "reported_by_currency": [[code, _decimal(amount)]
                                              for code, amount in interest.reported_by_currency],
                     "sign_semantics_verified": False,
                     "rows": interest_rows},
        "account": {"event_time_ns": account.event_time_ns,
                    "settle_currency": account.settle_currency,
                    "settle_balance": _decimal(account.settle_balance),
                    "total_equity": _decimal(account.total_equity),
                    "initial_margin": _decimal(account.initial_margin),
                    "maintenance_margin": _decimal(account.maintenance_margin),
                    "positions": [[name, _decimal(size)]
                                  for name, size in account.positions],
                    "open_order_count": account.open_order_count},
        "account_scope": {"margin_type": account.margin_type,
                          "sub_account_mode": account.sub_account_mode,
                          "other_balances": ([[code, _decimal(value)] for code, value in account.other_balances]
                                             if account.other_balances is not None else None),
                          "unrealized_pnl": (_decimal(account.unrealized_pnl)
                                             if account.unrealized_pnl is not None else None)},
        "complete_for_execution": False,
    }


def _encode(payload: dict) -> tuple[str, str]:
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return data, hashlib.sha256(data.encode("ascii")).hexdigest()


def _verified_payload(row: GrvtTestnetAuditEvidence) -> dict:
    try:
        digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
    except (AttributeError, UnicodeEncodeError):
        raise GrvtTestnetEvidenceError("Evidencia testnet alterada") from None
    if digest != row.payload_digest:
        raise GrvtTestnetEvidenceError("Evidencia testnet alterada")
    try:
        payload = json.loads(row.payload_json)
    except (ValueError, TypeError):
        raise GrvtTestnetEvidenceError("Evidencia testnet ilegible") from None
    if (not isinstance(payload, dict)
            or payload.get("schema") != "orbis.grvt.testnet.audit.v1"
            or not isinstance(payload.get("order"), dict)
            or payload["order"].get("client_order_id") != row.client_order_id
            or payload["order"].get("sub_account_id") != row.sub_account_id
            or not isinstance(payload.get("fills"), dict)
            or not isinstance(payload["fills"].get("rows"), list)):
        raise GrvtTestnetEvidenceError("Evidencia testnet incoherente")
    return payload


def save_testnet_audit(vault: RealVault,
                       order: GrvtTestnetOrderObservation,
                       fills: GrvtTestnetFillAudit,
                       funding: GrvtTestnetFundingAudit,
                       transfers: GrvtTestnetTransferAudit,
                       withdrawals: GrvtTestnetWithdrawalAudit,
                       deposits: GrvtTestnetDepositAudit,
                       interest: GrvtTestnetInterestAudit,
                       account: GrvtTestnetAccountSnapshot, *,
                       now: datetime) -> GrvtTestnetEvidenceRecord:
    """Archiva una lectura saneada. No cambia el estado del intento."""
    now = _utc(now, "now")
    payload = _payload(order, fills, funding, transfers, withdrawals,
                       deposits, interest, account)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            intent = db.get(GrvtTestnetOrderIntent, order.client_order_id)
            if (intent is None or intent.status != "OBSERVED"
                    or intent.sub_account_id != order.sub_account_id
                    or intent.instrument != order.instrument
                    or intent.side != order.side
                    or intent.size != Decimal(order.size)
                    or intent.external_order_id != order.order_id
                    or intent.external_status != order.status
                    or intent.external_update_ns != order.update_time_ns
                    or intent.external_traded_size != Decimal(order.traded_size)):
                raise GrvtTestnetEvidenceError("Evidencia ajena al intento testnet")
            preflight_row = db.get(GrvtTestnetPreflightEvidence,
                                   order.client_order_id)
            if preflight_row is None:
                # Intentos anteriores a v12 no tienen una base para comparar.
                payload["comparison"] = {"available": False,
                                         "causal_proof": False}
            else:
                before = _verified_preflight(preflight_row)
                if before.get("request_digest") != intent.request_digest:
                    raise GrvtTestnetEvidenceError("Preflight testnet ajeno al intento")
                try:
                    prior_state = before["state"]
                    prior_positions = {name: Decimal(size)
                                       for name, size in prior_state["positions"]}
                    if len(prior_positions) != len(prior_state["positions"]):
                        raise ValueError("duplicate positions")
                    current_positions = dict(account.positions)
                    instrument = order.instrument
                    actual_delta = (current_positions.get(instrument, Decimal(0))
                                    - prior_positions.get(instrument, Decimal(0)))
                    expected_delta = (fills.filled_size if order.side == "BUY"
                                      else -fills.filled_size)
                    other_positions_match = all(
                        prior_positions.get(name, Decimal(0))
                        == current_positions.get(name, Decimal(0))
                        for name in set(prior_positions) | set(current_positions)
                        if name != instrument
                    )
                    balance_delta = account.settle_balance - Decimal(
                        prior_state["settle_balance"])
                    funding_paid_settle = dict(funding.paid_by_currency).get(
                        account.settle_currency, Decimal(0))
                    transfer_net_settle = dict(transfers.net_by_currency).get(
                        account.settle_currency, Decimal(0))
                    withdrawal_out_settle = sum(
                        (row.amount for row in withdrawals.withdrawals
                         if row.currency == account.settle_currency), Decimal(0))
                    if not balance_delta.is_finite():
                        raise ValueError("nonfinite balance")
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    raise GrvtTestnetEvidenceError("Preflight testnet incoherente") from None
                payload["comparison"] = {
                    "available": True,
                    "preflight_digest": preflight_row.payload_digest,
                    "position_delta": _decimal(actual_delta),
                    "expected_fill_delta": _decimal(expected_delta),
                    "position_delta_matches": actual_delta == expected_delta
                                              and other_positions_match,
                    "settle_balance_delta": _decimal(balance_delta),
                    "funding_paid_settle": _decimal(funding_paid_settle),
                    "transfer_net_settle": _decimal(transfer_net_settle),
                    "withdrawal_out_settle_observed": _decimal(withdrawal_out_settle),
                    "balance_delta_excluding_known_funding": _decimal(
                        balance_delta + funding_paid_settle),
                    "balance_delta_excluding_known_flows": _decimal(
                        balance_delta + funding_paid_settle - transfer_net_settle),
                    "external_fill_count": len(fills.other_fills),
                    "causal_proof": False,
                }
            data, digest = _encode(payload)
            rows = db.scalars(select(GrvtTestnetAuditEvidence).where(
                GrvtTestnetAuditEvidence.client_order_id == order.client_order_id,
            ).order_by(GrvtTestnetAuditEvidence.observed_at,
                       GrvtTestnetAuditEvidence.id)).all()
            matching_row: GrvtTestnetAuditEvidence | None = None
            for row in rows:
                previous = _verified_payload(row)
                old_fills = {fill["trade_id"]: fill
                             for fill in previous["fills"]["rows"]}
                new_fills = {fill["trade_id"]: fill
                             for fill in payload["fills"]["rows"]}
                old_external = {fill["trade_id"]: fill
                                for fill in previous["fills"].get("external_rows", [])}
                new_external = {fill["trade_id"]: fill
                                for fill in payload["fills"]["external_rows"]}
                old_funding = {
                    (payment["tx_id"], payment["event_time_ns"],
                     payment["instrument"], payment["currency"]): payment
                    for payment in previous.get("funding", {}).get("rows", [])
                }
                new_funding = {
                    (payment["tx_id"], payment["event_time_ns"],
                     payment["instrument"], payment["currency"]): payment
                    for payment in payload["funding"]["rows"]
                }
                old_transfers = {
                    (transfer["tx_id"], transfer["event_time_ns"],
                     transfer["currency"], transfer["direction"]): transfer
                    for transfer in previous.get("transfers", {}).get("rows", [])
                }
                new_transfers = {
                    (transfer["tx_id"], transfer["event_time_ns"],
                     transfer["currency"], transfer["direction"]): transfer
                    for transfer in payload["transfers"]["rows"]
                }
                old_withdrawals = {
                    withdrawal["tx_id"]: withdrawal
                    for withdrawal in previous.get("withdrawals", {}).get("rows", [])
                }
                new_withdrawals = {
                    withdrawal["tx_id"]: withdrawal
                    for withdrawal in payload["withdrawals"]["rows"]
                }
                withdrawal_changed = any(
                    (new_withdrawals.get(tx_id) != old
                     and not (new_withdrawals.get(tx_id) == {
                         **old, "status": "FINALIZED"}
                         and old.get("status") == "PENDING"))
                    for tx_id, old in old_withdrawals.items()
                )
                old_deposits = {
                    deposit["deposit_ref"]: deposit
                    for deposit in previous.get("deposits", {}).get("rows", [])
                }
                new_deposits = {
                    deposit["deposit_ref"]: deposit
                    for deposit in payload["deposits"]["rows"]
                }
                deposit_changed = any(
                    (new_deposits.get(ref) != old
                     and not (old.get("status") == "PENDING"
                              and old.get("confirmed_time_ns") is None
                              and new_deposits.get(ref, {}).get("status") == "CONFIRMED"
                              and new_deposits[ref].get("confirmed_time_ns") is not None
                              and all(new_deposits[ref].get(key) == old.get(key)
                                      for key in ("deposit_ref", "initiated_time_ns",
                                                  "currency", "amount"))))
                    for ref, old in old_deposits.items()
                )
                old_interest = {
                    (payment["event_time_ns"], payment["charge_time_ns"],
                     payment["currency"]): payment
                    for payment in previous.get("interest", {}).get("rows", [])
                }
                new_interest = {
                    (payment["event_time_ns"], payment["charge_time_ns"],
                     payment["currency"]): payment
                    for payment in payload["interest"]["rows"]
                }
                if any(new_fills.get(trade_id) != old
                       for trade_id, old in old_fills.items()) or any(
                           new_external.get(trade_id) != old
                           for trade_id, old in old_external.items()) or any(
                               new_funding.get(key) != old
                               for key, old in old_funding.items()) or any(
                                   new_transfers.get(key) != old
                                   for key, old in old_transfers.items()) or withdrawal_changed or deposit_changed or any(
                                       new_interest.get(key) != old
                                       for key, old in old_interest.items()):
                    raise GrvtTestnetEvidenceError("Actividad testnet archivada: datos cambiaron o desaparecieron")
                if row.payload_digest == digest:
                    matching_row = row
            if matching_row is not None:
                result = GrvtTestnetEvidenceRecord(
                    matching_row.id, matching_row.client_order_id,
                    digest, matching_row.observed_at)
                db.commit()
                return result
            row = GrvtTestnetAuditEvidence(
                id=str(uuid4()), client_order_id=order.client_order_id,
                sub_account_id=order.sub_account_id, observed_at=now,
                payload_json=data, payload_digest=digest,
            )
            db.add(row)
            db.commit()
            return GrvtTestnetEvidenceRecord(row.id, order.client_order_id,
                                             digest, now)
        except Exception:
            db.rollback()
            raise


def list_testnet_audits(vault: RealVault, sub_account_id: str,
                        client_order_id: str) -> tuple[dict, ...]:
    """Reabre evidencia cifrada y verifica huellas; no devuelve secretos."""
    with vault.session() as db:
        rows = db.scalars(select(GrvtTestnetAuditEvidence).where(
            GrvtTestnetAuditEvidence.sub_account_id == sub_account_id,
            GrvtTestnetAuditEvidence.client_order_id == client_order_id,
        ).order_by(GrvtTestnetAuditEvidence.observed_at,
                   GrvtTestnetAuditEvidence.id)).all()
        return tuple(_verified_payload(row) for row in rows)
