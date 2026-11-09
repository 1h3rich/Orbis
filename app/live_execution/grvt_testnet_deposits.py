"""Depósitos a la cuenta principal GRVT testnet: contexto, no P/L de subcuenta."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient


_SUB = re.compile(r"[0-9]{1,30}\Z")
_CURRENCY = re.compile(r"[A-Z0-9]{2,16}\Z")
_NUMBER = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")
_HASH = re.compile(r"0x[a-fA-F0-9]{8,128}\Z")


class GrvtTestnetDepositError(RuntimeError):
    """Error saneado; no expone direcciones ni hashes externos."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetDeposit:
    deposit_ref: str  # Seudónimo determinista del hash L1; no hash bruto.
    initiated_time_ns: int
    confirmed_time_ns: int | None
    currency: str
    amount: Decimal
    status: str  # PENDING o CONFIRMED.


@dataclass(frozen=True, slots=True)
class GrvtTestnetDepositAudit:
    sub_account_id: str
    history_start_ns: int
    observed_until_ns: int
    pages_drained: int
    deposits: tuple[GrvtTestnetDeposit, ...]
    latest_deposit_time_ns: int | None
    attributed_to_sub_account: bool = False
    complete_for_execution: bool = False


def audit_testnet_deposits(client: GrvtTestnetOrderClient,
                           sub_account_id: str, *, start_time_ns: int,
                           now_ns: int) -> GrvtTestnetDepositAudit:
    """Agota el historial de la cuenta principal, sin inferir transferencias.

    La API puede devolver una dirección de origen y hashes de cadena: nunca se
    conservan. La coincidencia del destinatario se comprueba en memoria contra
    la sesión autenticada. Sólo una transferencia posterior permite relacionar
    estos fondos con la subcuenta; aquí no se realiza tal atribución.
    """
    if (not isinstance(sub_account_id, str) or not _SUB.fullmatch(sub_account_id)
            or type(start_time_ns) is not int or type(now_ns) is not int
            or not 1 < start_time_ns <= now_ns):
        raise GrvtTestnetDepositError("Ventana de depósitos testnet inválida")
    cursor = ""
    seen_cursors: set[str] = set()
    seen_refs: set[str] = set()
    deposits: list[GrvtTestnetDeposit] = []
    latest: int | None = None
    for page_number in range(1, 21):
        try:
            page = client.read_deposit_history(
                sub_account_id, str(start_time_ns - 1), str(now_ns), cursor,
            )
        except Exception:
            raise GrvtTestnetDepositError("Depósitos testnet no verificables") from None
        rows, following = page.get("result"), page.get("next")
        if (not isinstance(rows, list) or len(rows) > 500
                or not isinstance(following, str) or len(following) > 1024
                or not following.isascii() or any(ord(char) < 32 for char in following)):
            raise GrvtTestnetDepositError("Depósitos testnet incompletos")
        for row in rows:
            if not isinstance(row, dict):
                raise GrvtTestnetDepositError("Depósito testnet malformado")
            if not client.matches_main_account_id(row.get("to_account_id")):
                raise GrvtTestnetDepositError("Destino de depósito testnet distinto")
            l1_hash = row.get("l_1_hash")
            initiated_raw = row.get("initiated_time")
            confirmed_raw = row.get("confirmed_time")
            currency, amount_raw = row.get("currency"), row.get("num_tokens")
            if (not isinstance(l1_hash, str) or not _HASH.fullmatch(l1_hash)
                    or not isinstance(initiated_raw, str) or not initiated_raw.isdecimal()
                    or not start_time_ns <= int(initiated_raw) <= now_ns + 2_000_000_000
                    or not isinstance(confirmed_raw, str)
                    or confirmed_raw and not confirmed_raw.isdecimal()
                    or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
                    or not isinstance(amount_raw, str) or not _NUMBER.fullmatch(amount_raw)):
                raise GrvtTestnetDepositError("Depósito testnet malformado")
            initiated_ns = int(initiated_raw)
            confirmed_ns = int(confirmed_raw) if confirmed_raw else None
            if (confirmed_ns is not None and not initiated_ns <= confirmed_ns <= now_ns + 2_000_000_000):
                raise GrvtTestnetDepositError("Confirmación de depósito testnet inválida")
            try:
                amount = Decimal(amount_raw)
            except InvalidOperation:
                raise GrvtTestnetDepositError("Importe de depósito testnet inválido") from None
            if not amount.is_finite() or amount <= 0:
                raise GrvtTestnetDepositError("Importe de depósito testnet inválido")
            ref = hashlib.sha256(l1_hash.lower().encode("ascii")).hexdigest()
            if ref in seen_refs:
                raise GrvtTestnetDepositError("Depósito testnet duplicado")
            seen_refs.add(ref)
            deposits.append(GrvtTestnetDeposit(
                ref, initiated_ns, confirmed_ns, currency, amount,
                "CONFIRMED" if confirmed_ns is not None else "PENDING",
            ))
            latest = max(latest or 0, confirmed_ns or initiated_ns)
        if not following:
            return GrvtTestnetDepositAudit(
                sub_account_id, start_time_ns, now_ns, page_number,
                tuple(sorted(deposits, key=lambda row: (
                    row.initiated_time_ns, row.deposit_ref))), latest,
            )
        if following == cursor or following in seen_cursors:
            raise GrvtTestnetDepositError("Cursor de depósitos testnet repetido")
        seen_cursors.add(following)
        cursor = following
    raise GrvtTestnetDepositError("Depósitos testnet exceden límite verificable")
