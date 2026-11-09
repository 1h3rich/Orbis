"""Service readonly: snapshot/candles/symbols con caché + dedupe + backfill.

* price siempre como **cadena** (importes como cadenas, Decimal financiero).
* candles: dedupe por ``t`` ascendente, backfill paginado sin duplicados.
* Cada adaptador usa OHLC nativo o responde ``available:false`` con motivo
  matemáticamente honesto (nunca inventar). GRVT exige ``:PERP`` explícito.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time

from app.marketdata import cache as _cache
from app.marketdata.normalize import alternatives, canonical_symbol, parse_symbol
from app.marketdata.registry import available_providers, get_provider, resolve_provider


MARKET_TICKER_STALE_SECONDS = 60


def _snapshot_time(out: dict) -> dict:
    """Distingue hora publicada por el mercado de hora de recepción local."""
    result = dict(out)
    result["market_time_at"] = None
    result["market_age_seconds"] = None
    result["market_time_status"] = "unknown"
    stamp = result.get("event_time_ns")
    if stamp is None:
        return result
    try:
        event_seconds = int(stamp) / 1_000_000_000
        if event_seconds <= 0:
            raise ValueError("invalid event time")
        result["market_time_at"] = datetime.fromtimestamp(event_seconds, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        result["market_time_status"] = "invalid"
        return result
    age = time.time() - event_seconds
    if age < -120:
        result["market_time_status"] = "future"
    elif age > MARKET_TICKER_STALE_SECONDS:
        result["market_time_status"] = "stale"
        result["market_age_seconds"] = int(age)
    else:
        result["market_time_status"] = "fresh"
        result["market_age_seconds"] = max(0, int(age))
    return result


def _candle_end(t: int, timeframe: str) -> int | None:
    """Fin UTC de una vela; el mes usa calendario, no 30 días aproximados."""
    if timeframe == "1M":
        try:
            start = datetime.fromtimestamp(t, timezone.utc)
            year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
            return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
        except (OverflowError, ValueError, OSError):
            return None
    seconds = _cache.TF_SECONDS.get(timeframe)
    return t + seconds if seconds is not None else None


def snapshot(symbol: str, provider: str | None = None, http_get=None) -> dict:
    """Snapshot readonly de un par: precio como cadena + 24h + provider resuelto."""
    base, quote = parse_symbol(symbol or "")
    canon = canonical_symbol(base, quote)
    prov, resolved = resolve_provider(provider, symbol=canon, http_get=http_get)
    if prov is None:
        return {
            "symbol": canon,
            "requested_provider": (provider or "auto"),
            "resolved_provider": resolved,
            "available": False,
            "price": None,
            "reason": f"{canon} no disponible en providers AUTO",
            "actions": ["Reintentar", "Cambiar proveedor"],
        }
    key = (resolved, canon)
    cached = None if http_get is not None else _cache.get("ticker", key)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return _snapshot_time(out)
    try:
        ticker = prov.get_ticker(canon)
    except Exception as exc:  # nunca lanzar al exterior
        ticker = None
        tech = f"{type(exc).__name__}: {exc}"
    else:
        tech = None
    if ticker and (not isinstance(ticker, dict) or ticker.get("symbol") != canon):
        # Un adapter no puede rebautizar el precio de otro par como el
        # solicitado. Tampoco se archiva esa respuesta en la caché del par.
        return {
            "symbol": canon,
            "requested_provider": (provider or "auto"),
            "resolved_provider": resolved,
            "available": False,
            "price": None,
            "reason": "El proveedor devolvió un ticker de otro mercado o sin identidad verificable",
            "actions": ["Reintentar"],
        }
    if not ticker or ticker.get("price") is None:
        avail = []
        try:
            avail = prov.get_symbols()
        except Exception:
            avail = []
        alt = alternatives(base, resolved, avail, quote)
        out = {
            "symbol": canon,
            "requested_provider": (provider or "auto"),
            "resolved_provider": resolved,
            "available": False,
            "price": None,
            "bid": None,
            "ask": None,
            "reason": alt["message"],
            "alternatives": alt["available"],
            "actions": ["Reintentar", "Cambiar proveedor"],
        }
        if tech:
            out["detail_tech"] = tech
        if resolved == "grvt":
            out["note"] = "GRVT: sólo perpetuos BASE/QUOTE:PERP; streaming PAPER pendiente."
        return out
    out = {
        "symbol": canon,
        "requested_provider": (provider or "auto"),
        "resolved_provider": resolved,
        "available": True,
        "price": str(ticker.get("price")),
        "bid": str(ticker.get("bid")) if ticker.get("bid") is not None else None,
        "ask": str(ticker.get("ask")) if ticker.get("ask") is not None else None,
        "high": str(ticker.get("high")) if ticker.get("high") is not None else None,
        "low": str(ticker.get("low")) if ticker.get("low") is not None else None,
        "change_pct": (
            str(ticker.get("change_pct"))
            if ticker.get("change_pct") is not None
            else None
        ),
        "base_volume": (
            str(ticker.get("base_volume"))
            if ticker.get("base_volume") is not None
            else None
        ),
        "quote_volume": (
            str(ticker.get("quote_volume"))
            if ticker.get("quote_volume") is not None
            else None
        ),
        "source": ticker.get("source") or resolved,
        "cached": False,
        "received_at": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(),
    }
    for field in ("instrument", "instrument_type", "mark_price", "event_time_ns"):
        if ticker.get(field) is not None:
            out[field] = str(ticker[field])
    if ticker.get("note"):
        out["note"] = ticker["note"]
    if http_get is None:
        _cache.put("ticker", key, dict(out))
    return _snapshot_time(out)


def candles(
    symbol: str,
    provider: str | None = None,
    timeframe: str = "1h",
    limit: int = 200,
    since: int | None = None,
    http_get=None,
    before: int | None = None,
) -> dict:
    """Velas dedup t asc; ``before`` solicita sólo historia anterior."""
    if before is not None and (since is not None or type(before) is not int or before <= 0):
        raise ValueError("before must be a positive exclusive bound without since")
    base, quote = parse_symbol(symbol or "")
    canon = canonical_symbol(base, quote)
    limit = max(1, min(int(limit or 200), 1000))
    if (provider or "auto").strip().lower() == "auto":
        prov, resolved = resolve_provider(provider, symbol=canon, http_get=http_get,
                                          timeframe=timeframe)
    else:
        prov, resolved = resolve_provider(provider, symbol=canon, http_get=http_get)
    if prov is None:
        return {
            "symbol": canon,
            "resolved_provider": resolved,
            "timeframe": timeframe,
            "available": False,
            "candles": [],
            "reason": f"{canon}/{timeframe} no disponible en proveedores AUTO con esa temporalidad nativa",
            "actions": ["Reintentar", "Cambiar proveedor"],
        }
    supported = getattr(prov, "timeframes", ()) or ()
    if timeframe not in supported:
        return {
            "symbol": canon,
            "resolved_provider": resolved,
            "timeframe": timeframe,
            "available": False,
            "candles": [],
            "reason": (
                "No disponible para este proveedor: "
                f"timeframe {timeframe} no nativo en {resolved} "
                f"(nativos: {', '.join(supported) if supported else 'ninguno'}). "
                "No se inventan datos."
            ),
            "actions": ["Reintentar", "Cambiar proveedor"],
        }
    if before is not None and not callable(getattr(prov, "get_candles_before", None)):
        return {
            "symbol": canon, "resolved_provider": resolved, "timeframe": timeframe,
            "available": False, "candles": [],
            "reason": f"{resolved} no ofrece paginación hacia velas anteriores; cambia de fuente para ver más historial.",
            "actions": ["Cambiar proveedor"],
        }
    key = (resolved, canon, timeframe, limit, since, before)
    if http_get is None:
        cached = _cache.get("candles", key)
        if cached is not None:
            return dict(cached)
    try:
        raw = (prov.get_candles_before(canon, timeframe=timeframe, limit=limit, before=before)
               if before is not None else
               prov.get_candles(canon, timeframe=timeframe, limit=limit, since=since))
    except Exception as exc:
        return {
            "symbol": canon,
            "resolved_provider": resolved,
            "timeframe": timeframe,
            "available": False,
            "candles": [],
            "reason": f"{resolved} no devolvió velas para {canon}/{timeframe}.",
            "detail_tech": f"{type(exc).__name__}: {exc}",
            "actions": ["Reintentar", "Cambiar proveedor"],
        }
    if before is not None and isinstance(raw, list) and not raw:
        return {"symbol": canon, "resolved_provider": resolved,
                "timeframe": timeframe, "available": True, "candles": [],
                "count": 0, "history_exhausted": True}
    if not raw:
        return {
            "symbol": canon,
            "resolved_provider": resolved,
            "timeframe": timeframe,
            "available": False,
            "candles": [],
            "reason": (
                f"{resolved} no sirve OHLC para {canon}/{timeframe}. "
                "No se inventan datos."
            ),
            "actions": ["Reintentar", "Cambiar proveedor"],
        }
    # Dedupe por t asc. Dos versiones distintas de la misma vela no se
    # resuelven por orden de llegada: la página completa queda no disponible.
    seen: dict[int, dict] = {}
    duplicate_count = 0
    now = int(time.time())
    for c in raw:
        try:
            candle_t = int(c["t"])
            if candle_t < 0 or str(c["t"]).strip() != str(candle_t):
                raise ValueError("invalid candle timestamp")
            end_t = _candle_end(candle_t, timeframe)
            closed_by_time = end_t is not None and end_t <= now
            if before is not None and candle_t >= before:
                continue
            prices = {key: Decimal(str(c[key])) for key in ("o", "h", "l", "c")}
            volume = Decimal(str(c.get("v", "0")))
            if (any(not value.is_finite() or value <= 0 for value in prices.values())
                    or not volume.is_finite() or volume < 0
                    or prices["h"] < max(prices["o"], prices["l"], prices["c"])
                    or prices["l"] > min(prices["o"], prices["h"], prices["c"])):
                raise ValueError("invalid candle OHLCV")
            normalized = {
                "t": candle_t,
                "o": str(c["o"]),
                "h": str(c["h"]),
                "l": str(c["l"]),
                "c": str(c["c"]),
                "v": str(c.get("v", "0")),
                "closed": closed_by_time and c.get("closed") is not False,
            }
            if "observed_until" in c:
                normalized["observed_until"] = int(c["observed_until"])
            previous = seen.get(candle_t)
            if previous is not None:
                duplicate_count += 1
                if any(Decimal(previous[key]) != Decimal(normalized[key])
                       for key in ("o", "h", "l", "c", "v")):
                    return {"symbol": canon, "resolved_provider": resolved,
                            "timeframe": timeframe, "available": False,
                            "candles": [], "reason": "La fuente devolvió versiones incompatibles de una vela; no se usa esta página."}
                previous["closed"] = previous["closed"] and normalized["closed"]
                continue
            seen[candle_t] = normalized
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            return {"symbol": canon, "resolved_provider": resolved,
                    "timeframe": timeframe, "available": False,
                    "candles": [], "reason": "La fuente devolvió una vela inválida; no se usa esta página.",
                    "actions": ["Reintentar", "Cambiar proveedor"]}
    ordered = [seen[t] for t in sorted(seen)]
    if before is not None and not ordered:
        return {"symbol": canon, "resolved_provider": resolved,
                "timeframe": timeframe, "available": False, "candles": [],
                "reason": f"{resolved} no devolvió velas anteriores al límite solicitado.",
                "actions": ["Cambiar proveedor"]}
    page = ordered[-limit:]
    gaps = []
    for earlier, later in zip(page, page[1:]):
        expected = _candle_end(earlier["t"], timeframe)
        if expected is not None and later["t"] > expected:
            gaps.append({"from": expected, "to": later["t"]})
    last_end = _candle_end(page[-1]["t"], timeframe) if page else None
    boundary_gap_seconds = max(0, before - last_end) if before is not None and last_end is not None else 0
    out = {
        "symbol": canon,
        "resolved_provider": resolved,
        "timeframe": timeframe,
        "available": True,
        "candles": page,
        "count": len(page),
        "older_pagination_supported": callable(getattr(prov, "get_candles_before", None)),
        "duplicate_count": duplicate_count,
        "gap_count": len(gaps),
        "first_gap": gaps[0] if gaps else None,
        "boundary_gap_seconds": boundary_gap_seconds,
    }
    if http_get is None:
        _cache.put("candles", key, dict(out))
    return out


def symbols(provider: str | None = None, quote: str | None = None, search: str | None = None) -> dict:
    """Solo símbolos disponibles (para el buscador del Estudio)."""
    names = available_providers() if (provider or "auto").lower() == "auto" else [provider.lower()]
    result: dict[str, list[str]] = {}
    for name in names:
        try:
            prov = get_provider(name)
        except Exception:
            continue
        key = (name,)
        listing = _cache.get("symbols", key)
        if listing is None:
            try:
                listing = prov.get_symbols()
            except Exception:
                listing = []
            _cache.put("symbols", key, listing)
        items = list(listing or [])
        if quote:
            q = quote.strip().upper()
            items = [s for s in items if s.split("/")[-1] == q]
        if search:
            needle = search.strip().upper()
            items = [s for s in items if needle in s.replace("/", "")]
        result[name] = sorted(items)
    return {"providers": result}


def providers_health() -> dict:
    """Salud AVAILABLE/DEGRADED/UNAVAILABLE por provider (humano + técnico)."""
    out = {}
    for name in available_providers():
        key = (name,)
        cached = _cache.get("health", key)
        if cached is not None:
            out[name] = cached
            continue
        try:
            info = get_provider(name).health()
        except Exception as exc:
            info = {
                "provider": name,
                "status": "UNAVAILABLE",
                "detail_human": f"{name}: no disponible",
                "detail_tech": f"{type(exc).__name__}: {exc}",
            }
        _cache.put("health", key, info)
        out[name] = info
    return {"providers": out}
