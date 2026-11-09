"""Implementaciones concretas de precios para valoración PAPER.

Los proveedores públicos (Kraken primario, CoinGecko de recurso) son solo de
LECTURA, sin claves, y sirven únicamente para valorar la cartera PAPER. Nunca
deciden compras/ventas: ``/paper/buy`` y ``/paper/sell`` siguen exigiendo
``price`` en la petición y Guard lo valida. Si la red falla, ``get_price``
devuelve ``None`` (nunca lanza) y la valoración recurre al último precio
negociado o al coste medio.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.database.models import PriceQuote
from app.pricing.base import register_price_provider


def _key(asset: str, quote_currency: str) -> tuple[str, str]:
    return (asset.upper(), quote_currency.upper())


class ManualPriceProvider:
    """
    Lee el precio publicado manualmente en la tabla ``price_quotes``.

    Si el par no tiene cotización, devuelve ``None`` y quien valora decide la
    alternativa (por ejemplo, el último precio negociado en la sesión).
    """

    def __init__(self, db: Session):
        if db is None:
            raise ValueError("ManualPriceProvider requires a database session")
        self.db = db

    def get_price(self, asset: str, quote_currency: str) -> Decimal | None:
        row = (
            self.db.query(PriceQuote)
            .filter(
                PriceQuote.asset == asset,
                PriceQuote.quote_currency == quote_currency,
            )
            .first()
        )
        if row is None:
            return None
        return Decimal(str(row.price))

    def get_price_with_meta(self, asset: str, quote_currency: str) -> dict:
        return get_manual_price_with_meta(self.db, asset, quote_currency)


class StaticPriceProvider:
    """Precios fijos en memoria, útiles para pruebas y simulaciones."""

    def __init__(self, prices: dict[tuple[str, str], Decimal] | None = None):
        self.prices: dict[tuple[str, str], Decimal] = {}
        for (asset, quote), price in (prices or {}).items():
            self.prices[_key(asset, quote)] = Decimal(str(price))

    def get_price(self, asset: str, quote_currency: str) -> Decimal | None:
        return self.prices.get(_key(asset, quote_currency))

    def get_price_with_meta(self, asset: str, quote_currency: str) -> dict:
        price = self.get_price(asset, quote_currency)
        return {
            "price": str(price) if price is not None else None,
            "source": "static" if price is not None else "none",
            "as_of": None,
            "age_seconds": None,
            "stale": True,
        }

    def set_price(self, asset: str, quote_currency: str, price) -> None:
        self.prices[_key(asset, quote_currency)] = Decimal(str(price))


def get_stored_price(db: Session, asset: str, quote_currency: str) -> Decimal | None:
    """Helper compartido para leer la última cotización publicada."""
    return ManualPriceProvider(db).get_price(asset, quote_currency)


def list_prices(db: Session) -> list[PriceQuote]:
    """Cotizaciones publicadas, ordenadas por par."""
    return (
        db.query(PriceQuote)
        .order_by(PriceQuote.asset, PriceQuote.quote_currency)
        .all()
    )


def upsert_price(db: Session, asset: str, quote_currency: str, price) -> PriceQuote:
    """Publica (o actualiza) la cotización manual de un par y confirma."""
    price = Decimal(str(price))
    now = datetime.now(timezone.utc)
    row = (
        db.query(PriceQuote)
        .filter(
            PriceQuote.asset == asset,
            PriceQuote.quote_currency == quote_currency,
        )
        .first()
    )

    if row is None:
        row = PriceQuote(
            asset=asset,
            quote_currency=quote_currency,
            price=price,
            source="MANUAL",
            updated_at=now,
        )
        db.add(row)
    else:
        row.price = price
        # La edad (`age_seconds` de /market/price) se mide desde aquí.
        row.updated_at = now

    db.commit()
    db.refresh(row)
    return row


register_price_provider("manual", lambda db: ManualPriceProvider(db))
register_price_provider("static", lambda db: StaticPriceProvider())


# ---------------------------------------------------------------------------
# Precios públicos de solo lectura (Kraken primario, CoinGecko de recurso).
# Solo stdlib (urllib), timeout 5s, caché en memoria (30s ticker, 5min OHLC).
# Sin claves. Solo valoración PAPER. Nunca lanzan desde get_price.
# ---------------------------------------------------------------------------

HTTP_TIMEOUT = 5.0
TICKER_TTL_SECONDS = 30.0
OHLC_TTL_SECONDS = 300.0
# Tope de entradas por caché (eviction LRU): sin límite un proceso largo
# acumula una entrada por par consultado hasta agotar memoria.
TICKER_CACHE_MAXSIZE = 512
OHLC_CACHE_MAXSIZE = 256

KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

# Activo -> id de CoinGecko (cobertura mínima honesta; sin mapeo no se inventa).
ASSET_TO_COINGECKO_ID: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
}

# Timeframe UI -> intervalo OHLC de Kraken (minutos por vela).
TIMEFRAME_TO_INTERVAL: dict[str, int] = {
    "1h": 1,
    "24h": 60,
    "7d": 1440,
}

TIMEFRAME_TO_WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 7 * 86400,
}


def _history_window(points: list[dict], timeframe: str) -> list[dict]:
    """Recorta la respuesta de hasta 720 velas de Kraken al periodo pedido.

    Se ancla en la última vela disponible, no en el reloj del proceso: así
    una respuesta antigua nunca se mezcla con datos ficticios ni se vacía por
    un desfase del reloj. La fecha final se muestra explícitamente en la UI.
    """
    if not points:
        return []
    last_t = max(int(point["t"]) for point in points)
    cutoff = last_t - TIMEFRAME_TO_WINDOW_SECONDS[timeframe]
    return [point for point in points if int(point["t"]) >= cutoff]


def _history_quality(points: list[dict], interval_minutes: int, now_epoch: float) -> dict:
    """Frescura de la última vela según su fecha de mercado, no la descarga.

    Dos intervalos permiten que la vela actual aún no haya cerrado. Una
    respuesta de red exitosa no convierte en actuales unas velas antiguas.
    """
    if not points:
        return {"stale": True, "note": "Kraken no devolvió velas para este periodo.",
                "last_candle_at": None, "age_seconds": None}
    last_t = max(int(point["t"]) for point in points)
    interval_seconds = interval_minutes * 60
    age = now_epoch - last_t
    try:
        last_at = datetime.fromtimestamp(last_t, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return {"stale": True, "note": "La última vela tiene una fecha inválida.",
                "last_candle_at": None, "age_seconds": None}
    if age < -120:
        note = "La última vela tiene fecha futura; comprueba el reloj y el proveedor."
    elif age > 2 * interval_seconds:
        note = "La última vela es antigua para esta temporalidad."
    else:
        note = None
    return {"stale": note is not None, "note": note,
            "last_candle_at": last_at, "age_seconds": max(0, int(age))}

_ticker_cache: OrderedDict[tuple[str, str, str], tuple[float, Decimal, str]] = OrderedDict()
_ohlc_cache: OrderedDict[tuple[str, str, int], tuple[float, list]] = OrderedDict()
_cache_lock = threading.Lock()


def _lru_set(cache: OrderedDict, key, value, maxsize: int) -> None:
    """Inserta con eviction LRU. Llamar siempre con ``_cache_lock`` tomado."""
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > maxsize:
        cache.popitem(last=False)


def _lru_touch(cache: OrderedDict, key) -> None:
    """Marca uso reciente en un acierto. Llamar con ``_cache_lock`` tomado."""
    try:
        cache.move_to_end(key)
    except KeyError:
        pass


def _http_get_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict:
    """GET JSON con stdlib y timeout corto. Lanza en caso de fallo."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Orbis-PAPER-valuation (public prices, no keys)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Unexpected public price payload")
    return parsed


