"""Datos públicos de perpetuos GRVT. No ejecuta órdenes ni simula margen.

La identidad canónica incluye ``:PERP`` para impedir que una vela o precio de
derivados se use silenciosamente como si fuera BTC/USDT spot.
"""

from __future__ import annotations

import time

from app.marketdata import http as _http
from app.marketdata._base import RestAdapter
from app.marketdata.grvt_candle_time import (GrvtCandleNotClosed, GrvtHistoryError,
                                            read_candle_close)
from app.marketdata.normalize import parse_symbol


_ROOT = "https://market-data.grvt.io/full/v1"
_INTERVALS = {
    "1m": ("CI_1_M", 60),
    "5m": ("CI_5_M", 300),
    "15m": ("CI_15_M", 900),
    "30m": ("CI_30_M", 1800),
    "1h": ("CI_1_H", 3600),
    "4h": ("CI_4_H", 14400),
    "1d": ("CI_1_D", 86400),
    "1w": ("CI_1_W", 604800),
}


class GrvtAdapter(RestAdapter):
    name = "grvt"
    # Este adaptador expone consultas REST puntuales. El observador de riesgo
    # AUTO PAPER usa aparte GrvtTickerStream, sin credenciales ni órdenes.
    ws_kind = "none"
    timeframes = tuple(_INTERVALS)
    known_pairs = ("BTC/USDT:PERP", "ETH/USDT:PERP", "SOL/USDT:PERP")
    DATA_NOTE = ("Perpetuo GRVT; esta cotización es REST puntual. "
                 "El riesgo AUTO PAPER usa un canal público independiente.")

    def __init__(self, http_get=None, http_post=None):
        super().__init__(http_get=http_get)
        self._http_post = http_post or _http.http_post_json

    def _instrument(self, symbol: str) -> str | None:
        base, quote = parse_symbol(symbol)
        canon = f"{base}/{quote}"
        if canon not in self.known_pairs:
            return None
        return f"{base}_{quote.removesuffix(':PERP')}_Perp"

    def get_ticker(self, symbol: str) -> dict | None:
        instrument = self._instrument(symbol)
        if instrument is None:
            return None
        try:
            payload = self._http_post(f"{_ROOT}/ticker", {"instrument": instrument})
            row = payload["result"]
            if row.get("instrument") != instrument or not row.get("last_price"):
                raise ValueError("Ticker GRVT sin precio o instrumento esperado")
            raw_time = row.get("event_time")
            if (not isinstance(raw_time, str) or not 1 <= len(raw_time) <= 20
                    or not raw_time.isascii() or not raw_time.isdecimal()):
                raise ValueError("Ticker GRVT sin hora textual exacta")
            event_time_ns = int(raw_time)
            if event_time_ns <= 0:
                raise ValueError("Ticker GRVT sin hora positiva")
            self._mark_ok()
            return {
                "symbol": symbol.upper(),
                "instrument": instrument,
                "instrument_type": "PERPETUAL",
                "event_time_ns": event_time_ns,
                "price": str(row["last_price"]),
                "mark_price": str(row["mark_price"]) if row.get("mark_price") is not None else None,
                "bid": str(row["best_bid_price"]) if row.get("best_bid_price") is not None else None,
                "ask": str(row["best_ask_price"]) if row.get("best_ask_price") is not None else None,
                "high": str(row["high_price"]) if row.get("high_price") is not None else None,
                "low": str(row["low_price"]) if row.get("low_price") is not None else None,
                "source": self.name,
                "note": self.DATA_NOTE,
            }
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        instrument = self._instrument(symbol)
        if instrument is None or timeframe not in _INTERVALS:
            return None
        interval, seconds = _INTERVALS[timeframe]
        count = max(1, min(int(limit), 1000))
        request: dict[str, str | int] = {
            "instrument": instrument,
            "interval": interval,
            "type": "TRADE",
            "limit": count,
        }
        if since is not None:
            start_ns = int(since) * 1_000_000_000
            end_ns = min(start_ns + count * seconds * 1_000_000_000 - 1, time.time_ns())
            request["start_time"] = str(start_ns)
            request["end_time"] = str(end_ns)
        try:
            payload = self._http_post(f"{_ROOT}/kline", request)
            rows = payload["result"]
            if not isinstance(rows, list):
                raise ValueError("Velas GRVT malformadas")
            out = []
            for row in rows:
                if row.get("instrument") != instrument:
                    raise ValueError("Vela GRVT de otro instrumento")
                timestamp = int(row["open_time"]) // 1_000_000_000
                if since is not None and timestamp < int(since):
                    continue
                close_ns = int(row["close_time"])
                out.append({
                    "t": timestamp,
                    "o": str(row["open"]),
                    "h": str(row["high"]),
                    "l": str(row["low"]),
                    "c": str(row["close"]),
                    "v": str(row["volume_b"]),
                    "closed": close_ns >= int(row["open_time"]) + seconds * 1_000_000_000,
                    "observed_until": close_ns // 1_000_000_000,
                })
            self._mark_ok()
            return sorted(out, key=lambda row: row["t"])[-count:]
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_historical_candles(self, symbol: str, timeframe: str, start: int,
                               end: int, *, now: int | None = None) -> list[dict]:
        """Descarga [start,end) con el cursor inverso oficial, sin huecos.

        Sólo admite velas TRADE de perpetuos con cierre confirmado. Ningún resultado
        parcial llega a LAB si falla una página, cambia una vela o falta un
        intervalo; así la huella del dataset representa cobertura real.
        """
        instrument = self._instrument(symbol)
        if instrument is None or timeframe not in _INTERVALS:
            raise GrvtHistoryError("Instrumento o temporalidad GRVT no admitidos")
        interval, seconds = _INTERVALS[timeframe]
        count = (end - start) // seconds if type(start) is int and type(end) is int else 0
        observed_now = int(time.time()) if now is None else now
        if (type(start) is not int or type(end) is not int or type(observed_now) is not int
                or not 3 <= count <= 5000 or start <= 0 or start % seconds or end % seconds or end <= start
                or end > observed_now or end - start != count * seconds):
            raise GrvtHistoryError("Rango GRVT inválido o con vela aún abierta")
        expected = {start + index * seconds for index in range(count)}
        rows: dict[int, dict] = {}
        cursor = ""
        seen_cursors = set()
        max_pages = 50
        try:
            for _ in range(max_pages):
                body = {"instrument": instrument, "interval": interval, "type": "TRADE",
                        "start_time": str(start * 1_000_000_000),
                        "end_time": str(end * 1_000_000_000 - 1),
                        "limit": min(count, 1000), "cursor": cursor}
                response = self._http_post(f"{_ROOT}/kline", body)
                page = response["result"]
                if not isinstance(page, list) or not page or len(page) > body["limit"]:
                    raise GrvtHistoryError("Página GRVT vacía o malformada")
                for raw in page:
                    if not isinstance(raw, dict) or raw.get("instrument") != instrument:
                        raise GrvtHistoryError("Vela GRVT de otro instrumento")
                    if any(not isinstance(raw.get(field), str) for field in
                           ("open_time", "close_time", "open", "high", "low", "close", "volume_b")):
                        raise GrvtHistoryError("Vela GRVT sin cifras textuales verificables")
                    timestamp, confirmed = read_candle_close(raw["open_time"], raw["close_time"],
                                                             start=start, end=end, step=seconds)
                    if not confirmed:
                        continue
                    candle = {"t": timestamp, "o": str(raw["open"]), "h": str(raw["high"]),
                              "l": str(raw["low"]), "c": str(raw["close"]), "v": str(raw["volume_b"])}
                    if timestamp in rows and rows[timestamp] != candle:
                        raise GrvtHistoryError("Vela GRVT cambió entre páginas")
                    rows[timestamp] = candle
                if len(rows) == count:
                    self._mark_ok()
                    return [rows[timestamp] for timestamp in sorted(expected)]
                next_cursor = response.get("next")
                if next_cursor == "" and rows.keys() == expected - {end - seconds}:
                    raise GrvtCandleNotClosed(end * 1_000_000_000)
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                    raise GrvtHistoryError("Histórico GRVT incompleto o cursor repetido")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            raise GrvtHistoryError("Demasiadas páginas GRVT para el rango solicitado")
        except GrvtHistoryError as exc:
            self._mark_err(exc)
            raise
        except Exception as exc:
            self._mark_err(exc)
            raise GrvtHistoryError("GRVT no devolvió un histórico verificable") from exc

    def get_mark_history(self, symbol: str, timeframe: str, start: int,
                         end: int, *, now: int | None = None) -> list[dict]:
        """MARK público sin volumen sintético; nunca usa la caché TRADE."""
        from app.marketdata.grvt_mark_history import closed_mark_history
        from app.lab.engine import LabError
        instrument = self._instrument(symbol)
        if instrument is None:
            raise GrvtHistoryError("MARK requiere un perpetuo explícito")
        try:
            rows = closed_mark_history(self._http_post, instrument=instrument,
                timeframe=timeframe, start=start, end=end,
                now=int(time.time()) if now is None else now)
            self._mark_ok()
            return rows
        except Exception as exc:
            self._mark_err(exc)
            if isinstance(exc, GrvtHistoryError):
                raise
            message = str(exc) if isinstance(exc, LabError) else "GRVT no devolvió MARK verificable"
            raise GrvtHistoryError(message) from exc

    def get_funding_history(self, symbol: str, opened_at_ns: int, end_ns: int) -> list[dict]:
        """Liquidaciones públicas alrededor de [apertura, fin], con ancla previa.

        El endpoint es inversamente paginado. No entregamos una página parcial
        al cálculo de PAPER ni interpretamos aquí la tasa como rendimiento.
        """
        instrument = self._instrument(symbol)
        hour_ns = 3_600_000_000_000
        if (instrument is None or type(opened_at_ns) is not int or type(end_ns) is not int
                or opened_at_ns <= 8 * hour_ns or end_ns <= opened_at_ns
                or end_ns - opened_at_ns > 366 * 24 * hour_ns):
            raise GrvtHistoryError("Periodo de financiación GRVT inválido")
        lower = opened_at_ns - 8 * hour_ns
        cursor, seen_cursors = "", set()
        rows: list[dict] = []
        last_ns: int | None = None
        try:
            for _ in range(20):
                payload = self._http_post(f"{_ROOT}/funding", {
                    "instrument": instrument, "start_time": str(lower), "end_time": str(end_ns),
                    "limit": 1000, "cursor": cursor, "agg_type": "FUNDING_INTERVAL",
                })
                page = payload["result"]
                if not isinstance(page, list) or not page or len(page) > 1000:
                    raise GrvtHistoryError("Página de financiación GRVT vacía o malformada")
                for raw in page:
                    if not isinstance(raw, dict) or raw.get("instrument") != instrument:
                        raise GrvtHistoryError("Liquidación de otro instrumento")
                    if (not isinstance(raw.get("funding_time"), str)
                            or not raw["funding_time"].isdecimal()
                            or not isinstance(raw.get("funding_rate"), str)
                            or not isinstance(raw.get("mark_price"), str)):
                        raise GrvtHistoryError("Cifras de financiación GRVT no textuales")
                    timestamp = int(raw["funding_time"])
                    if timestamp < lower or timestamp > end_ns or (last_ns is not None and timestamp >= last_ns):
                        raise GrvtHistoryError("Financiación GRVT fuera de rango o desordenada")
                    last_ns = timestamp
                    rows.append(raw)
                if last_ns is not None and last_ns < opened_at_ns:
                    self._mark_ok()
                    return rows
                next_cursor = payload.get("next")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                    raise GrvtHistoryError("Funding GRVT sin ancla anterior o cursor repetido")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            raise GrvtHistoryError("Demasiadas páginas de financiación GRVT")
        except GrvtHistoryError as exc:
            self._mark_err(exc)
            raise
        except Exception as exc:
            self._mark_err(exc)
            raise GrvtHistoryError("GRVT no devolvió financiación verificable") from exc

    def get_margin_rules(self, symbol: str) -> dict | None:
        """Instantánea pública por instrumento; no decide una liquidación."""
        instrument = self._instrument(symbol)
        if instrument is None:
            return None
        try:
            payload = self._http_post(f"{_ROOT}/margin_rules", {"instrument": instrument})
            if (payload.get("instrument") != instrument or not isinstance(payload.get("risk_brackets"), list)
                    or not isinstance(payload.get("max_position_size"), str)):
                raise ValueError("Reglas de margen GRVT incompletas o de otro instrumento")
            self._mark_ok()
            return {
                "instrument": instrument,
                "max_position_size": str(payload["max_position_size"]),
                "risk_brackets": payload["risk_brackets"],
                "fetched_at_ns": time.time_ns(),
            }
        except Exception as exc:
            self._mark_err(exc)
            return None

    def get_instrument_spec(self, symbol: str) -> dict | None:
        """Restricciones públicas actuales del instrumento; sin credenciales."""
        instrument = self._instrument(symbol)
        if instrument is None:
            return None
        try:
            payload = self._http_post(f"{_ROOT}/instrument", {"instrument": instrument})
            row = payload["result"]
            if row.get("instrument") != instrument or row.get("kind") != "PERPETUAL":
                raise ValueError("Especificación GRVT de otro instrumento o mercado")
            if not isinstance(row.get("venues"), list) or "ORDERBOOK" not in row["venues"]:
                raise ValueError("Instrumento GRVT sin libro de órdenes")
            for field in ("min_size", "min_notional", "tick_size"):
                if not isinstance(row.get(field), str):
                    raise ValueError(f"Especificación GRVT sin {field} textual")
            if row.get("max_position_size") is not None and not isinstance(row["max_position_size"], str):
                raise ValueError("Especificación GRVT con máximo inválido")
            if type(row.get("base_decimals")) is not int:
                raise ValueError("Especificación GRVT sin granularidad")
            self._mark_ok()
            return {key: row.get(key) for key in ("instrument", "kind", "venues", "base_decimals",
                                                    "min_size", "min_notional", "tick_size", "max_position_size")} | {
                "fetched_at_ns": time.time_ns()}
        except Exception as exc:
            self._mark_err(exc)
            return None

    def health(self):
        base = super().health()
        base["detail_human"] += ". " + self.DATA_NOTE
        return base
