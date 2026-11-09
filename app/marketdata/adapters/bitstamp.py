"""Datos públicos BTC/USD y BTC/EUR de Bitstamp, sin claves ni órdenes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.marketdata._base import RestAdapter
from app.marketdata.http import qs


BASE_URL = "https://www.bitstamp.net/api/v2"
DAY = 86400
MARKETS = {"BTC/USD": "btcusd", "BTC/EUR": "btceur"}


def _valid_row(row: dict) -> dict | None:
    try:
        t = int(row["timestamp"])
        o, h, l, c, v = (Decimal(str(row[key])) for key in ("open", "high", "low", "close", "volume"))
        if t < 0 or t % DAY or not all(x.is_finite() for x in (o, h, l, c, v)):
            return None
        if min(o, h, l, c) <= 0 or v < 0 or l > min(o, c) or h < max(o, c):
            return None
        return {"t": t, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": str(v), "closed": True}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


class BitstampAdapter(RestAdapter):
    name = "bitstamp"
    ws_kind = "poll"
    timeframes = ("1d",)
    known_pairs = tuple(MARKETS)

    def get_ticker(self, symbol):
        market = self.canonical(symbol)
        if market not in MARKETS:
            return None
        try:
            raw = self._http_get(f"{BASE_URL}/ticker/{MARKETS[market]}/")
            price = Decimal(str(raw["last"]))
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid ticker")
            self._mark_ok()
            return {"symbol": market, "price": str(price), "source": self.name}
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_candles(self, symbol, timeframe="1d", limit=200, since=None, before=None):
        market = self.canonical(symbol)
        if market not in MARKETS or timeframe != "1d":
            return None
        params = {"step": DAY, "limit": min(1000, max(1, int(limit))), "exclude_current_candle": "true"}
        if before is not None:
            params["end"] = int(before) - 1
        elif since is not None:
            params["start"] = int(since)
        try:
            raw = self._http_get(f"{BASE_URL}/ohlc/{MARKETS[market]}/?{qs(params)}")
            rows = raw["data"]["ohlc"]
            if not isinstance(rows, list):
                raise ValueError("invalid OHLC response")
            unique = {}
            for item in rows:
                candle = _valid_row(item)
                if candle is not None:
                    unique[candle["t"]] = candle
            self._mark_ok()
            return [unique[t] for t in sorted(unique)]
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_candles_before(self, symbol, timeframe="1d", limit=200, *, before):
        return self.get_candles(symbol, timeframe=timeframe, limit=limit, before=before)
