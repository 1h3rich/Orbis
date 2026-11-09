"""Coinbase Exchange REST (público, sin claves)."""
from __future__ import annotations
from urllib.parse import urlencode

from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol

# Coinbase Exchange sólo acepta 60, 300, 900, 3600, 21600 y 86400 s.
# ORBIS aún no ofrece 6h; 30m y 4h no son granularidades nativas.
GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

class CoinbaseAdapter(RestAdapter):
    name = "coinbase"
    ws_kind = "json"
    timeframes = tuple(GRAN)
    known_pairs = ("BTC/USD", "BTC/USDT", "BTC/EUR", "ETH/USD", "ETH/EUR", "SOL/USD",
                   "ADA/USD", "DOT/USD", "MATIC/USD", "AVAX/USD", "LINK/USD", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        pid = f"{base}-{quote}"
        try:
            p = self._http_get(f"https://api.exchange.coinbase.com/products/{pid}/ticker")
            s = self._http_get(f"https://api.exchange.coinbase.com/products/{pid}/stats")
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(p.get("price")),
                    "bid": str(p.get("bid")) if p.get("bid") else None,
                    "ask": str(p.get("ask")) if p.get("ask") else None,
                    "high": str(s.get("high")) if isinstance(s, dict) and s.get("high") else None,
                    "low": str(s.get("low")) if isinstance(s, dict) and s.get("low") else None,
                    "base_volume": str(s.get("volume")) if isinstance(s, dict) and s.get("volume") else None,
                    "source": "coinbase"}
        except Exception as exc:
            self._mark_err(exc)
            return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        if timeframe not in GRAN:
            return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        pid = f"{base}-{quote}"
        try:
            params = {"granularity": GRAN[timeframe]}
            page_size = min(300, max(1, int(limit)))
            if since is not None:
                params["start"] = int(since)
                params["end"] = int(since) + min(299, page_size) * GRAN[timeframe]
            raw = self._http_get(
                f"https://api.exchange.coinbase.com/products/{pid}/candles?{urlencode(params)}"
            )
            self._mark_ok()
            rows = sorted(raw, key=lambda r: r[0])
            if since is None:
                rows = rows[-page_size:]
            else:
                rows = [row for row in rows if int(row[0]) >= int(since)][:page_size]
            out = [{"t": int(r[0]), "l": str(r[1]), "h": str(r[2]), "o": str(r[3]),
                    "c": str(r[4]), "v": str(r[5])} for r in rows]
            return out
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_candles_before(self, symbol, timeframe="1h", limit=200, *, before):
        if timeframe not in GRAN or int(before) <= 0:
            return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs:
            return None
        # Coinbase no publica buckets sin operaciones. Una página vacía (o
        # una respuesta que sólo solapa el límite) no acredita que no exista
        # historia anterior: se prueban unas pocas ventanas previas, nunca
        # más de 299 buckets por solicitud ni una búsqueda ilimitada.
        page_size = min(300, max(1, int(limit)))
        cursor = int(before)
        try:
            for _ in range(8):
                end = cursor - 1
                if end <= 0:
                    break
                start = max(0, end - 299 * GRAN[timeframe])
                url = f"https://api.exchange.coinbase.com/products/{base}-{quote}/candles?" + urlencode({
                    "granularity": GRAN[timeframe], "start": start, "end": end,
                })
                raw = self._http_get(url)
                rows = sorted((row for row in raw if int(row[0]) < int(before)),
                              key=lambda row: row[0])[-page_size:]
                if rows:
                    self._mark_ok()
                    return [{"t": int(row[0]), "l": str(row[1]), "h": str(row[2]),
                             "o": str(row[3]), "c": str(row[4]), "v": str(row[5])}
                            for row in rows]
                if start == 0:
                    break
                cursor = start
            self._mark_ok()
            return []
        except Exception as exc:
            self._mark_err(exc)
            return None
