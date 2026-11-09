"""Canal público GRVT de ticker para observación PAPER, sin credenciales.

El feed no es una orden ni un stop colocado en el exchange. Cada mensaje se
valida contra el instrumento y su tiempo antes de que otro módulo lo use.
"""

from __future__ import annotations

import json
import time
from typing import Callable,Literal

from websockets.sync.client import connect

from app.derivatives.paper_math import PaperPerpError, PerpQuote
from app.marketdata.adapters.grvt import GrvtAdapter


URL = "wss://market-data.grvt.io/ws/full"
TESTNET_URL = "wss://market-data.testnet.grvt.io/ws/full"
STREAM = "v1.ticker.s"


class GrvtStreamError(ValueError):
    """No se puede confiar en la suscripción o en el mensaje recibido."""


def instruments_for(symbols: tuple[str, ...]) -> dict[str, str]:
    if not symbols or len(set(symbols)) != len(symbols):
        raise GrvtStreamError("La suscripción GRVT requiere símbolos únicos")
    adapter = GrvtAdapter()
    instruments = {adapter._instrument(symbol): symbol for symbol in symbols}
    if None in instruments or len(instruments) != len(symbols):
        raise GrvtStreamError("Símbolo GRVT no admitido por el canal público")
    return instruments


def subscription(instruments: dict[str, str]) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": "subscribe",
                       "params": {"stream": STREAM,
                                  "selectors": [instrument + "@500" for instrument in sorted(instruments)]},
                       "id": 1}, separators=(",", ":"))


def parse_ticker_frame(raw: str, instruments: dict[str, str], now_ns: int) -> tuple[str, PerpQuote] | None:
    """Devuelve (símbolo, cotización) o None para una respuesta de control.

    Se rechazan mensajes de otros canales, instrumentos y horas futuras o
    envejecidas. No se infiere un precio ejecutable a partir de `last_price`.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise GrvtStreamError("Mensaje GRVT no es JSON") from exc
    if not isinstance(payload, dict):
        raise GrvtStreamError("Mensaje GRVT no es un objeto")
    if "error" in payload:
        raise GrvtStreamError("GRVT rechazó la suscripción pública")
    if "result" in payload:
        result = payload["result"]
        subs = result.get("subs") if isinstance(result, dict) else None
        if (payload.get("id") != 1 or payload.get("method") != "subscribe"
                or not isinstance(result, dict) or result.get("stream") != STREAM
                or not isinstance(subs, list) or not all(isinstance(item, str) for item in subs)
                or set(subs) != {name + "@500" for name in instruments}):
            raise GrvtStreamError("Confirmación GRVT de suscripción inesperada")
        return None
    if payload.get("stream") != STREAM:
        raise GrvtStreamError("Canal GRVT inesperado")
    feed = payload.get("feed")
    if not isinstance(feed, dict):
        raise GrvtStreamError("Ticker GRVT sin datos")
    instrument = feed.get("instrument")
    if (instrument not in instruments or payload.get("selector") != instrument + "@500"
            or not isinstance(feed.get("event_time"), str)
            or not feed["event_time"].isdecimal()):
        raise GrvtStreamError("Ticker GRVT de otro instrumento o sin hora textual")
    try:
        quote = PerpQuote.from_ticker({"instrument": instrument,
                                       "event_time_ns": feed["event_time"],
                                       "mark_price": feed.get("mark_price"),
                                       "bid": feed.get("best_bid_price"),
                                       "ask": feed.get("best_ask_price")}, instrument, now_ns)
    except PaperPerpError as exc:
        raise GrvtStreamError(f"Ticker GRVT no verificable: {exc}") from exc
    return instruments[instrument], quote


class GrvtTickerStream:
    """Una conexión de sólo lectura para los instrumentos requeridos ahora."""

    def __init__(self, symbols: tuple[str, ...], *, connector: Callable = connect,
                 clock_ns: Callable[[], int] = time.time_ns,
                 network: Literal['MAINNET','TESTNET']='MAINNET'):
        if network not in ('MAINNET','TESTNET'): raise GrvtStreamError('Red GRVT inválida')
        self.instruments = instruments_for(symbols)
        self.url=TESTNET_URL if network=='TESTNET' else URL
        self.connector = connector
        self.clock_ns = clock_ns
        self.socket = None

    def __enter__(self):
        self.socket = self.connector(self.url, open_timeout=7, close_timeout=2,
                                     ping_interval=20, ping_timeout=10,
                                     max_size=1_000_000)
        try:
            self.socket.send(subscription(self.instruments))
            confirmation = self.socket.recv(timeout=7)
            if parse_ticker_frame(confirmation, self.instruments, self.clock_ns()) is not None:
                raise GrvtStreamError("GRVT no confirmó la suscripción antes de enviar precios")
        except Exception:
            self.socket.close()
            self.socket = None
            raise
        return self

    def __exit__(self, _type, _value, _traceback):
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def recv(self, *, timeout: float = 2.0) -> tuple[str, PerpQuote] | None:
        if self.socket is None:
            raise GrvtStreamError("Canal GRVT no conectado")
        return parse_ticker_frame(self.socket.recv(timeout=timeout), self.instruments, self.clock_ns())