def _kraken_pair(asset: str, quote_currency: str) -> str:
    return f"{asset.strip().upper()}{quote_currency.strip().upper()}"


def _parse_kraken_ticker(payload: dict) -> dict[str, Decimal]:
    """Extrae último precio cerrado (campo ``c``) por clave de resultado."""
    errors = payload.get("error") or []
    if errors:
        raise ValueError(f"Kraken error: {errors}")
    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        raise ValueError("Kraken ticker without result")
    prices: dict[str, Decimal] = {}
    for key, entry in result.items():
        if not isinstance(entry, dict):
            continue
        close = entry.get("c")
        if not isinstance(close, list) or not close:
            continue
        try:
            prices[str(key)] = Decimal(str(close[0]))
        except (InvalidOperation, ValueError):
            continue
    if not prices:
        raise ValueError("Kraken ticker without close prices")
    return prices


def _parse_kraken_ohlc(payload: dict, pair_key: str | None = None) -> list:
    """Devuelve las velas [[t,o,h,l,c,...]] de la primera clave no-'last'."""
    errors = payload.get("error") or []
    if errors:
        raise ValueError(f"Kraken error: {errors}")
    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        raise ValueError("Kraken OHLC without result")
    key = pair_key
    if key is None or key not in result:
        key = next((k for k in result if k != "last"), None)
    if key is None:
        raise ValueError("Kraken OHLC without candles")
    candles = result[key]
    if not isinstance(candles, list):
        raise ValueError("Kraken OHLC malformed")
    return candles


