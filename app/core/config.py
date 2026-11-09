import os

from pydantic import BaseModel, Field


class OrbisConfig(BaseModel):
    """Valores por defecto del proyecto y selector de proveedor de precios."""

    app_name: str = "Orbis"
    version: str = "0.1.0"
    paper_mode: bool = True
    base_currency: str = "EUR"
    # Nombre registrado en `app.pricing`: kraken|coingecko|manual|static
    # (default: kraken). Solo se usa para valorar la cartera PAPER
    # (el precio de compra/venta sigue siendo obligatorio en la petición).
    price_provider: str = Field(default="kraken", pattern=r"^[a-z0-9_-]{1,32}$")
    # Proveedor por defecto de la TERMINAL (adapters públicos, sin CCXT):
    # kraken|binance|coinbase|okx|bybit|mexc|kucoin|gate|bitget|grvt|bitstamp.
    # Env ORBIS_MARKET_PROVIDER; la UI lo persiste en localStorage
    # (orbis.provider.default) y siempre muestra resolved_provider.
    market_provider: str = Field(default="kraken", pattern=r"^[a-z0-9_-]{1,32}$")


def get_orbis_config() -> OrbisConfig:
    """
    Configuración de la instancia.

    ``ORBIS_PRICE_PROVIDER`` permite elegir proveedor sin tocar código:
    ``kraken`` (público, primario EUR, sin claves, por defecto para demo
    local), ``coingecko`` (público
    de recurso, BTC/ETH, con rate-limit), ``manual`` (tabla `price_quotes`)
    o ``static`` (memoria, pruebas). Todos son solo valoración
    PAPER: nunca ejecutan órdenes ni sustituyen el `price` obligatorio.
    """
    return OrbisConfig(
        price_provider=os.environ.get("ORBIS_PRICE_PROVIDER", "kraken").strip().lower(),
        market_provider=os.environ.get("ORBIS_MARKET_PROVIDER", "kraken").strip().lower(),
    )
