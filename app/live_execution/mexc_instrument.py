"""Reglas públicas MEXC Spot y preflight local de órdenes límite REAL.

La validación local no demuestra aceptación del exchange. No consulta cuentas,
no firma solicitudes y no envía órdenes. Los filtros desconocidos bloquean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Callable
import re

import httpx

from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, _utc


MEXC_BASE = "https://api.mexc.com"
_SYMBOL = re.compile(r"^[A-Z0-9]{3,30}$")


class MexcInstrumentError(RuntimeError):
    """Datos públicos MEXC ausentes, contradictorios o no verificables."""


def _decimal(raw: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 80:
        raise MexcInstrumentError(f"{name} is not a decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise MexcInstrumentError(f"{name} is not a decimal string") from exc
    if (not value.is_finite() or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100
            or value < 0 or positive and value == 0):
        raise MexcInstrumentError(f"{name} is outside the accepted range")
    return value


def _precision(raw: object, name: str) -> int:
    if type(raw) is not int or not 0 <= raw <= 18:
        raise MexcInstrumentError(f"{name} is invalid")
    return raw


@dataclass(frozen=True, slots=True)
class MexcSpotRules:
    symbol: str
    base_asset: str
    quote_asset: str
    observed_at: datetime
    buy_allowed: bool
    sell_allowed: bool
    base_precision: int
    quote_precision: int
    base_step: Decimal
    min_quote: Decimal
    max_quote: Decimal
    fee_rate_upper: Decimal
    bid_multiplier_up: Decimal | None
    ask_multiplier_down: Decimal | None


@dataclass(frozen=True, slots=True)
class MexcPreflight:
    allowed: bool
    reason: str


def parse_exchange_info(payload: object, symbol: str, *, observed_at: datetime) -> MexcSpotRules:
    observed_at = _utc(observed_at, "observed_at")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise MexcInstrumentError("Spot symbol is invalid")
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise MexcInstrumentError("MEXC exchangeInfo is malformed")
    rows = payload["symbols"]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise MexcInstrumentError("MEXC did not return exactly one instrument")
    row = rows[0]
    base = row.get("baseAsset")
    quote = row.get("quoteAsset")
    if (row.get("symbol") != symbol or not isinstance(base, str)
            or not isinstance(quote, str) or base == quote or base + quote != symbol
            or not re.fullmatch(r"[A-Z0-9]{2,16}", base)
            or not re.fullmatch(r"[A-Z0-9]{2,16}", quote)):
        raise MexcInstrumentError("MEXC instrument identity does not match")
    status = row.get("status")
    if (type(status) not in (str, int) or status not in ("1", 1)
            or row.get("isSpotTradingAllowed") is not True):
        raise MexcInstrumentError("MEXC Spot instrument is not tradable")
    if (not isinstance(row.get("orderTypes"), list)
            or "LIMIT" not in row["orderTypes"]
            or not isinstance(row.get("permissions"), list)
            or "SPOT" not in row["permissions"]):
        raise MexcInstrumentError("MEXC LIMIT Spot trading is unavailable")
    side_type = row.get("tradeSideType")
    if type(side_type) is str and side_type in ("1", "2", "3", "4"):
        side_type = int(side_type)
    if type(side_type) is not int or side_type not in (1, 2, 3, 4):
        raise MexcInstrumentError("MEXC trading side is unknown")

    filters = row.get("filters")
    if not isinstance(filters, list):
        raise MexcInstrumentError("MEXC instrument filters are unavailable")
    bid_up = ask_down = None
    seen = set()
    for item in filters:
        if not isinstance(item, dict) or item.get("filterType") != "PERCENT_PRICE_BY_SIDE":
            raise MexcInstrumentError("MEXC instrument has an unsupported filter")
        if item["filterType"] in seen:
            raise MexcInstrumentError("MEXC instrument filter is duplicated")
        seen.add(item["filterType"])
        bid_up = _decimal(item.get("bidMultiplierUp"), "bidMultiplierUp")
        ask_down = _decimal(item.get("askMultiplierDown"), "askMultiplierDown")
        if bid_up > 1 or ask_down > 1:
            raise MexcInstrumentError("MEXC price filter is outside the accepted range")

    min_quote = _decimal(row.get("quoteAmountPrecision"), "quoteAmountPrecision", positive=True)
    max_quote = _decimal(row.get("maxQuoteAmount"), "maxQuoteAmount", positive=True)
    if min_quote > max_quote:
        raise MexcInstrumentError("MEXC quote limits are contradictory")
    maker = _decimal(row.get("makerCommission"), "makerCommission")
    taker = _decimal(row.get("takerCommission"), "takerCommission")
    if maker > 1 or taker > 1:
        raise MexcInstrumentError("MEXC commission is invalid")
    return MexcSpotRules(
        symbol=symbol, base_asset=base, quote_asset=quote,
        observed_at=observed_at,
        buy_allowed=side_type in (1, 2), sell_allowed=side_type in (1, 3),
        base_precision=_precision(row.get("baseAssetPrecision"), "baseAssetPrecision"),
        quote_precision=_precision(row.get("quotePrecision"), "quotePrecision"),
        base_step=_decimal(row.get("baseSizePrecision"), "baseSizePrecision", positive=True),
        min_quote=min_quote, max_quote=max_quote,
        fee_rate_upper=max(maker, taker),
        bid_multiplier_up=bid_up, ask_multiplier_down=ask_down,
    )


def _http_get(symbol: str) -> object:
    try:
        response = httpx.get(
            MEXC_BASE + "/api/v3/exchangeInfo", params={"symbol": symbol},
            timeout=5.0, follow_redirects=False, trust_env=False,
        )
    except httpx.HTTPError:
        raise MexcInstrumentError("MEXC instrument rules are unavailable") from None
    if response.status_code != 200:
        raise MexcInstrumentError("MEXC instrument rules are unavailable")
    try:
        return response.json()
    except ValueError:
        raise MexcInstrumentError("MEXC instrument rules are malformed") from None


def fetch_spot_rules(symbol: str, *, now: datetime,
                     http_get: Callable[[str], object] = _http_get) -> MexcSpotRules:
    """Consulta pública bajo demanda; no expone ni necesita credenciales."""
    now = _utc(now, "now")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise MexcInstrumentError("Spot symbol is invalid")
    try:
        payload = http_get(symbol)
    except MexcInstrumentError:
        raise
    except Exception:
        raise MexcInstrumentError("MEXC instrument rules are unavailable") from None
    return parse_exchange_info(payload, symbol, observed_at=now)


def preflight_limit_order(policy: LiveSpotPolicy, intent: LiveSpotIntent,
                          rules: MexcSpotRules, *, last_price: Decimal,
                          last_price_at: datetime, now: datetime) -> MexcPreflight:
    """Comprueba un LIMIT exacto, sin redondear ni enviar al exchange."""
    now = _utc(now, "now")
    last_price_at = _utc(last_price_at, "last_price_at")

    def deny(reason: str) -> MexcPreflight:
        return MexcPreflight(False, reason)

    if (rules.symbol != policy.symbol or intent.symbol != policy.symbol
            or intent.policy_id != policy.policy_id
            or intent.strategy_version_id != policy.strategy_version_id
            or intent.account_ref != policy.account_ref
            or rules.base_asset != policy.base_asset or rules.quote_asset != policy.quote_asset):
        return deny("INSTRUMENT_SCOPE_MISMATCH")
    if (rules.observed_at > now or last_price_at > now
            or (now - rules.observed_at).total_seconds() > policy.max_data_age_seconds
            or (now - last_price_at).total_seconds() > policy.max_data_age_seconds):
        return deny("STALE_INSTRUMENT_OR_TRADE")
    if (type(last_price) is not Decimal or not last_price.is_finite() or last_price <= 0
            or len(last_price.as_tuple().digits) > 40
            or abs(last_price.as_tuple().exponent) > 100):
        return deny("INVALID_LAST_PRICE")
    if intent.side == "BUY" and not rules.buy_allowed or intent.side == "SELL" and not rules.sell_allowed:
        return deny("SIDE_DISABLED")
    quantity = intent.quantity_base
    price = intent.limit_price_quote
    if -quantity.normalize().as_tuple().exponent > rules.base_precision:
        return deny("BASE_PRECISION")
    if -price.normalize().as_tuple().exponent > rules.quote_precision:
        return deny("PRICE_PRECISION")
    with localcontext() as context:
        context.prec = 300
        if quantity % rules.base_step != 0:
            return deny("BASE_STEP")
        notional = quantity * price
        if notional < rules.min_quote:
            return deny("MIN_NOTIONAL")
        if notional > rules.max_quote:
            return deny("MAX_NOTIONAL")
        if intent.estimated_fee_quote < notional * rules.fee_rate_upper:
            return deny("FEE_ESTIMATE_TOO_LOW")
        if (intent.side == "BUY" and rules.bid_multiplier_up is not None
                and price > last_price * (1 + rules.bid_multiplier_up)):
            return deny("EXCHANGE_PRICE_BOUND")
        if (intent.side == "SELL" and rules.ask_multiplier_down is not None
                and price < last_price * (1 - rules.ask_multiplier_down)):
            return deny("EXCHANGE_PRICE_BOUND")
    return MexcPreflight(True, "LOCAL_PREFLIGHT_ONLY")
