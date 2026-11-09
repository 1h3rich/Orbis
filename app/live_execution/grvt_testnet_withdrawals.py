"""Auditoría de retiros GRVT testnet sin direcciones, firmas ni hashes on-chain."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient


_SUB = re.compile(r"[0-9]{1,30}\Z")
_ID = re.compile(r"[A-Za-z0-9_:\-]{1,128}\Z")
_CURRENCY = re.compile(r"[A-Z0-9]{2,16}\Z")
_NUMBER = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")


class GrvtTestnetWithdrawalError(RuntimeError):
    """Historial no verificable; nunca incluye datos externos en el mensaje."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetWithdrawal:
    tx_id: str
    event_time_ns: int
    currency: str
    amount: Decimal
    status: str  # PENDING o FINALIZED; no implica saldo conciliado.


@dataclass(frozen=True, slots=True)
class GrvtTestnetWithdrawalAudit:
    sub_account_id: str
    history_start_ns: int
    observed_until_ns: int
    pages_drained: int
    withdrawals: tuple[GrvtTestnetWithdrawal, ...]
    unrelated_row_count: int
    latest_withdrawal_time_ns: int | None
    complete_for_execution: bool = False


def audit_testnet_withdrawals(client: GrvtTestnetOrderClient,
                              sub_account_id: str, *, start_time_ns: int,
                              now_ns: int) -> GrvtTestnetWithdrawalAudit:
    """Agota el historial de la cuenta principal y selecciona la subcuenta.

    Un retiro pendiente puede finalizar entre auditorías; el archivo de
    evidencia permite sólo esa transición de estado, nunca su desaparición.
    """
    if (not isinstance(sub_account_id, str) or not _SUB.fullmatch(sub_account_id)
            or type(start_time_ns) is not int or type(now_ns) is not int
            or not 1 < start_time_ns <= now_ns):
        raise GrvtTestnetWithdrawalError("Ventana de retiros testnet inválida")
    cursor = ""
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    rows_out: list[GrvtTestnetWithdrawal] = []
    unrelated = 0
    latest: int | None = None
    for page_number in range(1, 21):
        try:
            page = client.read_withdrawal_history(
                sub_account_id, str(start_time_ns - 1), str(now_ns), cursor,
            )
        except Exception:
            raise GrvtTestnetWithdrawalError("Retiros testnet no verificables") from None
        rows, following = page.get("result"), page.get("next")
        if (not isinstance(rows, list) or len(rows) > 500
                or not isinstance(following, str) or len(following) > 1024
                or not following.isascii() or any(ord(char) < 32 for char in following)):
            raise GrvtTestnetWithdrawalError("Retiros testnet incompletos")
        for row in rows:
            if not isinstance(row, dict):
                raise GrvtTestnetWithdrawalError("Retiro testnet malformado")
            owner = row.get("from_account_id")
            if not isinstance(owner, str) or not _SUB.fullmatch(owner):
                raise GrvtTestnetWithdrawalError("Subcuenta de retiro inválida")
            if owner != sub_account_id:
                unrelated += 1
                continue
            tx_id, event_raw = row.get("tx_id"), row.get("event_time")
            currency, amount_raw = row.get("currency"), row.get("num_tokens")
            l1_hash = row.get("l_1_hash", "")
            if (not isinstance(tx_id, str) or not _ID.fullmatch(tx_id)
                    or tx_id in seen_ids
                    or not isinstance(event_raw, str) or not event_raw.isdecimal()
                    or not start_time_ns <= int(event_raw) <= now_ns + 2_000_000_000
                    or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
                    or not isinstance(amount_raw, str) or not _NUMBER.fullmatch(amount_raw)
                    or not isinstance(l1_hash, str)):
                raise GrvtTestnetWithdrawalError("Retiro testnet malformado o duplicado")
            try:
                amount = Decimal(amount_raw)
            except InvalidOperation:
                raise GrvtTestnetWithdrawalError("Importe de retiro testnet inválido") from None
            if not amount.is_finite() or amount <= 0:
                raise GrvtTestnetWithdrawalError("Importe de retiro testnet inválido")
            seen_ids.add(tx_id)
            event_ns = int(event_raw)
            rows_out.append(GrvtTestnetWithdrawal(
                tx_id, event_ns, currency, amount,
                "FINALIZED" if l1_hash else "PENDING",
            ))
            latest = max(latest or 0, event_ns)
        if not following:
            return GrvtTestnetWithdrawalAudit(
                sub_account_id, start_time_ns, now_ns, page_number,
                tuple(sorted(rows_out, key=lambda row: (row.event_time_ns, row.tx_id))),
                unrelated, latest,
            )
        if following == cursor or following in seen_cursors:
            raise GrvtTestnetWithdrawalError("Cursor de retiros testnet repetido")
        seen_cursors.add(following)
        cursor = following
    raise GrvtTestnetWithdrawalError("Retiros testnet exceden límite verificable")
