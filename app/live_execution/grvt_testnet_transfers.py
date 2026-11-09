"""Transferencias internas GRVT testnet, saneadas y sin direcciones."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient


_NUMBER = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")
_ID = re.compile(r"[A-Za-z0-9_:\-]{1,128}\Z")
_SUB = re.compile(r"[0-9]{1,30}\Z")
_CURRENCY = re.compile(r"[A-Z0-9]{2,16}\Z")


class GrvtTestnetTransferError(RuntimeError):
    """Historial testnet no verificable; nunca imprime direcciones."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetTransfer:
    tx_id: str
    event_time_ns: int
    currency: str
    amount: Decimal
    direction: str  # IN, OUT, SELF respecto a la subcuenta auditada.


@dataclass(frozen=True, slots=True)
class GrvtTestnetTransferAudit:
    sub_account_id: str
    history_start_ns: int
    observed_until_ns: int
    pages_drained: int
    transfers: tuple[GrvtTestnetTransfer, ...]
    net_by_currency: tuple[tuple[str, Decimal], ...]
    unrelated_row_count: int
    latest_transfer_time_ns: int | None
    complete_for_execution: bool = False


def _amount(raw: object) -> Decimal:
    if not isinstance(raw, str) or not _NUMBER.fullmatch(raw):
        raise GrvtTestnetTransferError("Importe de transferencia testnet inválido")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise GrvtTestnetTransferError("Importe de transferencia testnet inválido") from None
    if not value.is_finite() or value <= 0:
        raise GrvtTestnetTransferError("Importe de transferencia testnet inválido")
    return value


def audit_testnet_transfers(client: GrvtTestnetOrderClient,
                            sub_account_id: str, *,
                            start_time_ns: int, now_ns: int,
                            ) -> GrvtTestnetTransferAudit:
    """Agota páginas del main account y retiene sólo movimientos de la subcuenta.

    No consulta depósitos/retiros on-chain ni guarda cuentas, firmas o metadata.
    """
    if (not isinstance(sub_account_id, str) or not _SUB.fullmatch(sub_account_id)
            or type(start_time_ns) is not int or type(now_ns) is not int
            or not 1 < start_time_ns <= now_ns):
        raise GrvtTestnetTransferError("Ventana de transferencias testnet inválida")
    cursor = ""
    seen_cursors: set[str] = set()
    seen: set[tuple[str, int, str, str]] = set()
    transfers: list[GrvtTestnetTransfer] = []
    net: dict[str, Decimal] = {}
    unrelated = 0
    latest: int | None = None
    for page_number in range(1, 21):
        try:
            page = client.read_transfer_history(
                sub_account_id, str(start_time_ns - 1), str(now_ns), cursor,
            )
        except Exception:
            raise GrvtTestnetTransferError("Transferencias testnet no verificables") from None
        rows, following = page.get("result"), page.get("next")
        if (not isinstance(rows, list) or len(rows) > 500
                or not isinstance(following, str) or len(following) > 1024
                or not following.isascii() or any(ord(char) < 32 for char in following)):
            raise GrvtTestnetTransferError("Transferencias testnet incompletas")
        for row in rows:
            if not isinstance(row, dict):
                raise GrvtTestnetTransferError("Transferencia testnet malformada")
            from_sub = row.get("from_sub_account_id")
            to_sub = row.get("to_sub_account_id")
            if (not isinstance(from_sub, str) or not _SUB.fullmatch(from_sub)
                    or not isinstance(to_sub, str) or not _SUB.fullmatch(to_sub)):
                raise GrvtTestnetTransferError("Subcuenta de transferencia inválida")
            if sub_account_id not in (from_sub, to_sub):
                unrelated += 1
                continue
            tx_id, event_raw = row.get("tx_id"), row.get("event_time")
            currency = row.get("currency")
            if (not isinstance(tx_id, str) or not _ID.fullmatch(tx_id)
                    or not isinstance(event_raw, str) or not event_raw.isdecimal()
                    or not start_time_ns <= int(event_raw) <= now_ns + 2_000_000_000
                    or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)):
                raise GrvtTestnetTransferError("Transferencia testnet malformada")
            direction = ("SELF" if from_sub == to_sub else
                         "IN" if to_sub == sub_account_id else "OUT")
            event_ns = int(event_raw)
            identity = (tx_id, event_ns, currency, direction)
            if identity in seen:
                raise GrvtTestnetTransferError("Transferencia testnet duplicada")
            seen.add(identity)
            amount = _amount(row.get("num_tokens"))
            transfers.append(GrvtTestnetTransfer(
                tx_id, event_ns, currency, amount, direction,
            ))
            with localcontext() as context:
                context.prec = 100
                effect = amount if direction == "IN" else -amount if direction == "OUT" else Decimal(0)
                net[currency] = net.get(currency, Decimal(0)) + effect
            latest = max(latest or 0, event_ns)
        if not following:
            return GrvtTestnetTransferAudit(
                sub_account_id, start_time_ns, now_ns, page_number,
                tuple(sorted(transfers, key=lambda item: (
                    item.event_time_ns, item.tx_id, item.currency, item.direction,
                ))), tuple(sorted(net.items())), unrelated, latest,
            )
        if following == cursor or following in seen_cursors:
            raise GrvtTestnetTransferError("Cursor de transferencias testnet repetido")
        seen_cursors.add(following)
        cursor = following
    raise GrvtTestnetTransferError("Transferencias testnet exceden límite verificable")
