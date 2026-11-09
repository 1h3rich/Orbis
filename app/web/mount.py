"""Monta el dashboard en /ui y los endpoints públicos de solo lectura.

``register_ui(app)`` es la única integración con ``app/main.py`` (+2 líneas).
``check_dir=False`` es obligatorio: la suite debe seguir verde aunque la
carpeta ``static`` falte. Sin CORS (mismo origen), sin jinja2, sin npm ni CDN.

Endpoints de mercado legacy (solo lectura, sin claves, solo valoración PAPER):
respetan ``ORBIS_PRICE_PROVIDER`` (``kraken`` por defecto para demo local):
solo ``kraken``
usa Kraken como primario (con recurso a CoinGecko y a manual); ``coingecko``
usa CoinGecko con recurso a manual; ``manual``/``static`` no tocan la red y
``/market/history`` solo sirve OHLC con ``kraken`` (el resto, nota honesta).

Endpoints TERMINAL (adapters propios REST urllib stdlib, sin CCXT — ver
``docs/CCXT_DECISION.md`` fase 8 —, sin keys, solo datos públicos):
``/market/providers``, ``/market/symbols``, ``/market/snapshot``,
``/market/candles``, ``/market/pair-alternatives``, WS ``/market/stream``
+ fallback SSE ``/market/stream-sse``, todo readonly. Más persistencia UI
(``/ui/*``) por mercado normalizado.
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import Scope

from app.core.config import get_orbis_config
from app.database.database import get_db
from app.pricing.providers import (
    get_configured_history,
    get_configured_price_with_meta,
)

_QUOTE_RE = re.compile(r"^[A-Z0-9]{2,12}$")
_ASSET_RE = re.compile(r"^[A-Z0-9]{1,12}$")


def _norm_quote(value: str) -> str:
    text = (value or "").strip().upper()
    if not _QUOTE_RE.match(text):
        raise HTTPException(
            status_code=400,
            detail="Invalid quote_currency; expected ^[A-Z0-9]{2,12}$",
        )
    return text


def _norm_asset(value: str) -> str:
    text = (value or "").strip().upper()
    if not _ASSET_RE.match(text):
        raise HTTPException(
            status_code=400,
            detail="Invalid asset; expected ^[A-Z0-9]{1,12}$",
        )
    return text


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles con Cache-Control: no-cache para evitar UI vieja en caché."""

    def file_response(
        self,
        full_path: os.PathLike,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response


def register_ui(app, static_dir: str | Path | None = None) -> None:
    """Monta /ui (StaticFiles html=True, check_dir=False) + /market/*."""
    directory = Path(static_dir) if static_dir is not None else Path(__file__).parent / "static"

    @app.get("/ui", include_in_schema=False)
    def ui_slash_redirect():
        """Redirige /ui -> /ui/ para que las rutas absolutas resuelvan igual."""
        return RedirectResponse(url="/ui/", status_code=307)

    app.mount(
        "/ui",
        _NoCacheStaticFiles(directory=str(directory), html=True, check_dir=False),
        name="ui",
    )

    @app.get("/market/price")
    def market_price(
        asset: str = Query(default="BTC"),
        quote_currency: str = Query(default="EUR"),
        db: Session = Depends(get_db),
    ):
        """Precio actual según el proveedor configurado (solo valoración)."""
        return get_configured_price_with_meta(
            db,
            _norm_asset(asset),
            _norm_quote(quote_currency),
            provider_name=get_orbis_config().price_provider,
        )

    @app.get("/market/history")
    def market_history(
        asset: str = Query(default="BTC"),
        quote_currency: str = Query(default="EUR"),
        timeframe: Literal["live", "1h", "24h", "7d"] = Query(default="24h"),
        db: Session = Depends(get_db),
    ):
        """OHLC con Kraken o nota honesta sin histórico inventado."""
        asset_n = _norm_asset(asset)
        quote_n = _norm_quote(quote_currency)
        return {
            "asset": asset_n,
            "quote_currency": quote_n,
            **get_configured_history(
                db,
                asset_n,
                quote_n,
                timeframe,
                provider_name=get_orbis_config().price_provider,
            ),
        }

    # -- TERMINAL: adapters propios (readonly, sin CCXT) --------------------
    @app.get("/market/providers")
    def market_providers():
        """Proveedores públicos + salud + default (env ORBIS_MARKET_PROVIDER)."""
        from app.marketdata import service as _svc
        from app.marketdata.registry import get_provider as _get_provider

        health = _svc.providers_health()
        provider_names = sorted(health["providers"].keys())
        timeframes = {}
        for name in provider_names:
            try:
                timeframes[name] = list(_get_provider(name).timeframes)
            except Exception:
                # Salud puede seguir respondiendo aunque un adaptador falle.
                # Omitirlo deja al servidor de velas como autoridad de rechazo.
                pass
        return {
            "default": get_orbis_config().market_provider,
            "providers": provider_names,
            "health": health["providers"],
            "timeframes": timeframes,
            "ws_note": "GRVT: datos públicos de perpetuos; streaming y PAPER derivados pendientes.",
        }

    @app.get("/market/symbols")
    def market_symbols(
        provider: str = Query(default="auto"),
        quote: str | None = Query(default=None),
        search: str | None = Query(default=None),
    ):
        """Solo símbolos disponibles (buscador del Estudio)."""
        from app.marketdata import service as _svc

        name = (provider or "auto").strip().lower()
        if name != "auto":
            from app.marketdata.registry import available_providers as _ap

            if name not in _ap():
                raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}")
        return {"provider": name, **_svc.symbols(provider=name, quote=quote, search=search)}

    @app.get("/market/snapshot")
    def market_snapshot(
        symbol: str = Query(default="BTC/EUR"),
        provider: str = Query(default="auto"),
    ):
        """Snapshot readonly: price como cadena + resolved_provider visible."""
        from app.marketdata import service as _svc

        return _svc.snapshot(symbol, provider=provider)

    @app.get("/market/candles")
    def market_candles(
        symbol: str = Query(default="BTC/EUR"),
        provider: str = Query(default="auto"),
        timeframe: str = Query(default="1h"),
        limit: int = Query(default=200, gt=0, le=1000),
        since: int | None = Query(default=None),
        before: int | None = Query(default=None, gt=0),
    ):
        """Velas dedup t asc con backfill; honesto si no hay nativo."""
        from app.marketdata import service as _svc

        # 1s/30s son opciones reales de la interfaz, pero ningún adaptador
        # actual las anuncia como OHLC nativo. El servicio devuelve
        # available=false de forma explícita; nunca agrega velas inventadas.
        allowed = ("1s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")
        if timeframe not in allowed:
            raise HTTPException(status_code=400, detail=f"Unknown timeframe {timeframe!r}")
        if before is not None and since is not None:
            raise HTTPException(status_code=400, detail="since and before are exclusive")
        return _svc.candles(symbol, provider=provider, timeframe=timeframe,
                            limit=limit, since=since, before=before)

    @app.get("/market/btc-daily-archive")
    def market_btc_daily_archive(quote_currency: Literal["USD", "EUR"] = Query(default="USD")):
        """Archivo público diario BTC/USD (2012) o BTC/EUR (2016)."""
        from app.marketdata.archive import btc_daily_archive

        return btc_daily_archive() if quote_currency == "USD" else btc_daily_archive(quote_currency="EUR")

    @app.get("/market/pair-alternatives")
    def market_alternatives(
        base: str = Query(default="BTC"),
        provider: str = Query(default="kraken"),
    ):
        """Alternativas con mensaje ``BTC/EUR no disponible en X``."""
        from app.marketdata import service as _svc
        from app.marketdata.normalize import alternatives as _alt
        from app.marketdata.registry import available_providers as _ap, get_provider as _gp

        name = (provider or "kraken").strip().lower()
        if name not in _ap():
            raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}")
        try:
            avail = _gp(name).get_symbols()
        except Exception:
            avail = []
        return _alt(base, name, avail)

    @app.websocket("/market/stream")
    async def market_stream(websocket: WebSocket):
        """WS JSON: snapshots cada 5s; el cliente reconecta 1-2-4-8 máx 30s."""
        import asyncio
        import json as _json

        from app.marketdata import service as _svc

        await websocket.accept()
        try:
            params = dict(websocket.query_params)
            symbol = params.get("symbol", "BTC/EUR")
            provider = params.get("provider", "auto")
            while True:
                try:
                    # Los adaptadores públicos usan HTTP síncrono: no bloquear
                    # el event loop que atiende la API local y otros sockets.
                    payload = await run_in_threadpool(_svc.snapshot, symbol, provider=provider)
                except Exception as exc:  # nunca tumbar el socket por un fallo
                    payload = {"symbol": symbol, "available": False,
                               "detail_tech": f"{type(exc).__name__}: {exc}"}
                try:
                    await websocket.send_text(_json.dumps(payload, default=str))
                except (WebSocketDisconnect, RuntimeError):
                    break
                if provider.strip().lower() == "grvt" or symbol.upper().endswith(":PERP"):
                    break  # Sin WS GRVT nativo: no sondear REST cada 5 segundos.
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
                except (TimeoutError, asyncio.TimeoutError):
                    continue
                except (WebSocketDisconnect, RuntimeError):
                    break
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    @app.get("/market/stream-sse")
    def market_stream_sse(
        symbol: str = Query(default="BTC/EUR"),
        provider: str = Query(default="auto"),
        rounds: int = Query(default=3, gt=0, le=12),
    ):
        """Fallback SSE/poll: N snapshots como eventos (sin WS)."""
        from fastapi.responses import StreamingResponse

        from app.marketdata import service as _svc

        def _gen():
            import json as _json
            import time as _time

            for _ in range(1 if provider.strip().lower() == "grvt" or symbol.upper().endswith(":PERP") else int(rounds)):
                try:
                    payload = _svc.snapshot(symbol, provider=provider)
                except Exception as exc:
                    payload = {"symbol": symbol, "available": False,
                               "detail_tech": f"{type(exc).__name__}: {exc}"}
                yield f"data: {_json.dumps(payload, default=str)}\n\n"
                _time.sleep(1)

        return StreamingResponse(_gen(), media_type="text/event-stream")

    # -- Persistencia UI (mercados normalizados, readonly PAPER) -------------
    def _utcnow():
        now = datetime.now(timezone.utc)
        return now.replace(tzinfo=None)

    @app.get("/ux/state")
    def ui_state_get(
        market: str = Query(default="BTC/EUR"),
        provider: str = Query(default="kraken"),
        timeframe: str = Query(default="1h"),
        kind: str = Query(default="drawings"),
        db: Session = Depends(get_db),
    ):
        """Lee dibujos/indicadores/config/planes de un mercado+provider."""
        from app.database.models import UiMarketState
        from app.marketdata.normalize import canonical_symbol, parse_symbol

        base, quote = parse_symbol(market)
        canon = canonical_symbol(base, quote)
        row = (
            db.query(UiMarketState)
            .filter(
                UiMarketState.market == canon,
                UiMarketState.provider == (provider or "kraken").lower(),
                UiMarketState.timeframe == timeframe,
                UiMarketState.kind == kind,
            )
            .first()
        )
        return {"market": canon, "provider": provider, "timeframe": timeframe,
                "kind": kind, "payload": (row.payload if row else {})}

    @app.put("/ux/state")
    def ui_state_put(body: dict, db: Session = Depends(get_db)):
        """Guarda estado UI por mercado normalizado (nunca ETH en BTC)."""
        from app.database.models import UiMarketState
        from app.marketdata.normalize import canonical_symbol, parse_symbol

        base, quote = parse_symbol(str(body.get("market", "BTC/EUR")))
        canon = canonical_symbol(base, quote)
        provider = str(body.get("provider", "kraken")).lower()
        timeframe = str(body.get("timeframe", "1h"))
        kind = str(body.get("kind", "drawings"))
        payload = body.get("payload") or {}
        row = (
            db.query(UiMarketState)
            .filter(
                UiMarketState.market == canon,
                UiMarketState.provider == provider,
                UiMarketState.timeframe == timeframe,
                UiMarketState.kind == kind,
            )
            .first()
        )
        if row is None:
            row = UiMarketState(market=canon, provider=provider, timeframe=timeframe,
                                kind=kind, payload=payload, updated_at=_utcnow())
            db.add(row)
        else:
            row.payload = payload
            row.updated_at = _utcnow()
        db.commit()
        db.refresh(row)
        return {"market": canon, "provider": provider, "timeframe": timeframe,
                "kind": kind, "payload": row.payload}

    @app.get("/ux/watchlist")
    def ui_watchlist_list(db: Session = Depends(get_db)):
        """Watchlist persistente (mercados con precio/24h/provider en Inicio)."""
        from app.database.models import UiWatchlist

        rows = db.query(UiWatchlist).order_by(UiWatchlist.position.asc()).all()
        return [{"market": r.market, "provider": r.provider, "position": r.position} for r in rows]

    @app.post("/ux/watchlist", status_code=201)
    def ui_watchlist_add(body: dict, db: Session = Depends(get_db)):
        """Añade un mercado a la watchlist (canónico)."""
        from app.database.models import UiWatchlist
        from app.marketdata.normalize import canonical_symbol, parse_symbol

        base, quote = parse_symbol(str(body.get("market", "")))
        if not base or not quote:
            raise HTTPException(status_code=400, detail="Invalid market; expected BASE/QUOTE")
        canon = canonical_symbol(base, quote)
        provider = str(body.get("provider", "kraken")).lower()
        position = int(body.get("position", 0))
        row = UiWatchlist(market=canon, provider=provider, position=position,
                          created_at=_utcnow())
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"market": row.market, "provider": row.provider, "position": row.position, "id": row.id}

    @app.delete("/ux/watchlist")
    def ui_watchlist_del(
        market: str = Query(default=""),
        provider: str = Query(default=""),
        db: Session = Depends(get_db),
    ):
        """Quita un mercado de la watchlist."""
        from app.database.models import UiWatchlist
        from app.marketdata.normalize import canonical_symbol, parse_symbol

        base, quote = parse_symbol(market)
        canon = canonical_symbol(base, quote)
        q = db.query(UiWatchlist).filter(UiWatchlist.market == canon)
        if provider:
            q = q.filter(UiWatchlist.provider == provider.lower())
        deleted = q.delete()
        db.commit()
        return {"deleted": deleted, "market": canon}

    @app.get("/ux/preferences")
    def ui_preferences_get(key: str | None = Query(default=None), db: Session = Depends(get_db)):
        """Lee preferencias UI (modo, provider default, config Estudio...)."""
        from app.database.models import UiPreferences

        if key:
            row = db.query(UiPreferences).filter(UiPreferences.key == key).first()
            return {"key": key, "value": (row.value if row else "")}
        rows = db.query(UiPreferences).all()
        return {r.key: r.value for r in rows}

    @app.put("/ux/preferences")
    def ui_preferences_put(body: dict, db: Session = Depends(get_db)):
        """Guarda una preferencia UI por clave."""
        from app.database.models import UiPreferences

        key = str(body.get("key", "")).strip()
        if not key:
            raise HTTPException(status_code=400, detail="Invalid key")
        value = str(body.get("value", ""))
        row = db.query(UiPreferences).filter(UiPreferences.key == key).first()
        if row is None:
            row = UiPreferences(key=key, value=value, updated_at=_utcnow())
            db.add(row)
        else:
            row.value = value
            row.updated_at = _utcnow()
        db.commit()
        return {"key": key, "value": value}
