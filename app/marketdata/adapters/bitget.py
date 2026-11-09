"""Bitget Spot REST (público, sin claves)."""
from __future__ import annotations
from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol
TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W", "1M": "1M"}
class BitgetAdapter(RestAdapter):
    name = "bitget"; ws_kind = "json"; timeframes = tuple(TF)
    known_pairs = ("BTC/USDT", "BTC/EUR", "ETH/USDT", "SOL/USDT",
                   "ADA/USDT", "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        sym = f"{base}{quote}"
        try:
            p = self._http_get(f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={sym}")
            arr = (p.get("data") or [{}]); d = arr[0] if arr else {}
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(d.get("lastPr")),
                    "bid": str(d.get("bidPr")) if d.get("bidPr") else None,
                    "ask": str(d.get("askPr")) if d.get("askPr") else None,
                    "high": str(d.get("high24h")) if d.get("high24h") else None,
                    "low": str(d.get("low24h")) if d.get("low24h") else None,
                    "change_pct": None,
                    "base_volume": str(d.get("baseVolume")) if d.get("baseVolume") else None,
                    "quote_volume": str(d.get("quoteVolume")) if d.get("quoteVolume") else None,
                    "source": "bitget"}
        except Exception as exc:
            self._mark_err(exc); return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        if timeframe not in TF: return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        sym = f"{base}{quote}"
        try:
            url = f"https://api.bitget.com/api/v2/spot/market/candles?symbol={sym}&granularity={TF[timeframe]}&limit={int(limit)}"
            if since is not None: url += f"&startTime={int(since) * 1000}"
            p = self._http_get(url)
            self._mark_ok()
            rows = sorted(p.get("data") or [], key=lambda r: int(r[0]))
            return [{"t": int(int(r[0]) / 1000), "o": str(r[1]), "h": str(r[2]),
                     "l": str(r[3]), "c": str(r[4]), "v": str(r[5])} for r in rows[-int(limit):]]
        except Exception as exc:
            self._mark_err(exc); return None
