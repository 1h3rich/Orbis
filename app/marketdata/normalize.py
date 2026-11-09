"""Normalización canónica de pares (sin inventar disponibilidad).

* Canónico: ``BASE/QUOTE`` en mayúsculas (``btc/eur`` -> ``BTC/EUR``).
* ``XBT`` -> ``BTC`` (alias histórico de Kraken; ``XXBT`` también).
* ``QUOTE_ALIASES`` normaliza el nombre pero **no fusiona** EUR/USDT/USDC:
  son mercados distintos.
* ``native_symbol(provider, base, quote)`` traduce al formato nativo del
  exchange según tabla por adapter (``XXBTZEUR``, ``BTCUSDT``, ``BTC-USDT``,
  ``BTC/USDT``, ``BTC_USDT`` ...). Si el adapter no soporta el par, el
  servicio responde "no disponible" con motivo, nunca inventa velas.
"""

from __future__ import annotations

BASE_ALIASES: dict[str, str] = {
    "XBT": "BTC",
    "XXBT": "BTC",
}

# Alias de escritura -> canónico. No fusiona: EUR != USDT != USDC.
QUOTE_ALIASES: dict[str, str] = {
    "EUR": "EUR",
    "EURO": "EUR",
    "USD": "USD",
    "USDT": "USDT",
    "USDC": "USDC",
    "BTC": "BTC",
    "ETH": "ETH",
}

# Formato nativo por proveedor: función (base, quote) -> símbolo nativo.
NATIVE_FORMATS: dict[str, str] = {
    "kraken": "{BASE}{QUOTE}",      # XXBTZEUR (con tabla especial Kraken)
    "binance": "{BASE}{QUOTE}",     # BTCUSDT
    "coinbase": "{BASE}-{QUOTE}",   # BTC-USDT
    "okx": "{BASE}-{QUOTE}",        # BTC-USDT
    "bybit": "{BASE}{QUOTE}",       # BTCUSDT
    "mexc": "{BASE}{QUOTE}",        # BTCUSDT
    "kucoin": "{BASE}-{QUOTE}",     # BTC-USDT
    "gate": "{BASE}_{QUOTE}",       # BTC_USDT
    "bitget": "{BASE}{QUOTE}",      # BTCUSDT
    "grvt": "{BASE}_{QUOTE}_Perp",  # GRVT sólo perpetuos explícitos
}

# Nombres nativos Kraken (los únicos con prefijo X/Z histórico).
KRAKEN_NATIVES: dict[tuple[str, str], str] = {
    ("BTC", "EUR"): "XXBTZEUR",
    ("BTC", "USD"): "XXBTZUSD",
    ("BTC", "USDT"): "XBTUSDT",
    ("ETH", "EUR"): "XETHZEUR",
    ("ETH", "USD"): "XETHZUSD",
    ("ETH", "USDT"): "ETHUSDT",
    ("SOL", "EUR"): "SOLEUR",
    ("SOL", "USD"): "SOLUSD",
    ("SOL", "USDT"): "SOLUSDT",
}


def canonical_base(raw: str) -> str:
    """Mayús + alias XBT->BTC."""
    text = (raw or "").strip().upper()
    return BASE_ALIASES.get(text, text)


def canonical_quote(raw: str) -> str:
    """Mayús + alias de escritura, sin fusionar monedas distintas."""
    text = (raw or "").strip().upper()
    return QUOTE_ALIASES.get(text, text)


def canonical_symbol(base: str, quote: str) -> str:
    """``BTC/EUR`` canónico."""
    return f"{canonical_base(base)}/{canonical_quote(quote)}"


def parse_symbol(symbol: str) -> tuple[str, str]:
    """Acepta ``BTC/EUR``, ``BTC-EUR``, ``BTC_EUR``, ``BTCEUR`` (6-8)."""
    text = (symbol or "").strip().upper().replace("-", "/").replace("_", "/")
    if "/" in text:
        base, _, quote = text.partition("/")
        return (canonical_base(base), canonical_quote(quote))
    # Sin separador: últimos 3-4 como quote (EUR/USD/USDT/USDC).
    for size in (4, 3):
        if len(text) > size:
            return (
                canonical_base(text[:-size]),
                canonical_quote(text[-size:]),
            )
    return (canonical_base(text), "")


def native_symbol(provider: str, base: str, quote: str) -> str:
    """Símbolo nativo del exchange para un par canónico."""
    name = (provider or "").strip().lower()
    b = canonical_base(base)
    q = canonical_quote(quote)
    if name == "grvt":
        if not q.endswith(":PERP"):
            raise ValueError("GRVT requiere instrumento explícito BASE/QUOTE:PERP")
        return f"{b}_{q.removesuffix(':PERP')}_Perp"
    if name == "kraken" and (b, q) in KRAKEN_NATIVES:
        return KRAKEN_NATIVES[(b, q)]
    fmt = NATIVE_FORMATS.get(name, "{BASE}/{QUOTE}")
    return fmt.replace("{BASE}", b).replace("{QUOTE}", q)


def alternatives(
    base: str,
    provider: str,
    available: list[str],
    quote: str | None = None,
) -> dict:
    """Pares alternativos disponibles para una base en un proveedor.

    Si ``BTC/EUR`` no existe en X pero sí ``BTC/USDT``, se devuelve la
    alternativa con el mensaje honesto ``BTC/EUR no disponible en X``.
    """
    b = canonical_base(base)
    matches = [s for s in (available or []) if s.split("/")[0] == b]
    return {
        "base": b,
        "provider": (provider or "").lower(),
        "requested_quote": canonical_quote(quote) if quote else None,
        "available": sorted(matches),
        "message": (
            f"{b}/{canonical_quote(quote) if quote else '?'} no disponible en "
            f"{provider}; alternativas: "
            + (", ".join(sorted(matches)) if matches else "ninguna")
        ),
    }
