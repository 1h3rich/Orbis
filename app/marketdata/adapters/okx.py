"""OKX REST (público, sin claves)."""
from __future__ import annotations
from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol

TF = {"1s": "1s", "30s": "30s", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W", "1M": "1M"}

class OkxAdapter(RestAdapter):
    name = "okx"
    ws_kind = "json"
    timeframes = tuple(TF)
    known_pairs = ("BTC/USDT", "BTC/USDC", "BTC/EUR", "ETH/USDT", "ETH/EUR", "SOL/USDT",
                   "ADA/USDT", "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        iid = f"{base}-{quote}"
        try:
            p = self._http_get(f"https://www.okx.com/api/v5/market/ticker?instId={iid}")
            d = (p.get("data") or [{}])[0]
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(d.get("last")),
                    "bid": str(d.get("bidPx")) if d.get("bidPx") else None,
                    "ask": str(d.get("askPx")) if d.get("askPx") else None,
                    "high": str(d.get("high24h")) if d.get("high24h") else None,
                    "low": str(d.get("low24h")) if d.get("low24h") else None,
                    "base_volume": str(d.get("vol24h")) if d.get("vol24h") else None,
                    "source": "okx"}
        except Exception as exc:
            self._mark_err(exc)
            return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        if timeframe not in TF:
            return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        iid = f"{base}-{quote}"
        try:
            url = f"https://www.okx.com/api/v5/market/candles?instId={iid}&bar={TF[timeframe]}&limit={int(limit)}"
            if since is not None:
                url += f"&after={int(since) * 1000}"
            p = self._http_get(url)
            self._mark_ok()
            rows = sorted(p.get("data") or [], key=lambda r: int(r[0]))
            return [{"t": int(int(r[0]) / 1000), "o": str(r[1]), "h": str(r[2]),
                     "l": str(r[3]), "c": str(r[4]), "v": str(r[5])} for r in rows[-int(limit):]]
        except Exception as exc:
            self._mark_err(exc)
            return None