def fetch_kraken_ticker(
    asset: str,
    quote_currency: str,
    http_get=None,
) -> tuple[Decimal, str]:
    """Precio Kraken (último cierre) para un par. Lanza si falla."""
    getter = http_get or _http_get_json
    pair = _kraken_pair(asset, quote_currency)
    url = f"{KRAKEN_TICKER_URL}?pair={urllib.parse.quote(pair)}"
    payload = getter(url)
    prices = _parse_kraken_ticker(payload)
    # Pedimos un solo par: nos quedamos con el primer precio devuelto.
    first = next(iter(prices.values()))
    fetched_at = datetime.now(timezone.utc).isoformat()
    return (first, fetched_at)


def fetch_kraken_ohlc(
    asset: str,
    quote_currency: str,
    interval: int,
    http_get=None,
) -> list:
    """Velas OHLC de Kraken para un par e intervalo. Lanza si falla."""
    getter = http_get or _http_get_json
    pair = _kraken_pair(asset, quote_currency)
    query = urllib.parse.urlencode({"pair": pair, "interval": int(interval)})
    payload = getter(f"{KRAKEN_OHLC_URL}?{query}")
    return _parse_kraken_ohlc(payload)


def fetch_coingecko_price(
    asset: str,
    quote_currency: str,
    http_get=None,
) -> tuple[Decimal, str]:
    """Precio CoinGecko simple para BTC/ETH. Lanza si falla o sin mapeo."""
    coin_id = ASSET_TO_COINGECKO_ID.get(asset.strip().upper())
    if coin_id is None:
        raise ValueError(f"CoinGecko has no mapping for {asset!r}")
    quote = quote_currency.strip().lower()
    query = urllib.parse.urlencode(
        {"ids": coin_id, "vs_currencies": quote}
    )
    getter = http_get or _http_get_json
    payload = getter(f"{COINGECKO_SIMPLE_PRICE_URL}?{query}")
    entry = payload.get(coin_id)
    if not isinstance(entry, dict) or entry.get(quote) is None:
        raise ValueError("CoinGecko price missing")
    fetched_at = datetime.now(timezone.utc).isoformat()
    return (Decimal(str(entry[quote])), fetched_at)


def get_public_ticker_price(
    asset: str,
    quote_currency: str,
    http_get=None,
) -> tuple[Decimal | None, str, str | None, bool]:
    """Kraken -> CoinGecko. Devuelve (precio|None, fuente, as_of|None, stale)."""
    cache_key = ("public", asset.strip().upper(), quote_currency.strip().upper())
    now = time.monotonic()
    if http_get is None:
        with _cache_lock:
            cached = _ticker_cache.get(cache_key)
        if cached is not None:
            fetched_mono, price, as_of = cached
            age = now - fetched_mono
            if age <= TICKER_TTL_SECONDS:
                with _cache_lock:
                    _lru_touch(_ticker_cache, cache_key)
                return (price, "kraken-cache", as_of, False)

    try:
        price, as_of = fetch_kraken_ticker(asset, quote_currency, http_get=http_get)
        if http_get is None:
            with _cache_lock:
                _lru_set(_ticker_cache, cache_key, (now, price, as_of), TICKER_CACHE_MAXSIZE)
        return (price, "kraken", as_of, False)
    except Exception:
        pass

    try:
        price, as_of = fetch_coingecko_price(asset, quote_currency, http_get=http_get)
        return (price, "coingecko", as_of, True)
    except Exception:
        pass

    if http_get is None:
        with _cache_lock:
            cached = _ticker_cache.get(cache_key)
        if cached is not None:
            _, price, as_of = cached
            return (price, "stale-cache", as_of, True)
    return (None, "none", None, True)


