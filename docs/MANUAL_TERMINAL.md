# Manual de verificación — TERMINAL PERSONAL Orbis (42 pasos)

Entorno: `./.venv/bin/python`, `uvicorn app.main:app --host 127.0.0.1 --port 8000`,
navegador en `http://127.0.0.1:8000/ui/`. Sin DevTools/HTTP/backend: user journey
solo con la UI (salvo pasos marcados [técnico]).

Leyenda de resultado (esta ejecución): ✅ verificado (suite/humo/técnico),
▶️ checklist ejecutable a mano (sin automatización de navegador disponible).

## F2 — Entornos separados (1-6)

1. ▶️ El nav muestra «Patrimonio» (registro REAL) y «Simulación» (PAPER); dentro de Simulación están Operar, Saldo, Spot PAPER, GRVT PAPER, DCA, Planes y Movimientos.
2. ▶️ No hay selector global PAPER/LIVE: el entorno depende de la sección.
3. ▶️ «Patrimonio» abre `#/real` y no el saldo ficticio.
4. ▶️ `#/live` (URL heredada) vuelve a `#/inicio`; no simula trading real.
5. ▶️ El saldo PAPER se abre desde «Simulación» → «Saldo» y se identifica como ficticio.
6. ✅ [técnico] `test_web_ui.py` comprueba que no se anuncie una ruta LIVE.

## F1 — Mercado multi-exchange (7-14)

7. ✅ Selector de exchange con AUTO + 10 + default persistente (`orbis.provider.default`
   + `ORBIS_MARKET_PROVIDER`): humo `/market/providers` → 10 + default kraken.
8. ✅ AUTO resuelve sin mezclar silencioso (`resolved_provider` visible en snapshot y Estudio).
9. ✅ Si falla: "Reintentar / Cambiar proveedor" (`snapshot?symbol=XXX/EUR&provider=binance`
   → `available:false` + motivo + actions).
10. ✅ Salud AVAILABLE/DEGRADED/UNAVAILABLE humana + detalle técnico en `/market/providers`.
11. ✅ `price` como cadena en `/market/snapshot`; velas dedupe t asc en `/market/candles`.
12. ✅ `BTC/EUR no disponible en X` + alternativas en `/market/pair-alternatives`.
13. ✅ WS `/market/stream` + fallback SSE/poll (tests TestClient verdes).
14. ✅ GRVT como `BTC/USDT:PERP`, ticker/velas REST puntuales; sin streaming
    automático ni formulario spot PAPER para perpetuos. LONG/SHORT pendiente.

## F4 — Operativa (15-18)

15. ▶️ Gráfico LWC embebido sobre el panel (Lightweight Charts v5.0.9 real vendorizado; mensaje honesto si falta).
16. ▶️ Panel Comprar/Vender: saldo, importe, precio mercado, recibirás, comisión, TOTAL.
17. ✅ Compra PAPER manda `exchange=<provider>` (código + humo buy con exchange kraken).
18. ✅ Ledger guarda la provenance (`o.exchange` visible como Proveedor).

## F7 — Ledger (19-21)

19. ▶️ Columna Proveedor secundaria ("—" si null), resumen summary intacto, filtros compactos.
20. ▶️ DCA con provider manual visible.
21. ✅ `GET /paper/operations/summary` + filtros sin cambios (suite verde).

## F5 — Portfolio (22-27)

22. ✅ 100€@50k → 0.002/100/50k (`test_marketdata.py::test_financial_validation_*`).
23. ✅ +100€@100k → 0.003/200/66666.67; venta 0.001@70k → realized 3.333, invested
    133.333, qty 0.002, avg intacto (mismo test, Decimal exacto).
24. ✅ Invariantes equity-initial==realized+unrealized, cash==initial-compras+ventas,
    equity==cash+positions, pesos 100% (suite `test_ux_backend` + portfolio).
25. ▶️ Valor actual destacado + DCA (aportado/acumulado/medio/actual/resultado/nº) con
    "Valoración usando Kraken"; distribución secundaria; evolución o mensaje honesto.

## F6 — Inicio (28-33)

28. ▶️ Resumen cartera/PnL/rent + watchlist (precio/24h/provider, abrir en Estudio) +
    estrategias + últimas ops + planes, con links Ver, sin sobrecargar.
29. ✅ Watchlist persiste (`/ux/watchlist` roundtrip verde).
30. ▶️ Abrir un mercado de la watchlist lo carga en el Estudio.
31-33. ▶️ Enlaces Ver estrategias / Ver todo / planes visibles y funcionales.

## F3 — Estudio (34-42)

34. ▶️ Toolbar par/provider/1m-1w/tipo/indicadores/comparar/guardar/fullscreen (28 pasos
    detallados en `MANUAL_ESTUDIO.md`).
35. ▶️ Buscador solo disponibles; TF no nativo → motivo honesto.
36. ▶️ Zoom rueda/táctil, pan, backfill sin duplicados con loading discreto, autoscale,
    crosshair OHLC+precio+fecha, último precio, responsive, reset.
37. ▶️ Volumen en pane propio; SMA/EMA/Bollinger/RSI/MACD con parámetros y persistencia.
38. ▶️ Dibujos (H, tendencia 2pts, vertical, zona, texto, medidor, long/short R2.0,
    fib 0-1 + extensión 3pts, niveles custom): seleccionar/mover/extremos/eliminar/
    bloquear/ocultar/persistir/undo-redo (29-42 en `MANUAL_FIB.md`).
39. ▶️ Watchlist plegable, bid/ask, comparar % normalizada, alertas >/ y desde línea,
    menú contextual, markers PAPER/DCA/planes con tooltip, precisión BTC 2dec/bajo 8dec.
40. ▶️ Fullscreen Esc; atajos Esc/Supr/Ctrl+Z/Ctrl+Mayús+Z; herramienta activa clara;
    magnet OHLC; persistencia por mercado normalizado (nunca ETH en BTC).
41. ✅ Vendor `lightweight-charts.standalone.production.js` v5.0.9 servido local
    (humo 200, sin CDN/npm); sin vendor → mensaje honesto.
42. ✅ Tabla comparativa TradingView en `MANUAL_TRADINGVIEW.md` (Sí en básicas).
