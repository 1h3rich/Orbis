"""Contrato común MarketDataProvider (solo datos públicos, sin claves).

Todos los adaptadores ofrecen consultas REST públicas. GRVT usa POST JSON
para perpetuos explícitos; su streaming nativo JSONRPC está pendiente. Las
consultas del terminal GRVT son puntuales, no polling automático.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class MarketDataProvider(Protocol):
    """Interfaz que cumplen los adapters públicos de mercado."""

    name: str
    ws_kind: str  # "json" | "poll" | "none"

    def get_ticker(self, symbol: str) -> dict | None:
        """Ticker normalizado o ``None`` si el par no está disponible."""
        ...

    def get_price(self, symbol: str) -> Decimal | None:
        ...

    def get_24h_stats(self, symbol: str) -> dict | None:
        ...

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        since: int | None = None,
    ) -> list | None:
        """Velas ``[{t,o,h,l,c,v}]`` (``t`` segundos UTC) o ``None`` no disponible."""
        ...

    def get_symbols(self) -> list[str]:
        """Símbolos canónicos ``BASE/QUOTE`` o ``BASE/QUOTE:PERP``."""
        ...

    def supports_pair(self, symbol: str) -> bool:
        ...

    def health(self) -> dict:
        """``{status, detail_human, detail_tech}`` sin lanzar nunca."""
        ...
