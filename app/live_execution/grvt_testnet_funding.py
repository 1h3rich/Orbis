"""Pagos de financiación GRVT testnet: observación, nunca asiento REAL."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient


_SIGNED = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")
_INSTRUMENT = re.compile(r"[A-Za-z0-9_]{1,80}\Z")
_CURRENCY = re.compile(r"[A-Z0-9]{2,16}\Z")
_TX_ID = re.compile(r"[A-Za-z0-9_:\-]{1,128}\Z")


class GrvtTestnetFundingError(RuntimeError):
    """Historial de financiación testnet incompleto o contradictorio."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetFundingPayment:
    tx_id: str
    event_time_ns: int
    instrument: str
    currency: str
    amount_paid: Decimal


@dataclass(frozen=True, slots=True)
class GrvtTestnetFundingAudit:
    sub_account_id: str
    history_start_ns: int
    observed_until_ns: int
    pages_drained: int
    payments: tuple[GrvtTestnetFundingPayment, ...]
    paid_by_currency: tuple[tuple[str, Decimal], ...]
    latest_payment_time_ns: int | None
    complete_for_execution: bool = False


def _amount(raw: object) -> Decimal:
    if not isinstance(raw, str) or not _SIGNED.fullmatch(raw):
        raise GrvtTestnetFundingError("Pago de financiación testnet inválido")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise GrvtTestnetFundingError("Pago de financiación testnet inválido") from None
    if not amount.is_finite():
        raise GrvtTestnetFundingError("Pago de financiación testnet inválido")
    return amount


def audit_testnet_funding(client: GrvtTestnetOrderClient,
                          sub_account_id: str, *,
                          start_time_ns: int, now_ns: int,
                          ) -> GrvtTestnetFundingAudit:
    """Agota hasta 20 páginas; retención y otros movimientos siguen inciertos."""
    if (not isinstance(sub_account_id, str) or not re.fullmatch(r"[0-9]{1,30}", sub_account_id)
            or type(start_time_ns) is not int or type(now_ns) is not int
            or not 1 < start_time_ns <= now_ns):
        raise GrvtTestnetFundingError("Ventana de financiación testnet inválida")
    cursor = ""
    seen_cursors: set[str] = set()
    seen_payments: set[tuple[str, int, str, str]] = set()
    payments: list[GrvtTestnetFundingPayment] = []
    paid: dict[str, Decimal] = {}
    latest: int | None = None
    for page_number in range(1, 21):
        try:
            page = client.read_funding_history(
                sub_account_id, str(start_time_ns - 1), str(now_ns), cursor,
            )
        except Exception:
            raise GrvtTestnetFundingError("Financiación testnet no verificable") from None
        rows, following = page.get("result"), page.get("next")
        if (not isinstance(rows, list) or len(rows) > 500
                or not isinstance(following, str) or len(following) > 1024
                or not following.isascii() or any(ord(char) < 32 for char in following)):
            raise GrvtTestnetFundingError("Financiación testnet incompleta")
        for row in rows:
            if not isinstance(row, dict) or row.get("sub_account_id") != sub_account_id:
                raise GrvtTestnetFundingError("Pago testnet de otra subcuenta")
            tx_id, event_raw = row.get("tx_id"), row.get("event_time")
            instrument, currency = row.get("instrument"), row.get("currency")
            if (not isinstance(tx_id, str) or not _TX_ID.fullmatch(tx_id)
                    or not isinstance(event_raw, str) or not event_raw.isdecimal()
                    or not start_time_ns <= int(event_raw) <= now_ns + 2_000_000_000
                    or not isinstance(instrument, str) or not _INSTRUMENT.fullmatch(instrument)
                    or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)):
                raise GrvtTestnetFundingError("Pago de financiación testnet malformado")
            event_ns = int(event_raw)
            identity = (tx_id, event_ns, instrument, currency)
            if identity in seen_payments:
                raise GrvtTestnetFundingError("Pago de financiación testnet duplicado")
            seen_payments.add(identity)
            amount = _amount(row.get("amount"))
            payments.append(GrvtTestnetFundingPayment(
                tx_id, event_ns, instrument, currency, amount,
            ))
            with localcontext() as context:
                context.prec = 100
                paid[currency] = paid.get(currency, Decimal(0)) + amount
            latest = max(latest or 0, event_ns)
        if not following:
            return GrvtTestnetFundingAudit(
                sub_account_id, start_time_ns, now_ns, page_number,
                tuple(sorted(payments, key=lambda item: (
                    item.event_time_ns, item.tx_id, item.instrument, item.currency,
                ))),
                tuple(sorted(paid.items())), latest,
            )
        if following == cursor or following in seen_cursors:
            raise GrvtTestnetFundingError("Cursor de financiación testnet repetido")
        seen_cursors.add(following)
        cursor = following
    raise GrvtTestnetFundingError("Financiación testnet excede el límite verificable")


def linked_testnet_funding(section: dict, fills: list[dict], *, instrument: str,
                           start_ns: int, end_ns: int) -> dict:
    """Recalcula financiación reportada y la vincula a fills propios.

    GRVT identifica con tx_id el trade que liquida el pago. No se infiere un
    pago desde una tasa pública ni se toma un movimiento de otra operación.
    """
    try:
        rows = section["rows"]
        if (not isinstance(rows,list) or len(rows) > 10000
                or type(section.get("payment_count")) is not int
                or section["payment_count"] != len(rows)):
            raise ValueError("funding count")
        if not rows:
            if section.get("paid_by_currency",[]) != []:
                raise ValueError("empty funding totals")
            return {"payment_count":0,"paid_usdt":"0","rows":[]}
        if (type(start_ns) is not int or type(end_ns) is not int or not 0 < start_ns <= end_ns
                or not isinstance(fills,list) or len(fills) > 10000):
            raise ValueError("funding window")
        owned = {fill["trade_id"]:fill["event_time_ns"] for fill in fills}
        if len(owned) != len(fills):
            raise ValueError("duplicate fills")
        seen = set()
        total = Decimal(0)
        with localcontext() as context:
            context.prec = 100
            for row in rows:
                if (not isinstance(row,dict)
                        or set(row) != {"tx_id","event_time_ns","instrument","currency","amount_paid"}
                        or not isinstance(row["tx_id"],str) or not _TX_ID.fullmatch(row["tx_id"])
                        or row["tx_id"] not in owned or type(owned[row["tx_id"]]) is not int
                        or type(row["event_time_ns"]) is not int
                        or not start_ns <= owned[row["tx_id"]] <= row["event_time_ns"] <= end_ns
                        or row["instrument"] != instrument or row["currency"] != "USDT"):
                    raise ValueError("unlinked funding")
                identity = (row["tx_id"],row["event_time_ns"],row["instrument"],row["currency"])
                if identity in seen:
                    raise ValueError("duplicate funding")
                seen.add(identity)
                total += _amount(row["amount_paid"])
        declared = section.get("paid_by_currency")
        if (not isinstance(declared,list) or len(declared) != 1
                or not isinstance(declared[0],list) or len(declared[0]) != 2
                or declared[0][0] != "USDT" or _amount(declared[0][1]) != total):
            raise ValueError("funding totals")
        return {"payment_count":len(rows),"paid_usdt":format(total,"f"),"rows":rows}
    except Exception:
        raise GrvtTestnetFundingError("Financiación testnet no vinculada a fills propios") from None
