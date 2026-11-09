"""Contraste observacional de fills GRVT testnet; nunca libera capital."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re

from app.live_execution.grvt_testnet_order import (
    GrvtTestnetOrderClient, GrvtTestnetOrderObservation,
)


_AMOUNT = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")
_SIGNED_AMOUNT = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")
_CURRENCY = re.compile(r"[A-Z0-9_]{2,16}\Z")


class GrvtTestnetFillError(RuntimeError):
    """Historial de fills testnet incompleto o contradictorio."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetFillEvidence:
    trade_id: str
    event_time_ns: int
    size: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    realized_pnl: Decimal
    builder_fee: Decimal
    is_taker: bool


@dataclass(frozen=True, slots=True)
class GrvtTestnetExternalFillEvidence:
    trade_id: str
    order_id: str
    client_order_id: str
    event_time_ns: int
    instrument: str
    is_buyer: bool
    size: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    realized_pnl: Decimal
    builder_fee: Decimal
    is_liquidation: bool


@dataclass(frozen=True, slots=True)
class GrvtTestnetFillAudit:
    client_order_id: str
    order_id: str
    fill_count: int
    latest_fill_time_ns: int | None
    latest_any_fill_time_ns: int | None
    history_start_ns: int
    filled_size: Decimal
    order_traded_size: Decimal
    sizes_match: bool
    pages_drained: int
    gross_notional: Decimal
    realized_pnl: Decimal
    builder_fee: Decimal
    fees_by_currency: tuple[tuple[str, Decimal], ...]
    fills: tuple[GrvtTestnetFillEvidence, ...]
    other_fills: tuple[GrvtTestnetExternalFillEvidence, ...]
    complete_for_execution: bool = False


def _amount(raw: object, *, positive: bool = False) -> Decimal:
    if not isinstance(raw, str) or not _AMOUNT.fullmatch(raw):
        raise GrvtTestnetFillError("Fill testnet malformado")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise GrvtTestnetFillError("Fill testnet malformado") from None
    if not value.is_finite() or positive and value <= 0:
        raise GrvtTestnetFillError("Fill testnet malformado")
    return value


def _signed_amount(raw: object) -> Decimal:
    if not isinstance(raw, str) or not _SIGNED_AMOUNT.fullmatch(raw):
        raise GrvtTestnetFillError("Coste o P/L testnet malformado")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise GrvtTestnetFillError("Coste o P/L testnet malformado") from None
    if not value.is_finite():
        raise GrvtTestnetFillError("Coste o P/L testnet malformado")
    return value


