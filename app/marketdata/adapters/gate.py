"""Gate.io Spot REST (público, sin claves)."""
from __future__ import annotations
from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol
TF = {"1s": "1s", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"}
class GateAdapter(RestAdapter):
    name = "gate"; ws_kind = "json"; timeframes = tuple(TF)
    known_pairs = ("BTC/USDT", "BTC/EUR", "ETH/USDT", "ETH/EUR", "SOL/USDT",
                   "ADA/USDT", "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        cp = f"{base}_{quote}"
        try:
            p = self._http_get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={cp}")
            d = (p or [{}])[0] if isinstance(p, list) else {}
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(d.get("last")),
                    "bid": str(d.get("highest_bid")) if d.get("highest_bid") else None,
                    "ask": str(d.get("lowest_ask")) if d.get("lowest_ask") else None,
                    "high": str(d.get("high_24h")) if d.get("high_24h") else None,
                    "low": str(d.get("low_24h")) if d.get("low_24h") else None,
                    "change_pct": str(d.get("change_percentage")) if d.get("change_percentage") else None,
                    "base_volume": str(d.get("base_volume")) if d.get("base_volume") else None,
                    "quote_volume": str(d.get("quote_volume")) if d.get("quote_volume") else None,
                    "source": "gate"}
        except Exception as exc:
            self._mark_err(exc); return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        if timeframe not in TF: return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        cp = f"{base}_{quote}"
        try:
            url = f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={cp}&interval={TF[timeframe]}&limit={int(limit)}"
            if since is not None: url += f"&from={int(since)}"
            raw = self._http_get(url)
            self._mark_ok()
            rows = sorted(raw or [], key=lambda r: int(r[0]))
            return [{"t": int(r[0]), "v": str(r[1]), "c": str(r[2]), "h": str(r[3]),
                     "l": str(r[4]), "o": str(r[5])} for r in rows[-int(limit):]]
        except Exception as exc:
            self._mark_err(exc); return None
