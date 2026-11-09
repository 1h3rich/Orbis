"""Observa señales GRVT y ejecuta sólo el modo PAPER autorizado localmente."""

from __future__ import annotations

from collections.abc import Callable

from app.core.local_observer import LocalObserver
from app.derivatives.signal_monitor import SignalMonitor, SignalMonitorError
from app.derivatives.paper_math import PaperPerpError
from app.lab.engine import INTERVALS, LabError


class SignalWorker(LocalObserver):
    def __init__(self, monitor: SignalMonitor, *, retry_seconds: float = 5.0,
                 idle_seconds: float = 60.0,
                 after_poll: Callable[[], None] | None = None):
        self.monitor = monitor
        def observe(session_id: str) -> dict:
            result = monitor.poll(session_id, observer="LOCAL_WORKER")
            if after_poll is not None:
                after_poll()
            return result
        super().__init__(name="orbis-paper-grvt-signals", items=monitor.active_sessions,
                         due_at=lambda row: row["last_t"] + (1 if row["pending"] else 2) * INTERVALS[row["timeframe"]],
                         run_item=observe,
                         clock_ns=monitor.clock_ns,
                         retry_exceptions=(SignalMonitorError, LabError, PaperPerpError),
                         execution="PAPER_AUTO_OR_OBSERVE_NO_REAL_ORDERS", retry_seconds=retry_seconds,
                         idle_seconds=idle_seconds)
