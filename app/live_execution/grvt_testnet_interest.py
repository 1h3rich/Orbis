"""Interés horario GRVT testnet observado, sin inferir su signo contable."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient


_SUB = re.compile(r"[0-9]{1,30}\Z")
_CURRENCY = re.compile(r"[A-Z0-9]{2,16}\Z")
_SIGNED = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")
_UNSIGNED = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")


class GrvtTestnetInterestError(RuntimeError):
    """Historial testnet incompleto o contradictorio; datos externos saneados."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetInterestPayment:
    event_time_ns: int
    charge_time_ns: int
    currency: str
    amount_reported: Decimal
    borrowed_amount: Decimal


@dataclass(frozen=True, slots=True)
class GrvtTestnetInterestAudit:
    sub_account_id: str
    history_start_ns: int
    observed_until_ns: int
    pages_drained: int
    payments: tuple[GrvtTestnetInterestPayment, ...]
    reported_by_currency: tuple[tuple[str, Decimal], ...]
    latest_payment_time_ns: int | None
    complete_for_execution: bool = False


def _decimal(raw: object, *, nonnegative: bool) -> Decimal:
    pattern = _UNSIGNED if nonnegative else _SIGNED
    if not isinstance(raw, str) or not pattern.fullmatch(raw):
        raise GrvtTestnetInterestError("Importe de interés testnet inválido")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise GrvtTestnetInterestError("Importe de interés testnet inválido") from None
    if not value.is_finite() or nonnegative and value < 0:
        raise GrvtTestnetInterestError("Importe de interés testnet inválido")
    return value


def audit_testnet_interest(client: GrvtTestnetOrderClient,
                            sub_account_id: str, *, start_time_ns: int,
                            now_ns: int) -> GrvtTestnetInterestAudit:
    """Agota páginas y conserva importes reportados, sin ajustarlos al P/L.

    La documentación describe interés MAM cargado por hora, pero no establece
    explícitamente una convención de signo para `amount`. Se conserva el signo
    recibido y se evita interpretar el campo como débito hasta validarlo.
    """
    if (not isinstance(sub_account_id, str) or not _SUB.fullmatch(sub_account_id)
            or type(start_time_ns) is not int or type(now_ns) is not int
            or not 1 < start_time_ns <= now_ns):
        raise GrvtTestnetInterestError("Ventana de interés testnet inválida")
    cursor = ""
    seen_cursors: set[str] = set()
    seen: set[tuple[int, int, str]] = set()
    payments: list[GrvtTestnetInterestPayment] = []
    reported: dict[str, Decimal] = {}
    latest: int | None = None
    for page_number in range(1, 21):
        try:
            page = client.read_interest_history(
                sub_account_id, str(start_time_ns - 1), str(now_ns), cursor,
            )
        except Exception:
            raise GrvtTestnetInterestError("Interés testnet no verificable") from None
        rows, following = page.get("result"), page.get("next")
        if (not isinstance(rows, list) or len(rows) > 500
                or not isinstance(following, str) or len(following) > 1024
                or not following.isascii() or any(ord(char) < 32 for char in following)):
            raise GrvtTestnetInterestError("Interés testnet incompleto")
        for row in rows:
            if not isinstance(row, dict) or row.get("sub_account_id") != sub_account_id:
                raise GrvtTestnetInterestError("Interés testnet de otra subcuenta")
            event_raw, charge_raw = row.get("event_time"), row.get("charge_time")
            currency = row.get("currency")
            if (not isinstance(event_raw, str) or not event_raw.isdecimal()
                    or not start_time_ns <= int(event_raw) <= now_ns + 2_000_000_000
                    or not isinstance(charge_raw, str) or not charge_raw.isdecimal()
                    or not 0 < int(charge_raw) <= int(event_raw) + 2_000_000_000
                    or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)):
                raise GrvtTestnetInterestError("Pago de interés testnet malformado")
            identity = (int(event_raw), int(charge_raw), currency)
            if identity in seen:
                raise GrvtTestnetInterestError("Pago de interés testnet duplicado")
            seen.add(identity)
            amount = _decimal(row.get("amount"), nonnegative=False)
            borrowed = _decimal(row.get("borrowed_amount"), nonnegative=True)
            payments.append(GrvtTestnetInterestPayment(
                identity[0], identity[1], currency, amount, borrowed,
            ))
            with localcontext() as context:
                context.prec = 100
                reported[currency] = reported.get(currency, Decimal(0)) + amount
            latest = max(latest or 0, identity[0])
        if not following:
            return GrvtTestnetInterestAudit(
                sub_account_id, start_time_ns, now_ns, page_number,
                tuple(sorted(payments, key=lambda row: (
                    row.event_time_ns, row.charge_time_ns, row.currency))),
                tuple(sorted(reported.items())), latest,
            )
        if following == cursor or following in seen_cursors:
            raise GrvtTestnetInterestError("Cursor de interés testnet repetido")
        seen_cursors.add(following)
        cursor = following
    raise GrvtTestnetInterestError("Interés testnet excede límite verificable")