def audit_testnet_fills(client: GrvtTestnetOrderClient,
                        order: GrvtTestnetOrderObservation, *,
                        now_ns: int, start_time_ns: int | None = None,
                        end_time_ns: int | None = None,
                        ) -> GrvtTestnetFillAudit:
    """Agota páginas desde la creación de la orden y compara tamaños exactos.

    No prueba liquidación, saldo ni cobertura fuera de la retención del venue.
    """
    if (type(now_ns) is not int or now_ns <= 0
            or not isinstance(order, GrvtTestnetOrderObservation)
            or not isinstance(order.create_time_ns, str)
            or not order.create_time_ns.isdecimal()
            or not 0 < int(order.create_time_ns) <= now_ns + 2_000_000_000):
        raise GrvtTestnetFillError("Creación de orden testnet no verificable")
    created_ns = int(order.create_time_ns)
    history_start_ns = created_ns if start_time_ns is None else start_time_ns
    if (type(history_start_ns) is not int
            or not 0 < history_start_ns <= created_ns
            or end_time_ns is not None and (type(end_time_ns) is not int
                or not history_start_ns <= end_time_ns <= now_ns)):
        raise GrvtTestnetFillError("Inicio de historial testnet inválido")
    traded = _amount(order.traded_size)
    cursor = ""
    seen_cursors: set[str] = set()
    seen_trades: set[str] = set()
    total = Decimal("0")
    notional = Decimal("0")
    realized_pnl = Decimal("0")
    builder_fee = Decimal("0")
    fees: dict[str, Decimal] = {}
    evidence: list[GrvtTestnetFillEvidence] = []
    other_evidence: list[GrvtTestnetExternalFillEvidence] = []
    count = 0
    latest_fill_time_ns: int | None = None
    latest_any_fill_time_ns: int | None = None
    for page_number in range(1, 21):
        try:
            page = client.read_fill_history(
                order.sub_account_id, str(history_start_ns - 1), cursor,
                **({"end_time_ns":str(end_time_ns)} if end_time_ns is not None else {}),
            )
        except Exception:
            raise GrvtTestnetFillError("Historial testnet no verificable") from None
        rows, following = page.get("result"), page.get("next")
        if (not isinstance(rows, list) or len(rows) > 500
                or not isinstance(following, str) or len(following) > 1024
                or not following.isascii() or any(ord(char) < 32 for char in following)):
            raise GrvtTestnetFillError("Historial testnet incompleto")
        for row in rows:
            if not isinstance(row, dict) or row.get("sub_account_id") != order.sub_account_id:
                raise GrvtTestnetFillError("Fill testnet de otra subcuenta")
            same_order = row.get("order_id") == order.order_id
            same_client = row.get("client_order_id") == order.client_order_id
            if same_order != same_client:
                raise GrvtTestnetFillError("Identidad de fill testnet contradictoria")
            trade_id = row.get("trade_id")
            event_time = row.get("event_time")
            if (not isinstance(trade_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_:\-]{1,100}", trade_id)
                    or trade_id in seen_trades
                    or not isinstance(event_time, str) or not event_time.isdecimal()
                    or not history_start_ns <= int(event_time) <= (end_time_ns if end_time_ns is not None else now_ns + 2_000_000_000)
                    or type(row.get("is_buyer")) is not bool):
                raise GrvtTestnetFillError("Fill testnet malformado o duplicado")
            if same_order and (int(event_time) < created_ns
                               or row.get("instrument") != order.instrument
                               or row["is_buyer"] != (order.side == "BUY")):
                raise GrvtTestnetFillError("Fill testnet ajeno a la orden")
            seen_trades.add(trade_id)
            latest_any_fill_time_ns = max(latest_any_fill_time_ns or 0,
                                          int(event_time))
            size = _amount(row.get("size"), positive=True)
            price = _amount(row.get("price"), positive=True)
            fee = _signed_amount(row.get("fee"))
            pnl = _signed_amount(row.get("realized_pnl"))
            builder = _amount(row.get("builder_fee"))
            currency = row.get("fee_currency")
            if (type(row.get("is_taker")) is not bool
                    or type(row.get("is_liquidation")) is not bool
                    or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)):
                raise GrvtTestnetFillError("Coste, divisa o liquidación testnet no verificable")
            if not same_order:
                external_order_id = row.get("order_id")
                external_client_id = row.get("client_order_id")
                external_instrument = row.get("instrument")
                if (not isinstance(external_order_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9x]{1,80}", external_order_id)
                        or not isinstance(external_client_id, str)
                        or not re.fullmatch(r"[0-9]{1,20}", external_client_id)
                        or not isinstance(external_instrument, str)
                        or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", external_instrument)):
                    raise GrvtTestnetFillError("Fill externo testnet no verificable")
                other_evidence.append(GrvtTestnetExternalFillEvidence(
                    trade_id, external_order_id, external_client_id,
                    int(event_time), external_instrument, row["is_buyer"],
                    size, price, fee, currency, pnl, builder,
                    row["is_liquidation"],
                ))
                continue
            if row["is_liquidation"]:
                raise GrvtTestnetFillError("Liquidación testnet inesperada")
            latest_fill_time_ns = max(latest_fill_time_ns or 0, int(event_time))
            with localcontext() as context:
                context.prec = 100
                total += size
                notional += size * price
                realized_pnl += pnl
                builder_fee += builder
                fees[currency] = fees.get(currency, Decimal("0")) + fee
            evidence.append(GrvtTestnetFillEvidence(
                trade_id, int(event_time), size, price, fee, currency,
                pnl, builder, row["is_taker"],
            ))
            count += 1
        if not following:
            return GrvtTestnetFillAudit(
                order.client_order_id, order.order_id, count, latest_fill_time_ns,
                latest_any_fill_time_ns, history_start_ns, total, traded,
                total == traded, page_number, notional, realized_pnl,
                builder_fee, tuple(sorted(fees.items())),
                tuple(sorted(evidence, key=lambda item: (item.event_time_ns, item.trade_id))),
                tuple(sorted(other_evidence,
                             key=lambda item: (item.event_time_ns, item.trade_id))),
            )
        if following == cursor or following in seen_cursors:
            raise GrvtTestnetFillError("Cursor de fills testnet repetido")
        seen_cursors.add(following)
        cursor = following
    raise GrvtTestnetFillError("Historial testnet excede el límite verificable")
