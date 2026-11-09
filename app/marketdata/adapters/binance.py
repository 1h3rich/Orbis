"""Binance Spot REST (público, sin claves)."""
from __future__ import annotations
from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol

TF = {"1s": "1s", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w", "1M": "1M"}

class BinanceAdapter(RestAdapter):
    name = "binance"
    ws_kind = "json"
    timeframes = tuple(TF)
    known_pairs = ("BTC/USDT", "BTC/USDC", "BTC/EUR", "ETH/USDT", "ETH/EUR", "SOL/USDT",
                   "ADA/USDT", "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        sym = f"{base}{quote}"
        try:
            p = self._http_get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}")
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(p.get("lastPrice")),
                    "bid": str(p.get("bidPrice")) if p.get("bidPrice") else None,
                    "ask": str(p.get("askPrice")) if p.get("askPrice") else None,
                    "high": str(p.get("highPrice")) if p.get("highPrice") else None,
                    "low": str(p.get("lowPrice")) if p.get("lowPrice") else None,
                    "change_pct": str(p.get("priceChangePercent")) if p.get("priceChangePercent") else None,
                    "base_volume": str(p.get("volume")) if p.get("volume") else None,
                    "quote_volume": str(p.get("quoteVolume")) if p.get("quoteVolume") else None,
                    "source": "binance"}
        except Exception as exc:
            self._mark_err(exc)
            return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None, before=None):
        if timeframe not in TF:
            return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        sym = f"{base}{quote}"
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={TF[timeframe]}&limit={int(limit)}"
            if before is not None:
                url += f"&endTime={int(before) * 1000 - 1}"
            elif since is not None:
                url += f"&startTime={int(since) * 1000}"
            raw = self._http_get(url)
            self._mark_ok()
            out = [{"t": int(int(c[0]) / 1000), "o": str(c[1]), "h": str(c[2]),
                    "l": str(c[3]), "c": str(c[4]), "v": str(c[5])} for c in raw]
            return out
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_candles_before(self, symbol, timeframe="1h", limit=200, *, before):
        return self.get_candles(symbol, timeframe=timeframe, limit=limit, before=before)
