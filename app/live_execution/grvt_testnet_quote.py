"""Cotización pública testnet y guardia de precio para una orden IOC GRVT.

No mezcla el ticker PAPER de mainnet con un precio ejecutable testnet.
La cotización es puntual; el preflight privado y el exchange pueden rechazar
la orden o ejecutarla parcialmente después de este control.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
import re
import time
from typing import Callable

import httpx

from app.live_execution.grvt_testnet_order import _INSTRUMENT


TESTNET_TICKER_URL = "https://market-data.testnet.grvt.io/full/v1/ticker"
MAX_QUOTE_AGE_NS = 5_000_000_000
_PRICE = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")


class GrvtTestnetQuoteError(RuntimeError):
    """Error saneado sin datos externos brutos ni secretos."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetQuote:
    instrument: str
    event_time_ns: int
    observed_at_ns: int
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    last_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class GrvtTestnetPriceGuard:
    quote: GrvtTestnetQuote
    max_spread_bps: int
    max_slippage_bps: int


def _decimal(raw: object) -> Decimal:
    if not isinstance(raw, str) or not _PRICE.fullmatch(raw):
        raise ValueError("invalid decimal")
    value = Decimal(raw)
    if not value.is_finite() or value <= 0:
        raise ValueError("invalid decimal")
    return value


def _post(url: str, body: dict) -> httpx.Response:
    return httpx.post(url, json=body, timeout=5.0,
                      follow_redirects=False, trust_env=False)


def fetch_testnet_quote(instrument: str, *,
                        http_post: Callable[[str, dict], httpx.Response] = _post,
                        clock_ns: Callable[[], int] = time.time_ns,
                        ) -> GrvtTestnetQuote:
    """Acepta únicamente el ticker testnet completo, con bid/ask recientes."""
    if not isinstance(instrument, str) or not _INSTRUMENT.fullmatch(instrument):
        raise GrvtTestnetQuoteError("Instrumento testnet inválido")
    try:
        response = http_post(TESTNET_TICKER_URL, {"instrument": instrument})
        payload = response.json() if response.status_code == 200 else None
        row = payload.get("result") if isinstance(payload, dict) and "error" not in payload and "code" not in payload else None
        now_ns = clock_ns()
        if (not isinstance(row, dict) or row.get("instrument") != instrument
                or type(now_ns) is not int or now_ns <= 0):
            raise ValueError("invalid ticker")
        raw_time = row.get("event_time")
        if not isinstance(raw_time, str) or not raw_time.isdecimal():
            raise ValueError("missing event time")
        event_ns = int(raw_time)
        if not -1_000_000_000 <= now_ns - event_ns <= MAX_QUOTE_AGE_NS:
            raise ValueError("stale or future ticker")
        bid = _decimal(row.get("best_bid_price"))
        ask = _decimal(row.get("best_ask_price"))
        bid_size = _decimal(row.get("best_bid_size"))
        ask_size = _decimal(row.get("best_ask_size"))
        if bid >= ask:
            raise ValueError("crossed ticker")
        last = _decimal(row["last_price"]) if row.get("last_price") is not None else None
        return GrvtTestnetQuote(instrument, event_ns, now_ns,
                                 bid, ask, bid_size, ask_size, last_price=last)
    except Exception:
        raise GrvtTestnetQuoteError("Cotización GRVT testnet no verificable") from None


def guard_testnet_limit_price(side: str, limit_price: Decimal,
                              quote: GrvtTestnetQuote, *,
                              max_spread_bps: int, max_slippage_bps: int,
                              now_ns: int) -> None:
    """Rechaza un límite IOC que exceda el deslizamiento autorizado."""
    if (side not in ("BUY", "SELL") or type(limit_price) is not Decimal
            or not limit_price.is_finite() or limit_price <= 0
            or not isinstance(quote, GrvtTestnetQuote)
            or type(quote.event_time_ns) is not int
            or type(quote.observed_at_ns) is not int
            or any(type(value) is not Decimal or not value.is_finite() or value <= 0
                   for value in (quote.bid, quote.ask, quote.bid_size, quote.ask_size))
            or quote.bid >= quote.ask
            or type(now_ns) is not int
            or not -1_000_000_000 <= now_ns - quote.event_time_ns <= MAX_QUOTE_AGE_NS
            or not -1_000_000_000 <= now_ns - quote.observed_at_ns <= MAX_QUOTE_AGE_NS
            or type(max_spread_bps) is not int or not 0 <= max_spread_bps <= 100
            or type(max_slippage_bps) is not int or not 0 <= max_slippage_bps <= 100):
        raise GrvtTestnetQuoteError("Política de precio GRVT testnet inválida")
    with localcontext() as context:
        context.prec = 100
        mid = (quote.bid + quote.ask) / 2
        spread_bps = (quote.ask - quote.bid) * 10_000 / mid
        max_slippage = Decimal(max_slippage_bps) / 10_000
        if spread_bps > max_spread_bps:
            raise GrvtTestnetQuoteError("Spread GRVT testnet superior al límite")
        if (side == "BUY" and limit_price > quote.ask * (1 + max_slippage)):
            raise GrvtTestnetQuoteError("Precio de compra fuera del deslizamiento testnet")
        if (side == "SELL" and limit_price < quote.bid * (1 - max_slippage)):
            raise GrvtTestnetQuoteError("Precio de venta fuera del deslizamiento testnet")
