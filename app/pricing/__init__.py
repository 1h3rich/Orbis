"""Precios de valoración (v1: solo para informar, nunca para ejecutar)."""

from app.pricing.base import (
    PriceProvider,
    available_price_providers,
    create_price_provider,
    register_price_provider,
)
from app.pricing.providers import (
    CoinGeckoPublicPriceProvider,
    KrakenPublicPriceProvider,
    ManualPriceProvider,
    StaticPriceProvider,
)

__all__ = [
    "PriceProvider",
    "CoinGeckoPublicPriceProvider",
    "KrakenPublicPriceProvider",
    "ManualPriceProvider",
    "StaticPriceProvider",
    "available_price_providers",
    "create_price_provider",
    "register_price_provider",
]
