"""Contrato de provisión de precios y registro por nombre.

En v1 el precio sigue siendo obligatorio en ``/paper/buy`` y ``/paper/sell``:
los proveedores se usan **solo** para valorar la cartera en
``GET /paper/portfolio``. Los proveedores públicos (Kraken/CoinGecko) son de
solo lectura, sin claves ni secretos, y nunca bloquean una compra/venta: si
la red falla devuelven ``None`` y la valoración recurre al último precio
negociado o al coste medio.
"""

from decimal import Decimal
from typing import Callable, Protocol

from sqlalchemy.orm import Session


class PriceProvider(Protocol):
    """Provee el último precio de un par; ``None`` si no lo conoce."""

    def get_price(self, asset: str, quote_currency: str) -> Decimal | None:
        ...


ProviderFactory = Callable[[Session | None], PriceProvider]

_REGISTRY: dict[str, ProviderFactory] = {}


def register_price_provider(name: str, factory: ProviderFactory) -> None:
    """Registra una fábrica de proveedores bajo un nombre (minúsculas)."""
    _REGISTRY[name.strip().lower()] = factory


def available_price_providers() -> list[str]:
    """Nombres registrados, ordenados, para mostrarlos en la API o en errores."""
    return sorted(_REGISTRY)


def create_price_provider(name: str, db: Session | None = None) -> PriceProvider:
    """Instancia el proveedor registrado; falla con el listado si no existe."""
    key = (name or "").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ValueError(
            f"Unknown price provider {name!r}; "
            f"available: {', '.join(available_price_providers())}"
        )
    return factory(db)
