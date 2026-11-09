"""Archivos diarios BTC/USD y BTC/EUR; sin interpolar días faltantes."""

from __future__ import annotations

import threading
import time
import hashlib
import json
import os
import stat
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.marketdata.adapters.bitstamp import BitstampAdapter, DAY

START = int(datetime(2012, 1, 1, tzinfo=timezone.utc).timestamp())
START_EUR = int(datetime(2016, 4, 16, tzinfo=timezone.utc).timestamp())
_lock = threading.Lock()
_cached: dict[str, tuple[float, dict]] = {}
CACHE_DIR = Path(os.environ.get("ORBIS_MARKET_CACHE_DIR", "~/.local/share/orbis/market-cache")).expanduser()
MAX_CACHE_BYTES = 8 * 1024 * 1024


def _cache_file(symbol: str) -> Path:
    return CACHE_DIR / ("btc-usd-daily-v1.json" if symbol == "BTC/USD" else "btc-eur-daily-v1.json")


def _digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_cached_result(result: object, symbol: str, start: int, today: int) -> bool:
    if not isinstance(result, dict) or result.get("available") is not True or result.get("symbol") != symbol:
        return False
    if result.get("resolved_provider") != "bitstamp" or result.get("timeframe") != "1d":
        return False
    if result.get("source") != "Bitstamp OHLC público":
        return False
    as_of = result.get("as_of_day")
    rows = result.get("candles")
    if type(as_of) is not int or as_of < start or as_of > today or as_of % DAY:
        return False
    if not isinstance(rows, list) or not 1 <= len(rows) <= 12000:
        return False
    gaps = 0
    complete_through = start
    contiguous = True
    previous = None
    for bar in rows:
        if not isinstance(bar, dict) or type(bar.get("t")) is not int or bar.get("closed") is not True:
            return False
        t = bar["t"]
        if t < start or t >= as_of or t % DAY or (previous is not None and t <= previous):
            return False
        if any(not isinstance(bar.get(key), str) or len(bar[key]) > 80
               for key in ("o", "h", "l", "c", "v")):
            return False
        try:
            o, h, l, c, v = (Decimal(bar[key]) for key in ("o", "h", "l", "c", "v"))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return False
        if (not all(value.is_finite() for value in (o, h, l, c, v))
                or min(o, h, l, c) <= 0 or v < 0 or l > min(o, c) or h < max(o, c)):
            return False
        if previous is not None:
            missing = (t - previous) // DAY - 1
            gaps += missing
            if missing:
                contiguous = False
        if contiguous:
            complete_through = t
        previous = t
    return (rows[0]["t"] == start and result.get("count") == len(rows)
            and result.get("first_candle_at") == start
            and result.get("last_candle_at") == previous
            and result.get("missing_days") == gaps
            and result.get("complete_through") == complete_through)


def _read_disk_cache(symbol: str, start: int, today: int) -> dict | None:
    path = _cache_file(symbol)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
            return None
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(wrapper, dict):
            return None
        result = wrapper.get("result")
        if (wrapper.get("schema") != 1 or wrapper.get("sha256") != _digest(result)
                or not _valid_cached_result(result, symbol, start, today)):
            return None
        return result
    except (OSError, ValueError, TypeError, OverflowError, InvalidOperation):
        return None


def _write_disk_cache(symbol: str, result: dict) -> None:
    path = _cache_file(symbol)
    temporary = None
    try:
        directory = path.parent
        if directory.is_symlink():
            return
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir() or path.is_symlink():
            return
        wrapper = {"schema": 1, "result": result, "sha256": _digest(result)}
        encoded = json.dumps(wrapper, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_CACHE_BYTES:
            return
        descriptor, name = tempfile.mkstemp(prefix=".btc-daily-", dir=directory)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        # La caché pública nunca debe impedir consultar un archivo válido.
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def btc_daily_archive(http_get=None, now: int | None = None, quote_currency: str = "USD") -> dict:
    """Descarga ventanas ascendentes de 1.000 días cerrados; máximo 12.

    Un fallo en cualquier página invalida toda la respuesta: nunca se presenta
    un histórico parcial como si fuera completo. La caché es solo pública.
    """
    if quote_currency not in ("USD", "EUR"):
        raise ValueError("quote_currency must be USD or EUR")
    symbol = f"BTC/{quote_currency}"
    start = START if quote_currency == "USD" else START_EUR
    today = ((int(now) if now is not None else int(time.time())) // DAY) * DAY
    disk = None
    if http_get is None:
        with _lock:
            cached = _cached.get(symbol)
            if cached and time.monotonic() - cached[0] < 3600 and cached[1].get("as_of_day") == today:
                return deepcopy(cached[1])
        disk = _read_disk_cache(symbol, start, today)
        if disk is not None and disk["as_of_day"] == today:
            with _lock:
                _cached[symbol] = (time.monotonic(), deepcopy(disk))
            return deepcopy(disk | {"cache_status": "local-current"})

    def unavailable(reason: str) -> dict:
        if disk is not None:
            return deepcopy(disk | {
                "cache_status": "local-stale", "stale": True,
                "note": "Archivo local sin actualización reciente; comprueba la fecha de la última vela.",
            })
        return {"available": False, "reason": reason, "candles": []}
    adapter = BitstampAdapter(http_get=http_get)
    cursor = start
    by_time: dict[int, dict] = {}
    for _ in range(12):
        if cursor >= today:
            break
        rows = adapter.get_candles(symbol, "1d", limit=1000, since=cursor)
        if rows is None:
            return unavailable("Bitstamp no respondió con el histórico completo; inténtalo de nuevo.")
        # Bitstamp limita la ventana temporal a 1.000 días, no necesariamente
        # a 1.000 velas: días sin vela reducen el número de filas sin indicar
        # que se haya llegado al presente.
        valid = [row for row in rows if cursor <= row["t"] < min(today, cursor + 1000 * DAY)]
        for row in valid:
            by_time[row["t"]] = row
        cursor += 1000 * DAY
    else:
        return unavailable("El archivo supera el límite de paginación.")
    candles = [by_time[t] for t in sorted(by_time)]
    if not candles or candles[0]["t"] != start:
        year = 2012 if quote_currency == "USD" else 2016
        return unavailable(f"La fuente no devolvió la primera vela BTC/{quote_currency} de {year}; no se completan con datos inventados.")
    if candles[-1]["t"] < today - 7 * DAY:
        return unavailable("El archivo de la fuente termina demasiado pronto.")
    gaps = 0
    complete_through = candles[0]["t"]
    contiguous = True
    for previous, current in zip(candles, candles[1:]):
        missing = (current["t"] - previous["t"]) // DAY - 1
        gaps += missing
        if missing:
            contiguous = False
        if contiguous:
            complete_through = current["t"]
    result = {
        "available": True, "symbol": symbol, "resolved_provider": "bitstamp", "timeframe": "1d",
        "source": "Bitstamp OHLC público", "candles": candles, "count": len(candles),
        "first_candle_at": candles[0]["t"], "last_candle_at": candles[-1]["t"],
        "missing_days": gaps, "as_of_day": today, "complete_through": complete_through,
    }
    if http_get is None:
        with _lock:
            _cached[symbol] = (time.monotonic(), deepcopy(result))
        _write_disk_cache(symbol, result)
    return result
