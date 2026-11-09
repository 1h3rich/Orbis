"""Vigila stops/objetivos AUTO PAPER con ticker público continuo de GRVT.

La hebra jamás envía órdenes. Si pierde cobertura de cotizaciones, la sesión
queda en GAP y la posición ficticia permanece abierta para cierre manual.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock, Thread

from app.derivatives.paper_math import MarginRules, PaperPerpError
from app.derivatives.signal_monitor import SignalMonitor, SignalMonitorError
from app.marketdata.grvt_stream import GrvtTickerStream

RULES_REFRESH_NS = 30_000_000_000
RULES_RETRY_NS = 10_000_000_000


class SignalRiskWorker:
    def __init__(self, monitor: SignalMonitor, *, stream_factory: Callable = GrvtTickerStream,
                 action_wake: Callable[[], None] | None = None, idle_seconds: float = 2.0):
        if idle_seconds <= 0:
            raise ValueError("Espera de ticker inválida")
        self.monitor = monitor
        self.stream_factory = stream_factory
        self.action_wake = action_wake
        self.idle_seconds = idle_seconds
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._connected = False
        self._symbols: tuple[str, ...] = ()
        self._last_frame_ns: int | None = None
        self._last_error: str | None = None
        self._error_count = 0
        self._margin_rules: dict[str, MarginRules] = {}
        self._next_rules_fetch_ns: dict[str, int] = {}

    def _current_margin_rules(self, symbol: str, instrument: str) -> MarginRules | None:
        """Usa reglas públicas recientes; una consulta fallida nunca rejuvenece la caché."""
        now_ns = self.monitor.clock_ns()
        cached = self._margin_rules.get(symbol)
        if now_ns >= self._next_rules_fetch_ns.get(symbol, 0):
            self._next_rules_fetch_ns[symbol] = now_ns + RULES_RETRY_NS
            try:
                raw = self.monitor.adapter_factory().get_margin_rules(symbol)
                refreshed = MarginRules.from_payload(raw, instrument, self.monitor.clock_ns())
                self._margin_rules[symbol] = refreshed
                self._next_rules_fetch_ns[symbol] = self.monitor.clock_ns() + RULES_REFRESH_NS
                cached = refreshed
            except Exception as exc:
                with self._lock:
                    self._last_error = f"Reglas de margen GRVT no verificables: {type(exc).__name__}"
                    self._error_count += 1
        try:
            return MarginRules.from_payload(cached.json() if cached else None,
                                            instrument, self.monitor.clock_ns())
        except PaperPerpError:
            return None

    def snapshot(self) -> dict:
        with self._lock:
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "connected": self._connected, "symbols": list(self._symbols),
                    "last_frame_ns": self._last_frame_ns,
                    "last_frame_age_seconds": ((self.monitor.clock_ns() - self._last_frame_ns) / 1e9
                                               if self._last_frame_ns is not None else None),
                    "last_error": self._last_error, "error_count": self._error_count,
                    "mode": "GRVT_PUBLIC_WS_PAPER_ONLY_NO_REAL_ORDERS"}

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = Thread(target=self._loop, name="orbis-paper-grvt-risk", daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def stop(self, timeout: float | None = None) -> None:
        """No liberar la lease GRVT PAPER con un ticker en proceso."""
        self.request_stop()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.ident is not None:
            thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _loop(self) -> None:
        retry = 0
        while not self._stop.is_set():
            try:
                self.monitor.expire_risk_gaps()
                sessions = self.monitor.active_risk_sessions()
                symbols = tuple(sorted({row["symbol"] for row in sessions}))
                if not symbols:
                    with self._lock:
                        self._connected = False
                        self._symbols = ()
                    retry = 0
                    self._wait(self.idle_seconds)
                    continue
                with self.stream_factory(symbols) as stream:
                    with self._lock:
                        self._connected = True
                        self._symbols = symbols
                        self._last_frame_ns = None
                        self._last_error = None
                    retry = 0
                    while not self._stop.is_set():
                        self.monitor.expire_risk_gaps()
                        sessions = self.monitor.active_risk_sessions()
                        current = tuple(sorted({row["symbol"] for row in sessions}))
                        if current != symbols:
                            break  # Reabrir con suscripciones exactas.
                        try:
                            item = stream.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        if item is None:
                            continue
                        symbol, quote = item
                        with self._lock:
                            self._last_frame_ns = quote.event_time_ns
                        rules = self._current_margin_rules(symbol, quote.instrument)
                        for session in sessions:
                            if session["symbol"] != symbol:
                                continue
                            try:
                                outcome = self.monitor.on_risk_quote(
                                    session["id"], quote, rules, rules_checked=True,
                                )
                                if outcome in ("EXECUTED", "ACTION_ALREADY_PENDING") and self.action_wake:
                                    self.action_wake()
                            except SignalMonitorError as exc:
                                with self._lock:
                                    self._last_error = str(exc)
                                    self._error_count += 1
            except Exception as exc:
                with self._lock:
                    self._last_error = f"Stream GRVT no disponible: {type(exc).__name__}"
                    self._error_count += 1
                retry = min(retry + 1, 5)
                self._wait(min(2 ** (retry - 1), 16))
            finally:
                with self._lock:
                    self._connected = False
                    self._symbols = ()
