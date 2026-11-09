"""Planificador local reutilizable para observaciones PAPER sin órdenes.

La idempotencia y la decisión financiera pertenecen al dominio persistido;
esta hebra sólo despierta a tiempo, reintenta datos y expone su latido.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Condition, Event, Lock, Thread
import time


class LocalObserver:
    def __init__(self, *, name: str, items: Callable[[], list[dict]],
                 due_at: Callable[[dict], int], run_item: Callable[[str], dict],
                 clock_ns: Callable[[], int], retry_exceptions: tuple[type[Exception], ...],
                 execution: str, retry_seconds: float = 5.0, idle_seconds: float = 60.0):
        if retry_seconds <= 0 or idle_seconds <= 0:
            raise ValueError("Intervalos del observador inválidos")
        self.name = name
        self.items = items
        self.due_at = due_at
        self.run_item = run_item
        self.clock_ns = clock_ns
        self.retry_exceptions = retry_exceptions
        self.execution = execution
        self.retry_seconds = retry_seconds
        self.idle_seconds = idle_seconds
        self._stop = Event()
        self._condition = Condition()
        self._wake_sequence = 0
        self._thread: Thread | None = None
        self._lock = Lock()
        self._last_cycle_ns: int | None = None
        self._last_error: str | None = None
        self._error_count = 0

    def snapshot(self) -> dict:
        with self._lock:
            age = ((time.time_ns() - self._last_cycle_ns) / 1_000_000_000
                   if self._last_cycle_ns is not None else None)
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "last_cycle_ns": self._last_cycle_ns, "last_error": self._last_error,
                    "heartbeat_age_seconds": age, "error_count": self._error_count,
                    "mode": "LOCAL_PROCESS_ONLY", "execution": self.execution}

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = Thread(target=self._loop, name=self.name, daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

    def stop(self, timeout: float | None = None) -> None:
        """Drena el trabajo en curso antes de soltar la lease del proceso.

        Un timeout explícito queda disponible para diagnósticos, pero el
        apagado normal no debe devolver mientras esta hebra pueda escribir.
        """
        self.request_stop()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.ident is not None:
            thread.join(timeout)

    def wake(self) -> None:
        with self._condition:
            self._wake_sequence += 1
            self._condition.notify_all()

    def run_once(self) -> dict[str, str]:
        now = self.clock_ns() // 1_000_000_000
        due = [row["id"] for row in self.items() if self.due_at(row) <= now]
        results: dict[str, str] = {}
        for item_id in due:
            if self._stop.is_set():
                break
            try:
                results[item_id] = self.run_item(item_id)["poll_result"]
            except self.retry_exceptions as exc:
                results[item_id] = "RETRY"
                with self._lock:
                    self._last_error = f"Datos no disponibles: {exc}"
                    self._error_count += 1
            except Exception as exc:
                results[item_id] = "ERROR"
                with self._lock:
                    self._last_error = f"Error interno {type(exc).__name__}"
                    self._error_count += 1
        with self._lock:
            self._last_cycle_ns = time.time_ns()
            if not any(value in ("RETRY", "ERROR") for value in results.values()):
                self._last_error = None
        return results

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._condition:
                seen_wake = self._wake_sequence
            try:
                results = self.run_once()
                now = self.clock_ns() // 1_000_000_000
                active = [self.due_at(row) for row in self.items()]
                if any(value in ("RETRY", "ERROR") for value in results.values()):
                    delay = self.retry_seconds
                elif active:
                    delay = max(0.1, min(self.idle_seconds, min(active) - now))
                else:
                    delay = self.idle_seconds
            except Exception as exc:
                with self._lock:
                    self._last_error = f"Error del observador {type(exc).__name__}"
                    self._error_count += 1
                delay = self.retry_seconds
            with self._condition:
                # Si wake llegó mientras se consultaba el proveedor, no
                # dormir ahora: procesar inmediatamente la nueva sesión.
                if not self._stop.is_set() and self._wake_sequence == seen_wake:
                    self._condition.wait(delay)