def get_public_history(
    asset: str,
    quote_currency: str,
    timeframe: str,
    http_get=None,
    *,
    now_epoch: float | None = None,
) -> dict:
    """OHLC Kraken para 1h/24h/7d. ``live`` no tiene histórico (nota honesta)."""
    if timeframe == "live":
        price, source, as_of, stale = get_public_ticker_price(
            asset, quote_currency, http_get=http_get
        )
        return {
            "timeframe": "live",
            "interval": None,
            "source": source,
            "points": [],
            "stale": stale if price is None else stale,
            "last_candle_at": None,
            "age_seconds": None,
            "note": (
                "Vista live: precio actual con refresco periódico; "
                "sin serie histórica en este modo."
            ),
        }
    if timeframe not in TIMEFRAME_TO_INTERVAL:
        raise ValueError(f"Unknown timeframe {timeframe!r}; expected live|1h|24h|7d")

    interval = TIMEFRAME_TO_INTERVAL[timeframe]

    def response(source: str, points: list[dict], *, force_stale: bool = False,
                 note: str | None = None) -> dict:
        window = _history_window(points, timeframe)
        quality = _history_quality(window, interval,
                                   time.time() if now_epoch is None else now_epoch)
        return {"timeframe": timeframe, "interval": interval, "source": source,
                "points": window, "stale": force_stale or quality["stale"],
                "note": note or quality["note"],
                "last_candle_at": quality["last_candle_at"],
                "age_seconds": quality["age_seconds"]}

    cache_key = (
        asset.strip().upper(),
        quote_currency.strip().upper(),
        interval,
    )
    now = time.monotonic()
    if http_get is None:
        with _cache_lock:
            cached = _ohlc_cache.get(cache_key)
        if cached is not None:
            fetched_mono, points = cached
            if now - fetched_mono <= OHLC_TTL_SECONDS:
                with _cache_lock:
                    _lru_touch(_ohlc_cache, cache_key)
                return response("kraken-cache", points)

    try:
        candles = fetch_kraken_ohlc(asset, quote_currency, interval, http_get=http_get)
        points = []
        for c in candles:
            if not isinstance(c, list) or len(c) < 5:
                continue
            try:
                pt = {"t": int(c[0]), "close": str(c[4])}
            except (ValueError, TypeError, IndexError):
                continue
            # OHLC completo para la vista exchange (velas simples en canvas).
            # Se mantiene `close` por compatibilidad; el resto es presentación.
            try:
                pt["open"] = str(c[1])
                pt["high"] = str(c[2])
                pt["low"] = str(c[3])
            except IndexError:
                pass
            if len(c) >= 7:
                try:
                    pt["volume"] = str(c[6])
                except (ValueError, TypeError):
                    pass
            points.append(pt)
        if http_get is None:
            with _cache_lock:
                _lru_set(_ohlc_cache, cache_key, (now, points), OHLC_CACHE_MAXSIZE)
        return response("kraken", points)
    except Exception:
        if http_get is None:
            with _cache_lock:
                cached = _ohlc_cache.get(cache_key)
            if cached is not None:
                _, points = cached
                return response("stale-cache", points, force_stale=True,
                                note="OHLC no disponible ahora; se muestra la última serie en caché.")
        return response("none", [], force_stale=True, note=(
                "Sin histórico disponible para este par/timeframe "
                "(red o proveedor no disponible). No se inventan datos."
            ))


def get_manual_price_with_meta(
    db: Session | None,
    asset: str,
    quote_currency: str,
    source: str = "manual",
) -> dict:
    """Meta honesta sin red: precio manual + edad real desde ``updated_at``.

    Nunca toca la red. Si no hay cotización publicada, ``price`` es ``None``,
    ``source`` es ``"none"`` y ``age_seconds`` es ``None`` (nunca 0).
    """

    asset_n = asset.strip().upper()
    quote_n = quote_currency.strip().upper()
    price: Decimal | None = None
    as_of: str | None = None
    age_seconds: float | None = None
    row = None
    if db is not None:
        try:
            row = (
                db.query(PriceQuote)
                .filter(
                    PriceQuote.asset == asset_n,
                    PriceQuote.quote_currency == quote_n,
                )
                .first()
            )
        except Exception:
            row = None
    if row is not None:
        try:
            price = Decimal(str(row.price))
        except (InvalidOperation, ValueError):
            price = None
    updated = getattr(row, "updated_at", None) if row is not None else None
    if price is not None and updated is not None:
        if updated.tzinfo is None:
            # SQLite devuelve datetimes naive: son UTC.
            updated = updated.replace(tzinfo=timezone.utc)
        as_of = updated.isoformat()
        age_seconds = max(
            0.0, (datetime.now(timezone.utc) - updated).total_seconds()
        )
    return {
        "asset": asset_n,
        "quote_currency": quote_n,
        "price": str(price) if price is not None else None,
        "source": source if price is not None else "none",
        "as_of": as_of,
        "age_seconds": age_seconds,
        "stale": True,
        "manual_fallback": False,
    }


