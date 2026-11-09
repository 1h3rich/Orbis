"""Evidencia puntual de mercado y cuenta MEXC para una futura puerta REAL.

No construye por sí sola un LiveSpotState autorizado: exposición, P/L diario y
conciliación requieren un libro de riesgo independiente y verificable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Callable

import httpx

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.live_execution.mexc_instrument import MexcSpotRules, fetch_spot_rules
from app.live_execution.policy import LiveSpotPolicy, _utc


_SYMBOL = re.compile(r"[A-Z0-9]{3,30}\Z")
_ASSET = re.compile(r"[A-Z0-9]{2,24}\Z")
_UID = re.compile(r"[1-9][0-9]{0,31}\Z")


class MexcLiveObservationError(RuntimeError):
    """Error seguro, sin clave, URL firmada ni respuesta bruta."""


@dataclass(frozen=True, slots=True, repr=False)
class MexcObservedBalance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        with localcontext() as context:
            context.prec = 300
            return self.free + self.locked


@dataclass(frozen=True, slots=True, repr=False)
class MexcValuedBalance:
    asset: str
    total: Decimal
    bid_quote: Decimal
    value_quote: Decimal
    price_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class MexcAccountValuation:
    account_ref: str
    quote_asset: str
    observed_at: datetime
    valued_at: datetime
    total_quote: Decimal
    non_quote_exposure_quote: Decimal
    balances: tuple[MexcValuedBalance, ...]


@dataclass(frozen=True, slots=True)
class MexcLiveEvidence:
    account_ref: str
    view_uid: str = field(repr=False)
    symbol: str
    account_at: datetime
    open_orders_at: datetime
    quote_at: datetime
    rules: MexcSpotRules
    open_orders: tuple[dict, ...] = field(repr=False)
    balances: tuple[MexcObservedBalance, ...] = field(repr=False)
    bid: Decimal
    ask: Decimal
    last: Decimal
    free_base: Decimal
    locked_base: Decimal
    free_quote: Decimal
    locked_quote: Decimal


def _positive(raw: object) -> Decimal:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 80:
        raise MexcLiveObservationError("Precio MEXC inválido")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise MexcLiveObservationError("Precio MEXC inválido") from None
    if (not value.is_finite() or value <= 0 or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100):
        raise MexcLiveObservationError("Precio MEXC inválido")
    return value


def _nonnegative(raw: object) -> Decimal:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 80:
        raise MexcLiveObservationError("Saldo MEXC inválido")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise MexcLiveObservationError("Saldo MEXC inválido") from None
    if (not value.is_finite() or value < 0 or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100):
        raise MexcLiveObservationError("Saldo MEXC inválido")
    return value


def _quote_get(symbol: str) -> object:
    try:
        response = httpx.get(
            "https://api.mexc.com/api/v3/ticker/24hr", params={"symbol": symbol},
            timeout=5.0, follow_redirects=False, trust_env=False,
        )
    except httpx.HTTPError:
        raise MexcLiveObservationError("Ticker MEXC indisponible") from None
    if response.status_code != 200:
        raise MexcLiveObservationError("Ticker MEXC indisponible")
    try:
        return response.json()
    except ValueError:
        raise MexcLiveObservationError("Ticker MEXC no verificable") from None


def fetch_live_quote(symbol: str, *, now: datetime, max_age_seconds: int,
                     http_get: Callable[[str], object] = _quote_get,
                     clock: Callable[[], datetime] | None = None,
                     ) -> tuple[Decimal, Decimal, Decimal, datetime]:
    """Ticker exacto sin caché, con hora del evento informada por MEXC."""
    now = _utc(now, "now")
    if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
            or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300):
        raise MexcLiveObservationError("Solicitud de ticker REAL inválida")
    try:
        row = http_get(symbol)
    except MexcLiveObservationError:
        raise
    except Exception:
        raise MexcLiveObservationError("Ticker MEXC indisponible") from None
    if not isinstance(row, dict) or row.get("symbol") != symbol:
        raise MexcLiveObservationError("Ticker MEXC de otro mercado")
    timestamp_ms = row.get("closeTime")
    if type(timestamp_ms) is not int or timestamp_ms <= 0:
        raise MexcLiveObservationError("Ticker MEXC sin hora verificable")
    try:
        event_at = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise MexcLiveObservationError("Ticker MEXC con hora inválida") from None
    checked_at = _utc(clock(), "now") if clock is not None else now
    if checked_at < now:
        raise MexcLiveObservationError("Reloj local retrocedió durante el ticker MEXC")
    if event_at > checked_at or (checked_at - event_at).total_seconds() > max_age_seconds:
        raise MexcLiveObservationError("Ticker MEXC caducado o futuro")
    bid, ask, last = (_positive(row.get(name)) for name in
                      ("bidPrice", "askPrice", "lastPrice"))
    if bid > ask:
        raise MexcLiveObservationError("Libro MEXC cruzado")
    return bid, ask, last, event_at


def fetch_live_evidence(
    view: MexcReadOnlyClient, policy: LiveSpotPolicy, *,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable[[str], object] = _quote_get,
    rules_fetch: Callable[[str, datetime], MexcSpotRules] | None = None,
) -> MexcLiveEvidence:
    """Exige dos lecturas concordantes; no inventa un estado reconciliado.

    El segundo barrido detecta cambios de inventario u órdenes durante la
    lectura, pero no demuestra que no hubiera movimientos intermedios ni
    sustituye el historial completo de la cuenta.
    """
    first = _fetch_live_evidence_once(
        view, policy, clock=clock, quote_get=quote_get, rules_fetch=rules_fetch,
    )
    second = _fetch_live_evidence_once(
        view, policy, clock=clock, quote_get=quote_get, rules_fetch=rules_fetch,
    )
    finished_at = _utc((clock or (lambda: datetime.now(timezone.utc)))(), "now")
    if (first.account_ref != second.account_ref or first.view_uid != second.view_uid
            or first.symbol != second.symbol
            or first.balances != second.balances
            or first.open_orders != second.open_orders
            or replace(first.rules, observed_at=second.rules.observed_at) != second.rules
            or second.account_at < first.account_at
            or second.open_orders_at < first.open_orders_at
            or second.quote_at < first.quote_at
            or second.rules.observed_at < first.rules.observed_at):
        raise MexcLiveObservationError("Cuenta MEXC cambió durante la observación")
    if any(stamp > finished_at or (finished_at - stamp).total_seconds()
           > policy.max_data_age_seconds for stamp in (
               first.account_at, first.open_orders_at, first.quote_at,
               first.rules.observed_at, second.account_at, second.open_orders_at,
               second.quote_at, second.rules.observed_at,
           )):
        raise MexcLiveObservationError("Barrido MEXC caducado antes de terminar")
    return second


def _fetch_live_evidence_once(
    view: MexcReadOnlyClient, policy: LiveSpotPolicy, *,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable[[str], object] = _quote_get,
    rules_fetch: Callable[[str, datetime], MexcSpotRules] | None = None,
) -> MexcLiveEvidence:
    """Lee UID, saldos, reglas y ticker una sola vez."""
    if not isinstance(policy, LiveSpotPolicy):
        raise MexcLiveObservationError("Política REAL inválida")
    now = clock or (lambda: datetime.now(timezone.utc))
    try:
        account_ref = view.account_reference()
    except Exception:
        raise MexcLiveObservationError("Referencia View no verificable") from None
    if account_ref != policy.account_ref:
        raise MexcLiveObservationError("Clave View de otra referencia local")
    try:
        uid = view.account_uid()
        balances = view.balances()
        open_orders = view.open_orders(policy.symbol)
        rules = (rules_fetch(policy.symbol, _utc(now(), "now")) if rules_fetch
                 else fetch_spot_rules(policy.symbol, now=_utc(now(), "now")))
    except (MexcReadOnlyError, ValueError):
        raise MexcLiveObservationError("Identidad, saldos o reglas MEXC no verificables") from None
    except MexcLiveObservationError:
        raise
    except Exception:
        raise MexcLiveObservationError("Identidad, saldos o reglas MEXC no disponibles") from None
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise MexcLiveObservationError("UID View MEXC no verificable")
    if not isinstance(rules, MexcSpotRules):
        raise MexcLiveObservationError("Reglas MEXC no verificables")
    checked_at = _utc(now(), "now")
    if (rules.symbol != policy.symbol or rules.base_asset != policy.base_asset
            or rules.quote_asset != policy.quote_asset or rules.observed_at > checked_at
            or (checked_at - rules.observed_at).total_seconds() > policy.max_data_age_seconds):
        raise MexcLiveObservationError("Reglas MEXC caducadas o de otro instrumento")
    bid, ask, last, quote_at = fetch_live_quote(
        policy.symbol, now=_utc(now(), "now"),
        max_age_seconds=policy.max_data_age_seconds, http_get=quote_get, clock=now,
    )
    try:
        uid_after = view.account_uid()
    except Exception:
        raise MexcLiveObservationError("UID View MEXC no verificable al finalizar") from None
    if uid_after != uid:
        raise MexcLiveObservationError("UID View MEXC cambió durante la observación")
    if (not isinstance(balances, dict)
            or balances.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or balances.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or not isinstance(balances.get("balances"), list)):
        raise MexcLiveObservationError("Saldos MEXC sin procedencia válida")
    if (not isinstance(open_orders, dict)
            or open_orders.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or open_orders.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or open_orders.get("coverage") != "ALL_OPEN_ORDERS_FOR_ONE_SYMBOL"
            or open_orders.get("symbol") != policy.symbol
            or not isinstance(open_orders.get("orders"), list)):
        raise MexcLiveObservationError("Órdenes abiertas MEXC sin cobertura verificable")
    try:
        account_at = _utc(datetime.fromisoformat(balances["observed_at"]), "observed_at")
        open_orders_at = _utc(datetime.fromisoformat(open_orders["observed_at"]), "open_orders_at")
    except (KeyError, TypeError, ValueError):
        raise MexcLiveObservationError("Observaciones MEXC sin hora válida") from None
    end_at = _utc(now(), "now")
    if (account_at > end_at or open_orders_at > end_at
            or (end_at - account_at).total_seconds() > policy.max_data_age_seconds
            or (end_at - open_orders_at).total_seconds() > policy.max_data_age_seconds):
        raise MexcLiveObservationError("Cuenta MEXC caducada o futura")
    ids = set()
    for order in open_orders["orders"]:
        if (not isinstance(order, dict) or order.get("symbol") != policy.symbol
                or order.get("status") not in ("NEW", "PARTIALLY_FILLED")
                or not isinstance(order.get("order_id"), str)
                or not isinstance(order.get("client_order_id"), str)
                or order["order_id"] in ids):
            raise MexcLiveObservationError("Órdenes abiertas MEXC inválidas")
        ids.add(order["order_id"])
    amounts: dict[str, tuple[Decimal, Decimal]] = {}
    for item in balances["balances"]:
        if (not isinstance(item, dict) or not isinstance(item.get("asset"), str)
                or not _ASSET.fullmatch(item["asset"])
                or item["asset"] in amounts):
            raise MexcLiveObservationError("Saldos MEXC malformados o duplicados")
        free = _nonnegative(item.get("free"))
        locked = _nonnegative(item.get("locked"))
        total = _nonnegative(item.get("total"))
        with localcontext() as context:
            context.prec = 300
            if free + locked != total:
                raise MexcLiveObservationError("Saldo MEXC no coincide con libre más bloqueado")
        amounts[item["asset"]] = (free, locked)
    free_base, locked_base = amounts.get(policy.base_asset, (Decimal(0), Decimal(0)))
    free_quote, locked_quote = amounts.get(policy.quote_asset, (Decimal(0), Decimal(0)))
    return MexcLiveEvidence(
        account_ref=policy.account_ref, view_uid=uid, symbol=policy.symbol,
        account_at=account_at, open_orders_at=open_orders_at,
        quote_at=quote_at, rules=rules,
        open_orders=tuple(dict(row) for row in open_orders["orders"]),
        balances=tuple(MexcObservedBalance(asset, *amounts[asset]) for asset in sorted(amounts)),
        bid=bid, ask=ask, last=last, free_base=free_base,
        locked_base=locked_base, free_quote=free_quote, locked_quote=locked_quote,
    )


def value_spot_account(
    evidence: MexcLiveEvidence, policy: LiveSpotPolicy, *,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable[[str], object] = _quote_get,
) -> MexcAccountValuation:
    """Valora todo el Spot en la divisa de la política; no infiere coste/P&L.

    Cada activo no cotizado directamente contra esa divisa bloquea el cálculo.
    Una valoración completa tampoco acredita fills, saldo anterior o límites
    diarios y no construye un estado REAL reconciliado.
    """
    if (not isinstance(evidence, MexcLiveEvidence) or not isinstance(policy, LiveSpotPolicy)
            or evidence.account_ref != policy.account_ref or evidence.symbol != policy.symbol
            or evidence.rules.base_asset != policy.base_asset
            or evidence.rules.quote_asset != policy.quote_asset):
        raise MexcLiveObservationError("Cuenta y política MEXC no coinciden")
    current = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(current(), "now")
    if (evidence.account_at > started or evidence.quote_at > started
            or (started - evidence.account_at).total_seconds() > policy.max_data_age_seconds
            or (started - evidence.quote_at).total_seconds() > policy.max_data_age_seconds):
        raise MexcLiveObservationError("Saldos o precio MEXC caducados")
    if (type(evidence.bid) is not Decimal or not evidence.bid.is_finite()
            or evidence.bid <= 0):
        raise MexcLiveObservationError("Precio MEXC inválido")
    rows: list[MexcValuedBalance] = []
    seen: set[str] = set()
    with localcontext() as context:
        context.prec = 300
        total_quote = Decimal(0)
        non_quote = Decimal(0)
        for balance in evidence.balances:
            if (not isinstance(balance, MexcObservedBalance)
                    or not isinstance(balance.asset, str)
                    or not _ASSET.fullmatch(balance.asset) or balance.asset in seen
                    or type(balance.free) is not Decimal or type(balance.locked) is not Decimal
                    or not balance.free.is_finite() or not balance.locked.is_finite()
                    or balance.free < 0 or balance.locked < 0
                    or any(len(value.as_tuple().digits) > 40
                           or abs(value.as_tuple().exponent) > 100
                           for value in (balance.free, balance.locked))):
                raise MexcLiveObservationError("Inventario MEXC no verificable")
            seen.add(balance.asset)
            amount = balance.total
            if amount == 0:
                continue
            if balance.asset == policy.quote_asset:
                bid, price_at = Decimal(1), evidence.account_at
            elif balance.asset == policy.base_asset:
                bid, price_at = evidence.bid, evidence.quote_at
            else:
                symbol = balance.asset + policy.quote_asset
                if not _SYMBOL.fullmatch(symbol):
                    raise MexcLiveObservationError("Activo MEXC sin par de valoración directo")
                bid, _, _, price_at = fetch_live_quote(
                    symbol, now=_utc(current(), "now"),
                    max_age_seconds=policy.max_data_age_seconds, http_get=quote_get, clock=current,
                )
            value = amount * bid
            rows.append(MexcValuedBalance(balance.asset, amount, bid, value, price_at))
            total_quote += value
            if balance.asset != policy.quote_asset:
                non_quote += value
        by_asset = {item.asset: item for item in evidence.balances}
        for asset, free, locked in (
            (policy.base_asset, evidence.free_base, evidence.locked_base),
            (policy.quote_asset, evidence.free_quote, evidence.locked_quote),
        ):
            observed = by_asset.get(asset)
            if ((observed.free, observed.locked) if observed else (Decimal(0), Decimal(0))) != (free, locked):
                raise MexcLiveObservationError("Resumen e inventario MEXC no coinciden")
        finished = _utc(current(), "now")
        if (finished < started or (finished - evidence.account_at).total_seconds()
                > policy.max_data_age_seconds or any(
                    item.price_at > finished or (finished - item.price_at).total_seconds()
                    > policy.max_data_age_seconds for item in rows)):
            raise MexcLiveObservationError("Valoración MEXC caducada antes de terminar")
    return MexcAccountValuation(
        account_ref=policy.account_ref, quote_asset=policy.quote_asset,
        observed_at=evidence.account_at, valued_at=finished,
        total_quote=total_quote, non_quote_exposure_quote=non_quote,
        balances=tuple(rows),
    )
