"""Trabajador REAL optativo: permiso efímero, parada cooperativa y cero reenvíos.

No lee tokens HTTP ni toma el cerrojo de RealAccess durante un ciclo: evita
invertir el orden entre el acceso HTTP y la guarda de la bóveda. El permiso
expira aunque la UI sondee su estado, y nunca se persiste una credencial.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Lock, Thread, current_thread
from typing import Callable
import math
import time

from app.live_execution.mexc_spot_bot import run_mexc_spot_bot_once
from app.live_execution.spot_bot_store import SpotBotStoreError
from app.portfolio.vault import VaultError


class MexcBotPermissionError(SpotBotStoreError):
    """El usuario retiró el permiso o la sesión dejó de estar vigente."""


class MexcBotSupervisor:
    def __init__(self, vault, view, trade, paper, bot_id: str, *, deadline: float,
                 quote_get=None, rules_fetch=None, interval_seconds: float = 5.0,
                 monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], datetime] | None = None,
                 extra_authorized: Callable[[], None] | None = None,
                 on_exit: Callable[[], None] | None = None,
                 expected_policy_id: str | None = None,
                 execution_permission:dict|None=None,market_candles:Callable|None=None,
                 policy_expires_at:datetime|None=None,
                 cycle=run_mexc_spot_bot_once):
        if (not math.isfinite(deadline) or type(interval_seconds) not in (int,float)
                or not math.isfinite(interval_seconds) or not 0.1 <= interval_seconds <= 10):
            raise ValueError("Configuración del trabajador de bot inválida")
        self.vault, self.bot_id = vault, bot_id
        self._view, self._trade, self._paper = view, trade, paper
        self._deadline, self._monotonic, self._clock = deadline, monotonic, clock
        self._quote_get, self._rules_fetch, self._cycle = quote_get, rules_fetch, cycle
        self._extra_authorized, self._on_exit = extra_authorized, on_exit
        self._expected_policy_id = expected_policy_id
        self._execution_permission,self._market_candles = execution_permission,market_candles
        self._policy_expires_at = policy_expires_at
        self._interval = interval_seconds
        self._stop, self._lock = Event(), Lock()
        self._thread: Thread | None = None
        self._generation: int | None = None
        self._status = "NOT_STARTED"
        self._cycles = 0
        self._last_at: str | None = None
        self._last_result: dict | None = None

    def extend_deadline(self, deadline: float) -> None:
        """Sólo RealAccess llama al registrar actividad humana autorizada."""
        with self._lock:
            if self._execution_permission is None and not self._stop.is_set() and math.isfinite(deadline):
                self._deadline = deadline

    def _authorized(self) -> None:
        with self._lock:
            if self._stop.is_set():
                raise MexcBotPermissionError("Permiso de bot retirado")
            if self._monotonic() >= self._deadline:
                raise MexcBotPermissionError("Permiso de bot caducado")
            if self._policy_expires_at is not None and (self._clock or (lambda:datetime.now(timezone.utc)))() >= self._policy_expires_at:
                raise MexcBotPermissionError("La apertura diaria del bot caducó")
        if self.vault.session_generation != self._generation:
            raise MexcBotPermissionError("La sesión REAL del bot cambió")
        if self._extra_authorized is not None:
            self._extra_authorized()

    def start(self) -> None:
        # Captura la generación antes del cerrojo propio; el ciclo nunca
        # mantiene éste mientras espera al de la bóveda.
        generation = self.vault.session_generation
        if generation is None:
            raise MexcBotPermissionError("La bóveda REAL está bloqueada")
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                raise MexcBotPermissionError("Este trabajador ya se inició o detuvo")
            if self._monotonic() >= self._deadline:
                raise MexcBotPermissionError("Permiso de bot caducado")
            self._generation = generation
            self._status = "RUNNING"
            self._thread = Thread(target=self._run, name="orbis-mexc-bot-" + self.bot_id[:8], daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None or thread is current_thread():
            return thread is None
        thread.join(timeout)
        return not thread.is_alive()

    def state(self) -> dict:
        with self._lock:
            return {"running":bool(self._thread and self._thread.is_alive()), "status":self._status,
                    "cycles":self._cycles, "last_cycle_at":self._last_at,
                    "stop_requested":self._stop.is_set(),
                    "permission_remaining_seconds":max(0, int(self._deadline - self._monotonic())),
                    "last_result":dict(self._last_result) if self._last_result else None,
                    "policy_id":self._expected_policy_id,
                    **({"evidence_remaining_seconds":max(0,int((self._policy_expires_at-(self._clock or (lambda:datetime.now(timezone.utc)))()).total_seconds()))}
                       if self._policy_expires_at is not None else {})}

    def _policy_renewed(self,policy):
        with self._lock:
            self._expected_policy_id,self._policy_expires_at = policy.policy_id,policy.expires_at

    def run_once(self) -> dict:
        self._authorized()
        result = self._cycle(self.vault, self._view, self._trade, self._paper, self.bot_id,
            quote_get=self._quote_get, rules_fetch=self._rules_fetch, clock=self._clock,
            assert_active=self._authorized, expected_policy_id=self._expected_policy_id,
            execution_permission=self._execution_permission,market_candles=self._market_candles,
            **({"on_policy_renewed":self._policy_renewed}
               if self._execution_permission and self._execution_permission.get("protocol") == "CONTINUOUS_V1" else {}))
        # Únicamente estados propios; no publicar excepciones de proveedores.
        order = result.get("order") or {}
        safe = {"action":result["action"], "reason":result["reason"],
                "intent_id":order.get("intent_id") or result.get("intent_id"),
                "journal_status":order.get("journal_status") or result.get("journal_status"), "paused":bool(result.get("paused"))}
        with self._lock:
            self._cycles += 1
            self._last_at = datetime.now(timezone.utc).isoformat()
            self._last_result = safe
            self._status = "PAUSED_NEEDS_REVIEW" if safe["paused"] or safe["action"] == "PAUSE" or safe["reason"] == "ORDER_REVIEW_REQUIRED" else "RUNNING"
        return safe

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    result = self.run_once()
                except MexcBotPermissionError:
                    with self._lock:
                        self._status = "PERMISSION_REVOKED_OR_EXPIRED"
                    return
                except VaultError:
                    with self._lock:
                        self._status = "VAULT_LOCKED"
                    return
                except Exception:
                    with self._lock:
                        self._status = "ERROR_NEEDS_REVIEW"
                    return
                if result["paused"] or result["action"] == "PAUSE" or result["reason"] == "ORDER_REVIEW_REQUIRED":
                    return
                self._stop.wait(self._interval)
            with self._lock:
                self._status = "STOPPED"
        finally:
            self._stop.set()
            with self._lock:
                self._view = self._trade = self._paper = None
                self._market_candles = self._extra_authorized = None
            if self._on_exit is not None:
                try:
                    self._on_exit()
                except Exception:
                    # La marca de ciclo/orden se conserva si ya se bloqueó la
                    # bóveda. Nunca reabrirla ni inventar que se limpió.
                    pass