def get_configured_price_with_meta(
    db: Session | None,
    asset: str,
    quote_currency: str,
    provider_name: str | None = None,
) -> dict:
    """Precio + edad + fuente según ``ORBIS_PRICE_PROVIDER`` (solo valoración).

    ``kraken`` mantiene el primario con recurso a CoinGecko y a manual;
    ``coingecko`` usa CoinGecko con recurso a manual; ``manual``/``static``
    (y cualquier nombre desconocido, por seguridad) no tocan la red.
    """

    from app.core.config import get_orbis_config

    if provider_name is None:
        provider_name = get_orbis_config().price_provider
    name = (provider_name or "manual").strip().lower() or "manual"
    if name == "kraken":
        return KrakenPublicPriceProvider(db).get_price_with_meta(
            asset, quote_currency
        )
    if name == "coingecko":
        return CoinGeckoPublicPriceProvider(db).get_price_with_meta(
            asset, quote_currency
        )
    if name == "static":
        price = StaticPriceProvider().get_price(asset, quote_currency)
        asset_n = asset.strip().upper()
        quote_n = quote_currency.strip().upper()
        return {
            "asset": asset_n,
            "quote_currency": quote_n,
            "price": str(price) if price is not None else None,
            "source": "static" if price is not None else "none",
            "as_of": None,
            "age_seconds": None,
            "stale": True,
            "manual_fallback": False,
        }
    return get_manual_price_with_meta(db, asset, quote_currency, source="manual")


def get_configured_history(
    db: Session | None,
    asset: str,
    quote_currency: str,
    timeframe: str,
    provider_name: str | None = None,
    http_get=None,
) -> dict:
    """OHLC/histórico según el proveedor configurado, sin inventar datos.

    Solo ``kraken`` sirve velas OHLC. El resto no tiene serie: ``live``
    devuelve el precio actual del proveedor configurado (sin serie) y
    ``1h``/``24h``/``7d`` una nota honesta. Nunca toca la red salvo kraken
    (primario) o coingecko en vista ``live``.
    """

    from app.core.config import get_orbis_config

    if provider_name is None:
        provider_name = get_orbis_config().price_provider
    name = (provider_name or "manual").strip().lower() or "manual"
    if name == "kraken":
        return get_public_history(asset, quote_currency, timeframe, http_get=http_get)
    if timeframe not in ("live", *TIMEFRAME_TO_INTERVAL):
        raise ValueError(
            f"Unknown timeframe {timeframe!r}; expected live|1h|24h|7d"
        )
    label = name if name in ("manual", "static", "coingecko") else "manual"
    if timeframe == "live":
        if name == "coingecko":
            meta = CoinGeckoPublicPriceProvider(db).get_price_with_meta(
                asset, quote_currency
            )
        elif name == "static":
            meta = get_configured_price_with_meta(db, asset, quote_currency, "static")
        else:
            meta = get_manual_price_with_meta(db, asset, quote_currency, "manual")
        return {
            "timeframe": "live",
            "interval": None,
            "source": meta["source"],
            "points": [],
            "stale": meta["stale"],
            "note": (
                "Vista live: precio actual con refresco periódico; "
                "sin serie histórica en este modo."
            ),
        }
    interval = TIMEFRAME_TO_INTERVAL[timeframe]
    return {
        "timeframe": timeframe,
        "interval": interval,
        "source": label,
        "points": [],
        "stale": True,
        "note": (
            f"El proveedor configurado ({label}) no sirve OHLC para este "
            "par/timeframe. No se inventan datos."
        ),
    }


