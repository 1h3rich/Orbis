"""Caché en memoria por (provider, par, timeframe).

TTL (frescura coherente con el refresco de la UI, Fase 4b): el refresco Auto
de la UI es 1/4 de vela con topes 1-60 s (``autoRefreshFor`` en estudio.js),
así que la caché de velas usa la MISMA regla —nunca anuncia un refresco más
rápido que el dato real—:

* ``ticker``: 5 s fijos. La UI consulta el último precio en cada tick (hasta
  1/s con TF 1s); 5 s dejan como máximo ~0,2 pet/s al exchange por par/
  proveedor y vista, y el precio mostrado nunca lleva más de 5 s de retraso
  (60 s lo dejaban visiblemente viejo con cualquier temporalidad).
* ``candles``: proporcional al timeframe = ``clamp(redondeo(duración/4),
  1, 60)`` s → 1s→1 s, 30s→8 s (7,5 redondeado, igual que la UI), 1m→15 s,
  5m y mayores→60 s (tope). ``TTL["candles"]`` (60 s) queda como tope/
  fallback para claves sin timeframe legible.
* ``symbols`` 900 s, ``health`` 15 s (sin cambios: datos lentos).

La caché sigue protegiendo el backend de picos: varias peticiones dentro del
TTL se resuelven en caché; solo se baja la ventana para TF rápidos. La clave
de velas ya incluye el timeframe (``(resolved, canon, timeframe, limit,
since)`` en ``service.py``), así que ``get()`` lo extrae de ``key[2]``.

Fuente única de duración de vela en Python: ``TF_SECONDS``/``tf_seconds()``
aquí (los adapters solo mapean nombres de TF a parámetros del exchange, sin
duraciones; réplica comentada de ``TF_SECONDS``/``tfSeconds`` del frontend).

Thread-safe.
"""

from __future__ import annotations

import re
import threading
import time

TTL = {
    "ticker": 5.0,
    "candles": 60.0,
    "symbols": 900.0,
    "health": 15.0,
}

# Duración de cada vela en segundos. ÚNICA fuente en Python (réplica de
# TF_SECONDS de estudio.js: 1s solo existe en binance/okx/gate y 30s solo en
# okx; 1M solo en binance/okx/bybit/mexc/kucoin/bitget (verificado contra las
# APIs; el resto lo rechaza con available:false honesto). 1M ≈ 30 días
# (2592000 s, como la granularidad mensual de referencia): el refresco Auto
# y el TTL caen al tope de 60 s por la regla de 1/4 de vela.
TF_SECONDS = {
    "1s": 1, "30s": 30, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800, "1M": 2592000,
}
_TF_RE = re.compile(r"^(\d+)([smhdw])$")
_TF_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def tf_seconds(timeframe: str) -> float:
    """Duración de la vela en segundos (0 si ilegible; réplica de tfSeconds)."""
    if timeframe in TF_SECONDS:
        return float(TF_SECONDS[timeframe])
    m = _TF_RE.match(str(timeframe or ""))
    if not m:
        return 0.0
    return float(int(m.group(1)) * _TF_MULT[m.group(2)])


def candle_ttl_for(timeframe: str | None) -> float:
    """TTL de velas para un timeframe: 1/4 de vela redondeado, [1, 60] s.

    Idéntica regla que ``autoRefreshFor`` del frontend, así que el TTL
    efectivo nunca supera la frecuencia de refresco anunciada en la UI.
    Timeframe ilegible → 15 s (cadencia media; nunca peor que antes).
    """
    s = tf_seconds(timeframe or "")
    if not s > 0:
        return 15.0
    ttl = round(s / 4)
    return float(min(60, max(1, ttl)))


def ttl_for(kind: str, key: tuple | None = None) -> float:
    """TTL efectivo: velas según su timeframe (``key[2]``), resto de la tabla."""
    if kind == "ticker":
        return TTL["ticker"]
    if kind == "candles":
        tf = None
        try:
            tf = key[2] if key is not None and len(key) >= 3 else None
        except Exception:
            tf = None
        return candle_ttl_for(tf)
    return TTL.get(kind, 60.0)

_lock = threading.Lock()
_store: dict[tuple, tuple[float, object]] = {}


def get(kind: str, key: tuple):
    """Devuelve el valor cacheado o ``None`` si caducó/ausente.

    El TTL es el efectivo de ``ttl_for`` (velas: según su timeframe)."""
    now = time.monotonic()
    with _lock:
        entry = _store.get((kind, key))
        if entry is None:
            return None
        at, value = entry
        if now - at > ttl_for(kind, key):
            _store.pop((kind, key), None)
            return None
        return value


def put(kind: str, key: tuple, value: object):
    """Guarda un valor con marca temporal."""
    with _lock:
        _store[(kind, key)] = (time.monotonic(), value)


def clear():
    """Vacía la caché (pruebas)."""
    with _lock:
        _store.clear()
