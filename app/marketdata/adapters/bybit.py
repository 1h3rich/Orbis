"""Bybit v5 REST (público, sin claves)."""
from __future__ import annotations
from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol

TF = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D", "1w": "W", "1M": "M"}

class BybitAdapter(RestAdapter):
    name = "bybit"
    ws_kind = "json"
    timeframes = tuple(TF)
    known_pairs = ("BTC/USDT", "BTC/USDC", "BTC/EUR", "ETH/USDT", "SOL/USDT",
                   "ADA/USDT", "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        sym = f"{base}{quote}"
        try:
            p = self._http_get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={sym}")
            d = ((p.get("result") or {}).get("list")) or [{}]
            d = d[0]
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(d.get("lastPrice")),
                    "bid": str(d.get("bid1Price")) if d.get("bid1Price") else None,
                    "ask": str(d.get("ask1Price")) if d.get("ask1Price") else None,
                    "high": str(d.get("highPrice24h")) if d.get("highPrice24h") else None,
                    "low": str(d.get("lowPrice24h")) if d.get("lowPrice24h") else None,
                    "change_pct": str(d.get("price24hPcnt")) if d.get("price24hPcnt") else None,
                    "base_volume": str(d.get("volume24h")) if d.get("volume24h") else None,
                    "source": "bybit"}
        except Exception as exc:
            self._mark_err(exc)
            return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        if timeframe not in TF:
            return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        sym = f"{base}{quote}"
        try:
            url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={sym}&interval={TF[timeframe]}&limit={int(limit)}"
            if since is not None:
                url += f"&start={int(since) * 1000}"
            p = self._http_get(url)
            self._mark_ok()
            rows = sorted(((p.get("result") or {}).get("list")) or [], key=lambda r: int(r[0]))
            return [{"t": int(int(r[0]) / 1000), "o": str(r[1]), "h": str(r[2]),
                     "l": str(r[3]), "c": str(r[4]), "v": str(r[5])} for r in rows[-int(limit):]]
        except Exception as exc:
            self._mark_err(exc)
            return None
