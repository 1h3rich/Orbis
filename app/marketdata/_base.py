"""Base REST común: ningún adapter lanza hacia fuera (salud honesta)."""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

from app.marketdata import http as _http
from app.marketdata.normalize import canonical_symbol, native_symbol, parse_symbol


def _dec(value) -> Decimal | None:
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class RestAdapter:
    """Esqueleto con salud AVAILABLE/DEGRADED/UNAVAILABLE y ``http_get`` inyectable."""

    name = "base"
    ws_kind = "json"
    # Timeframes canónicos soportados por el adapter (subset de 1m..1w).
    timeframes: tuple[str, ...] = ()
    # Pares canónicos conocidos (la lista completa sale de /symbols del exchange).
    known_pairs: tuple[str, ...] = ()

    def __init__(self, http_get=None):
        self._http_get = http_get or _http.http_get_json
        self._last_error: str | None = None
        self._last_ok: float | None = None

    # -- helpers ---------------------------------------------------------
    def _mark_ok(self):
        self._last_error = None
        self._last_ok = time.monotonic()

    def _mark_err(self, exc: Exception):
        self._last_error = f"{type(exc).__name__}: {exc}"

    def native(self, symbol: str) -> str:
        base, quote = parse_symbol(symbol)
        return native_symbol(self.name, base, quote)

    def canonical(self, symbol: str) -> str:
        base, quote = parse_symbol(symbol)
        return canonical_symbol(base, quote)

    # -- contrato ---------------------------------------------------------
    def get_ticker(self, symbol: str) -> dict | None:  # pragma: no cover
        raise NotImplementedError

    def get_price(self, symbol: str) -> Decimal | None:
        try:
            ticker = self.get_ticker(symbol)
        except Exception:
            return None
        if not ticker or ticker.get("price") is None:
            return None
        return _dec(ticker["price"])

    def get_24h_stats(self, symbol: str) -> dict | None:
        try:
            return self.get_ticker(symbol)
        except Exception:
            return None

    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        return None  # por defecto: no disponible con motivo en service

    def get_symbols(self) -> list[str]:
        return sorted(self.known_pairs)

    def supports_pair(self, symbol: str) -> bool:
        return self.canonical(symbol) in set(self.get_symbols())

    def health(self) -> dict:
        if self._last_error is None:
            status, human = "AVAILABLE", f"{self.name}: operativo"
        else:
            status, human = "DEGRADED", f"{self.name}: degradado (último fallo guardado)"
        return {
            "provider": self.name,
            "status": status,
            "detail_human": human,
            "detail_tech": self._last_error or "ok",
            "ws": self.ws_kind,
        }
