"""KuCoin Spot REST (público, sin claves)."""
from __future__ import annotations
from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol
TF = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day", "1w": "1week", "1M": "1month"}
# Duración aproximada de cada vela en segundos (solo para dimensionar la
# ventana startAt/endAt que pide la API; 1M ≈ 30 días como en el resto del
# proyecto). Antes la ventana era fija (200 h) y con 1M/1w no cabía ni una
# vela: ahora es proporcional al TF pedido (limit × duración).
TF_SECS = {"1min": 60, "5min": 300, "15min": 900, "30min": 1800, "1hour": 3600,
           "4hour": 14400, "1day": 86400, "1week": 604800, "1month": 2592000}
class KucoinAdapter(RestAdapter):
    name = "kucoin"; ws_kind = "json"; timeframes = tuple(TF)
    known_pairs = ("BTC/USDT", "BTC/EUR", "ETH/USDT", "ETH/EUR", "SOL/USDT",
                   "ADA/USDT", "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        sym = f"{base}-{quote}"
        try:
            p = self._http_get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={sym}")
            s = self._http_get(f"https://api.kucoin.com/api/v1/market/stats?symbol={sym}")
            d = (p.get("data") or {}); st = (s.get("data") or {})
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(d.get("price")),
                    "bid": str(d.get("bestBid")) if d.get("bestBid") else None,
                    "ask": str(d.get("bestAsk")) if d.get("bestAsk") else None,
                    "high": str(st.get("high")) if st.get("high") else None,
                    "low": str(st.get("low")) if st.get("low") else None,
                    "base_volume": str(st.get("vol")) if st.get("vol") else None,
                    "source": "kucoin"}
        except Exception as exc:
            self._mark_err(exc); return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        if timeframe not in TF: return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        sym = f"{base}-{quote}"
        try:
            import time as _t
            end = int(_t.time())
            span = TF_SECS.get(TF[timeframe], 3600) * int(limit)
            start = end - span
            url = f"https://api.kucoin.com/api/v1/market/candles?type={TF[timeframe]}&symbol={sym}&startAt={int(since) if since else start}&endAt={end}"
            p = self._http_get(url)
            self._mark_ok()
            rows = sorted(p.get("data") or [], key=lambda r: int(r[0]))
            return [{"t": int(r[0]), "o": str(r[1]), "c": str(r[2]), "h": str(r[3]),
                     "l": str(r[4]), "v": str(r[5])} for r in rows[-int(limit):]]
        except Exception as exc:
            self._mark_err(exc); return None
