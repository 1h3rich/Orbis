"""MEXC Spot REST (público, sin claves)."""
from __future__ import annotations
from datetime import datetime, timezone

from app.marketdata.cache import TF_SECONDS
from app.marketdata._base import RestAdapter
from app.marketdata.normalize import parse_symbol
TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "4h": "4h", "1d": "1d", "1w": "1W", "1M": "1M"}
MEXC_HISTORICAL_PAGE_SIZE = 500
class MexcAdapter(RestAdapter):
    name = "mexc"; ws_kind = "json"; timeframes = tuple(TF)
    known_pairs = ("BTC/USDT", "BTC/USDC", "BTC/EUR", "ETH/USDT", "SOL/USDT", "BNB/USDT",
                   "ADA/USDT", "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT", "ETH/BTC")
    def get_ticker(self, symbol):
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        sym = f"{base}{quote}"
        try:
            p = self._http_get(f"https://api.mexc.com/api/v3/ticker/24hr?symbol={sym}")
            if not isinstance(p, dict) or p.get("symbol") != sym:
                raise ValueError("Ticker MEXC de otro mercado o sin símbolo")
            self._mark_ok()
            return {"symbol": f"{base}/{quote}", "price": str(p.get("lastPrice")),
                    "bid": str(p.get("bidPrice")) if p.get("bidPrice") else None,
                    "ask": str(p.get("askPrice")) if p.get("askPrice") else None,
                    "high": str(p.get("highPrice")) if p.get("highPrice") else None,
                    "low": str(p.get("lowPrice")) if p.get("lowPrice") else None,
                    "change_pct": str(p.get("priceChangePercent")) if p.get("priceChangePercent") else None,
                    "base_volume": str(p.get("volume")) if p.get("volume") else None,
                    "quote_volume": str(p.get("quoteVolume")) if p.get("quoteVolume") else None,
                    "source": "mexc"}
        except Exception as exc:
            self._mark_err(exc); return None

    def get_paper_quote(self, symbol: str) -> dict | None:
        """Ticker MEXC con identidad y tiempo verificables para PAPER DCA.

        `closeTime` es la marca del endpoint 24h, no una confirmación de
        operación. Se conserva separada del momento de recepción local.
        """
        base, quote = parse_symbol(symbol)
        pair = f"{base}/{quote}"
        if pair not in self.known_pairs:
            return None
        native = f"{base}{quote}"
        try:
            row = self._http_get(f"https://api.mexc.com/api/v3/ticker/24hr?symbol={native}")
            if not isinstance(row, dict) or row.get("symbol") != native:
                raise ValueError("Ticker MEXC de otro mercado")
            timestamp_ms = int(row["closeTime"])
            if timestamp_ms <= 0:
                raise ValueError("Ticker MEXC sin tiempo válido")
            result = {"symbol": pair, "source": "mexc-spot-public",
                      "event_time_ns": timestamp_ms * 1_000_000,
                      "bid": str(row["bidPrice"]), "ask": str(row["askPrice"]),
                      "last": str(row["lastPrice"])}
            self._mark_ok()
            return result
        except Exception as exc:
            self._mark_err(exc)
            return None
    def get_candles(self, symbol, timeframe="1h", limit=200, since=None, before=None):
        if timeframe not in TF: return None
        base, quote = parse_symbol(symbol)
        if f"{base}/{quote}" not in self.known_pairs: return None
        sym = f"{base}{quote}"
        try:
            url = f"https://api.mexc.com/api/v3/klines?symbol={sym}&interval={TF[timeframe]}&limit={int(limit)}"
            if since is not None: url += f"&startTime={int(since) * 1000}"
            if before is not None: url += f"&endTime={int(before) * 1000 - 1}"
            raw = self._http_get(url)
            self._mark_ok()
            return [{"t": int(int(c[0]) / 1000), "o": str(c[1]), "h": str(c[2]),
                     "l": str(c[3]), "c": str(c[4]), "v": str(c[5])} for c in raw]
        except Exception as exc:
            self._mark_err(exc); return None

    def get_candles_before(self, symbol, timeframe="1h", limit=200, *, before):
        # MEXC publica ambos parámetros, pero en la API observada endTime solo
        # se ignora: la ventana histórica requiere también startTime.
        if timeframe not in TF or int(before) <= 0:
            return None
        if timeframe == "1M":
            dt = datetime.fromtimestamp(int(before), timezone.utc)
            month_index = dt.year * 12 + dt.month - 1 - int(limit)
            start = 0 if month_index < 12 else int(datetime(
                month_index // 12, month_index % 12 + 1, 1,
                tzinfo=timezone.utc,
            ).timestamp())
        else:
            start = max(0, int(before) - TF_SECONDS[timeframe] * int(limit))
        return self.get_candles(symbol, timeframe=timeframe, limit=limit,
                                since=start, before=before)
