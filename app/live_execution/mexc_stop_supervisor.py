"""Trabajador optativo para repetir ciclos de parada MEXC de una cuenta.

La API puede iniciarlo sólo con confirmación expresa; no hay activación UI.
El propietario debe detenerlo al bloquear la bóveda o descartar credenciales.
Un proceso cerrado deja de vigilar MEXC: no equivale a una parada garantizada
por el exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Callable
import math
import re
import time

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.mexc_live_cancel import (
    MexcStoppedSupervisionStep, supervise_stopped_spot_account_once,
)
from app.live_execution.mexc_live_order import MexcSpotLiveClient
from app.portfolio.models import RealLiveStop
from app.portfolio.vault import RealVault, VaultError


class MexcStopSupervisorError(RuntimeError):
    """El trabajador no puede ponerse en marcha con seguridad."""


@dataclass(frozen=True, slots=True)
class MexcStopSupervisorState:
    running: bool
    status: str
    cycles: int
    intent_id: str | None
    unresolved_intents: int | None
    stop_requested: bool = False
    stalled: bool = False


class MexcStopSupervisor:
    """Un único trabajador por instancia, con parada cooperativa.

    Cada ciclo delega en el journal durable: el hilo nunca reintenta un DELETE
    por su cuenta. Un error no se trata como éxito ni libera reservas. Tras un
    error el trabajador se detiene para exigir revisión explícita.
    """

    def __init__(self, vault: RealVault, trade: MexcSpotLiveClient,
                 view: MexcReadOnlyClient, account_ref: str, *,
                 interval_seconds: float = 10.0,
                 max_silence_seconds: float = 90.0,
                 clock: Callable[[], float] = time.monotonic,
                 cycle: Callable[..., MexcStoppedSupervisionStep] = supervise_stopped_spot_account_once):
        if (not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref)
                or type(interval_seconds) not in (int, float)
                or not math.isfinite(interval_seconds)
                or not 0.25 <= interval_seconds <= 60
                or type(max_silence_seconds) not in (int, float)
                or not math.isfinite(max_silence_seconds)
                or not interval_seconds + 5 <= max_silence_seconds <= 600
                or not callable(clock)):
            raise MexcStopSupervisorError("Configuración de supervisor MEXC inválida")
        self._vault = vault
        self._trade = trade
        self._view = view
        self._account_ref = account_ref
        self._interval = float(interval_seconds)
        self._max_silence = float(max_silence_seconds)
        self._clock = clock
        self._cycle = cycle
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._status = "NOT_STARTED"
        self._cycles = 0
        self._intent_id: str | None = None
        self._unresolved: int | None = None
        self._vault_generation: int | None = None
        self._last_progress_at: float | None = None

    def start(self) -> None:
        """Arranca sólo para una cuenta ya detenida en la bóveda desbloqueada."""
        with self._lock:
            if self._thread is not None:
                raise MexcStopSupervisorError("El supervisor MEXC ya fue iniciado")
            if self._stop.is_set():
                raise MexcStopSupervisorError("El supervisor MEXC ya fue detenido")
            generation = self._vault.session_generation
            if generation is None:
                raise MexcStopSupervisorError("La bóveda REAL está bloqueada")
            try:
                same_reference = self._view.account_reference() == self._account_ref
            except Exception:
                raise MexcStopSupervisorError("No se pudo verificar la referencia View") from None
            if not same_reference:
                raise MexcStopSupervisorError("La clave View no pertenece a la cuenta detenida")
            try:
                with self._vault.session() as db:
                    stop = db.get(RealLiveStop, self._account_ref)
                    if stop is None or not stop.stopped:
                        raise MexcStopSupervisorError("La cuenta MEXC no está detenida")
            except VaultError:
                raise MexcStopSupervisorError("La bóveda REAL está bloqueada") from None
            if self._vault.session_generation != generation:
                raise MexcStopSupervisorError("La sesión de la bóveda REAL cambió")
            self._vault_generation = generation
            self._status = "RUNNING"
            self._last_progress_at = self._clock()
            self._thread = Thread(target=self._run, name="orbis-mexc-stop", daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        """Señala la parada sin esperar una llamada de red en curso."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        """Devuelve False si la llamada externa aún no terminó; no la interrumpe."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def state(self) -> MexcStopSupervisorState:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            stalled = bool(running and self._last_progress_at is not None
                           and self._clock() - self._last_progress_at > self._max_silence)
            return MexcStopSupervisorState(
                running=running,
                status=self._status, cycles=self._cycles,
                intent_id=self._intent_id, unresolved_intents=self._unresolved,
                stop_requested=self._stop.is_set(),
                stalled=stalled,
            )

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                current_generation = self._vault.session_generation
                if current_generation != self._vault_generation:
                    with self._lock:
                        self._status = ("VAULT_LOCKED" if current_generation is None
                                        else "VAULT_SESSION_REPLACED")
                    return
                try:
                    with self._vault.guard_session_generation(self._vault_generation):
                        result = self._cycle(self._vault, self._trade, self._view,
                                             self._account_ref)
                except VaultError:
                    current_generation = self._vault.session_generation
                    with self._lock:
                        self._status = ("VAULT_LOCKED" if current_generation is None
                                        else "VAULT_SESSION_REPLACED")
                    return
                except Exception:
                    # Nunca se publican respuestas brutas ni credenciales. El
                    # journal conserva el resultado incierto para conciliación.
                    with self._lock:
                        self._status = "ERROR_NEEDS_REVIEW"
                    return
                with self._lock:
                    self._cycles += 1
                    self._intent_id = result.intent_id
                    self._unresolved = result.unresolved_intents
                    completed_at = self._clock()
                    if (self._last_progress_at is not None
                            and completed_at - self._last_progress_at > self._max_silence):
                        # El ciclo pudo tocar MEXC antes de expirar: no se
                        # repite a ciegas ni se anuncia como vigilancia sana.
                        self._status = "STALLED_NEEDS_REVIEW"
                        self._stop.set()
                        return
                    self._status = result.status
                    self._last_progress_at = completed_at
                self._stop.wait(self._interval)
            with self._lock:
                self._status = "STOPPED"
        finally:
            # Descarta las referencias a credenciales al salir del hilo; Python
            # no garantiza borrado físico de strings en memoria.
            with self._lock:
                self._trade = None
                self._view = None
