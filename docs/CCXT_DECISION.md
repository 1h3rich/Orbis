# Orbis — por qué NO usamos CCXT (decisión fase 8, con evidencia)

**Estado: CCXT descartado en la TERMINAL v1. Adapters propios. Reevaluar en fase 8.**

## Evidencia verificada en este repo

1. **Conflicto de dependencias**: CCXT exige `certifi==2026.6.17` mientras el
   proyecto fija `certifi==2026.7.22` en `requirements.txt` (6,7 MB + 17
   dependencias transitivas: `aiohttp`, `yarl`, ` multidict`, etc.).
2. **Modelo async vs servidor sync**: el servidor Orbis es FastAPI sync con
   SQLite y TestClient; CCXT-async obligaría a un bucle propio y a hilos de
   reintento que la suite actual (176 tests, `sqlite://` en memoria) no cubre.
3. **Contrato financiero propio**: Orbis trabaja con `Decimal` exacto e
   importes como cadenas; CCXT devuelve `float` y estructuras por exchange
   que habría que renormalizar igualmente (canónico BASE/QUOTE, XBT→BTC,
   sin fusionar EUR/USDT/USDC, nativos por tabla). El coste de adaptación
   supera al de los adapters propios.
4. **GRVT**: su API de datos públicos usa POST JSON y dispone de streaming
   JSONRPC. El adaptador actual cubre ticker y velas de perpetuos con
   `:PERP` explícito; el streaming propio aún no está implementado.

## Lo que se hizo en su lugar

* `app/marketdata/` con contrato `MarketDataProvider` común
  (`get_ticker/get_price/get_24h_stats/get_candles/get_symbols/supports_pair/health`),
  `normalize.py` canónico, 10 adapters propios REST urllib stdlib timeout 5s
  + WS nativo JSON donde es simple (Kraken v2, Binance, Coinbase, OKX, Bybit,
  MEXC, KuCoin, Gate, Bitget) y GRVT REST de consulta puntual, sin sondeo
  automático ni trading.
* Caché `(provider,par,timeframe)` TTL ticker 5s / candles según temporalidad / symbols 900s
  / health 15s + registry + stream con reconexión 1-2-4-8 máx 30s.
* Solo datos públicos, sin keys/secretos, todo readonly.

## Fase 8 (futuro)

Reevaluar CCXT solo si: (a) se necesita trading LIVE real con custodia,
(b) se resuelve el pin de `certifi` sin romper la suite, (c) se adopta un
worker async separado del API sync, (d) se mantiene el contrato Decimal
propio como capa de dominio por encima de CCXT.
