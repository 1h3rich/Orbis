"""Registry de proveedores públicos + resolución AUTO sin mezclar silencioso."""

from __future__ import annotations

import os

from app.marketdata.adapters.binance import BinanceAdapter
from app.marketdata.adapters.bitstamp import BitstampAdapter
from app.marketdata.adapters.bitget import BitgetAdapter
from app.marketdata.adapters.bybit import BybitAdapter
from app.marketdata.adapters.coinbase import CoinbaseAdapter
from app.marketdata.adapters.gate import GateAdapter
from app.marketdata.adapters.grvt import GrvtAdapter
from app.marketdata.adapters.kraken import KrakenAdapter
from app.marketdata.adapters.kucoin import KucoinAdapter
from app.marketdata.adapters.mexc import MexcAdapter
from app.marketdata.adapters.okx import OkxAdapter

_FACTORIES = {
    "kraken": KrakenAdapter,
    "binance": BinanceAdapter,
    "coinbase": CoinbaseAdapter,
    "okx": OkxAdapter,
    "bybit": BybitAdapter,
    "mexc": MexcAdapter,
    "kucoin": KucoinAdapter,
    "gate": GateAdapter,
    "bitget": BitgetAdapter,
    "grvt": GrvtAdapter,
    "bitstamp": BitstampAdapter,
}

ORDER = ["kraken", "binance", "coinbase", "okx", "bybit", "mexc", "kucoin", "gate", "bitget", "grvt", "bitstamp"]

_instances: dict[str, object] = {}


def available_providers() -> list[str]:
    """Nombres ordenados de los proveedores."""
    return list(ORDER)


def default_provider() -> str:
    """Default persistente: env ORBIS_MARKET_PROVIDER o kraken."""
    return (os.environ.get("ORBIS_MARKET_PROVIDER", "kraken") or "kraken").strip().lower() or "kraken"


def get_provider(name: str, http_get=None):
    """Instancia (memoizada salvo inyección de dobles en pruebas)."""
    key = (name or "").strip().lower() or default_provider()
    if key not in _FACTORIES:
        raise ValueError(f"Unknown market provider {name!r}; available: {', '.join(ORDER)}")
    if http_get is not None:
        return _FACTORIES[key](http_get=http_get)
    if key not in _instances:
        _instances[key] = _FACTORIES[key]()
    return _instances[key]


def resolve_provider(requested: str | None, symbol: str | None = None, http_get=None,
                     timeframe: str | None = None):
    """Resuelve el proveedor efectivo.

    * ``requested`` concreto -> ese (``resolved_provider`` visible).
    * ``auto``/vacío -> primer provider que soporte el par y, si se solicita,
      la temporalidad nativa (sin mezclar silencioso: se devuelve
      ``resolved_provider`` y la UI lo muestra).
    * Si ninguno lo soporta -> ``(None, motivo)`` honesto.
    """
    req = (requested or "").strip().lower() or "auto"
    if req not in ("auto", ""):
        return get_provider(req, http_get=http_get), req
    if symbol:
        for name in ORDER:
            try:
                provider = get_provider(name, http_get=http_get)
                if (provider.supports_pair(symbol) and
                        (timeframe is None or timeframe in (getattr(provider, "timeframes", ()) or ()))):
                    return provider, name
            except Exception:
                continue
        return None, "auto"
    return get_provider(default_provider(), http_get=http_get), default_provider()