class KrakenPublicPriceProvider:
    """Kraken público (primario EUR) con recurso a CoinGecko; nunca lanza.

    Solo valoración PAPER. Si todo falla, ``get_price`` devuelve ``None`` y
    quien valora usa el último precio negociado o el coste medio.
    """

    def __init__(self, db: Session | None = None, http_get=None):
        self.db = db
        self._http_get = http_get

    def get_price(self, asset: str, quote_currency: str) -> Decimal | None:
        try:
            price, _source, _as_of, _stale = get_public_ticker_price(
                asset, quote_currency, http_get=self._http_get
            )
            if price is not None:
                return Decimal(str(price))
        except Exception:
            pass
        if self.db is not None:
            try:
                return ManualPriceProvider(self.db).get_price(asset, quote_currency)
            except Exception:
                return None
        return None

    def get_price_with_meta(self, asset: str, quote_currency: str) -> dict:
        """Precio + edad + fuente para la cabecera del dashboard."""
        before = time.monotonic()
        try:
            price, source, as_of, stale = get_public_ticker_price(
                asset, quote_currency, http_get=self._http_get
            )
        except Exception:
            price, source, as_of, stale = None, "none", None, True
        age_seconds: float | None = None
        if price is not None and self._http_get is None:
            cache_key = ("public", asset.strip().upper(), quote_currency.strip().upper())
            with _cache_lock:
                cached = _ticker_cache.get(cache_key)
            if cached is not None:
                age_seconds = max(0.0, time.monotonic() - cached[0])
            else:
                age_seconds = max(0.0, time.monotonic() - before)
        manual_used = False
        if price is None and self.db is not None:
            try:
                manual = ManualPriceProvider(self.db).get_price(asset, quote_currency)
            except Exception:
                manual = None
            if manual is not None:
                price, source, stale, manual_used = manual, "manual", True, True
                # Edad real desde `updated_at` (C-06): la caché Kraken está
                # vacía en este camino y medir desde ella daría ~0s falso.
                meta = get_manual_price_with_meta(
                    self.db, asset, quote_currency, source="manual"
                )
                as_of = meta["as_of"]
                age_seconds = meta["age_seconds"]
        return {
            "asset": asset.strip().upper(),
            "quote_currency": quote_currency.strip().upper(),
            "price": str(price) if price is not None else None,
            "source": source,
            "as_of": as_of,
            "age_seconds": age_seconds,
            "stale": bool(stale) if price is not None else True,
            "manual_fallback": manual_used,
        }


class CoinGeckoPublicPriceProvider:
    """CoinGecko público (recurso, BTC/ETH); nunca lanza desde get_price."""

    def __init__(self, db: Session | None = None, http_get=None):
        self.db = db
        self._http_get = http_get

    def get_price(self, asset: str, quote_currency: str) -> Decimal | None:
        try:
            price, _as_of = fetch_coingecko_price(
                asset, quote_currency, http_get=self._http_get
            )
            return Decimal(str(price))
        except Exception:
            pass
        if self.db is not None:
            try:
                return ManualPriceProvider(self.db).get_price(asset, quote_currency)
            except Exception:
                return None
        return None

    def get_price_with_meta(self, asset: str, quote_currency: str) -> dict:
        """Precio + edad + fuente con CoinGecko como primario y manual de recurso."""
        asset_n = asset.strip().upper()
        quote_n = quote_currency.strip().upper()
        try:
            price, as_of = fetch_coingecko_price(
                asset, quote_currency, http_get=self._http_get
            )
            return {
                "asset": asset_n,
                "quote_currency": quote_n,
                "price": str(price),
                "source": "coingecko",
                "as_of": as_of,
                "age_seconds": 0.0,
                "stale": False,
                "manual_fallback": False,
            }
        except Exception:
            pass
        if self.db is not None:
            meta = get_manual_price_with_meta(
                self.db, asset, quote_currency, source="manual"
            )
            if meta["price"] is not None:
                meta["manual_fallback"] = True
                return meta
        return {
            "asset": asset_n,
            "quote_currency": quote_n,
            "price": None,
            "source": "none",
            "as_of": None,
            "age_seconds": None,
            "stale": True,
            "manual_fallback": False,
        }


register_price_provider("kraken", lambda db: KrakenPublicPriceProvider(db))
register_price_provider("coingecko", lambda db: CoinGeckoPublicPriceProvider(db))
