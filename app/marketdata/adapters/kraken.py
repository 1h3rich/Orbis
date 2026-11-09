"""Kraken v2 REST (primario EUR). Reutiliza fetch_kraken sin duplicar."""
from __future__ import annotations
from decimal import Decimal
from app.marketdata._base import RestAdapter, _dec
from app.marketdata.normalize import parse_symbol, native_symbol
from app.pricing import providers as _pub

TF_TO_INTERVAL = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

class KrakenAdapter(RestAdapter):
    name = "kraken"
    ws_kind = "json"
    timeframes = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
    known_pairs = ("BTC/EUR", "BTC/USD", "BTC/USDT", "ETH/EUR", "ETH/USD", "ETH/USDT",
                   "SOL/EUR", "SOL/USD", "SOL/USDT", "ADA/EUR", "DOT/EUR", "MATIC/EUR",
                   "AVAX/EUR", "LINK/EUR", "ETH/BTC")

    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        try:
            price, as_of = _pub.fetch_kraken_ticker(base, quote, http_get=self._http_get)
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(price), "bid": None,
                    "ask": None, "as_of": as_of, "source": "kraken"}
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        if timeframe not in TF_TO_INTERVAL:
            return None
        base, quote = parse_symbol(symbol)
        try:
            raw = _pub.fetch_kraken_ohlc(base, quote, TF_TO_INTERVAL[timeframe], http_get=self._http_get)
            self._mark_ok()
            out = []
            for c in raw[-int(limit):]:
                try:
                    out.append({"t": int(c[0]), "o": str(c[1]), "h": str(c[2]),
                                "l": str(c[3]), "c": str(c[4]), "v": str(c[6]) if len(c) > 6 else "0"})
                except Exception:
                    continue
            if since is not None:
                out = [p for p in out if p["t"] >= int(since)]
            return out
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_symbols(self):
        return sorted(self.known_pairs)
