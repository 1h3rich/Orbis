"""Revocable public-data worker for a separately authorized REAL coordinator.

There are no exchange credentials or financial actions here. Every source
record must be durably appended before a candidate is published. Lost archive
responses stop this worker; they never replay a write or restart a thread.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from threading import Event, RLock, Thread
import time
from typing import Callable

from app.derivatives.perp_observed_source import can_retry_grvt_candle
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import (
    MAX_CONSUMER_DELAY_NS, MAX_SIGNAL_DELAY_NS, PerpSignalSeed, PerpSignalState,
    record_signal_input, signal_checkpoint,
)
from app.marketdata.grvt_candle_time import GrvtCandleNotClosed


@dataclass(frozen=True)
class PerpEntrySignal:
    seed_hash: str
    origin_reference_hash: str
    profile_json: str
    definition_json: str
    instrument: str
    timeframe: str
    decision_json: str


class PerpSignalWorker:
    def __init__(self, seed: PerpSignalSeed, *, source: GrvtPerpSignalSource,
                 append_record: Callable[[dict], None], require_permission: Callable[[], None],
                 clock_ns: Callable[[], int]=time.time_ns, poll_seconds: float=2,
                 waiter: Callable | None=None):
        if not callable(append_record) or not callable(require_permission):
            raise ValueError('Falta diario durable o permiso revocable de la fuente REAL')
        if type(poll_seconds) not in (int, float) or not 0 < poll_seconds <= 5:
            raise ValueError('Intervalo de la fuente REAL fuera de límites')
        self._state = seed.start()
        self._source = source
        self._append = append_record
        self._permission = require_permission
        self._clock = clock_ns
        self._poll = poll_seconds
        self._wait = waiter if waiter is not None else lambda stop, seconds:stop.wait(seconds)
        self._lock = RLock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._candidate: PerpEntrySignal | None = None
        self._status = 'PREPARED'
        self._last_ns = self._state.last_accepted_ns

    def _checked(self) -> int:
        if self._stop.is_set():
            raise RuntimeError('Fuente REAL detenida')
        self._permission()
        at = self._clock()
        # Only the worker thread may advance this clock. Candidate readers
        # check against it without changing the worker's producer state.
        if type(at) is not int or not self._last_ns <= at < 2**63:
            raise ValueError('Reloj de la fuente REAL regresado o inválido')
        self._last_ns = at
        return at

    def start(self) -> None:
        self._checked()
        with self._lock:
            if self._thread is not None or self._status != 'PREPARED' or self._stop.is_set():
                raise ValueError('La fuente REAL no permite reinicio ni restauración automática')
            self._thread = Thread(target=self._run, name='orbis-grvt-perp-signals', daemon=True)
            self._status = 'RUNNING'
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._status = 'START_ERROR'
                self._stop.set()
                raise

    def stop(self) -> None:
        # Revocation precedes any storage or waiting. An in-flight public
        # request may finish, but cannot publish a fresh entry afterwards.
        self._stop.set()
        with self._lock:
            self._candidate = None
            if self._status in ('PREPARED','RUNNING'):
                self._status = 'STOPPED'

    def join(self, timeout: float=0) -> bool:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {'status':self._status, 'worker_alive':bool(self._thread and self._thread.is_alive()),
                'checkpoint':signal_checkpoint(self._state), 'execution_authority':'NONE'}

    def candidate(self) -> PerpEntrySignal | None:
        # This method never grants permission. The executor still checks its
        # own RAM authorization, durable selection, limits and fresh preflight.
        if self._stop.is_set():
            return None
        self._permission()
        at = self._clock()
        with self._lock:
            candidate = self._candidate
            if (self._status != 'RUNNING' or candidate is None or self._stop.is_set()
                    or type(at) is not int or not self._last_ns <= at < 2**63):
                return None
            decision = json.loads(candidate.decision_json)
            if (not 0 <= at-int(decision['known_at_ns']) <= MAX_SIGNAL_DELAY_NS
                    or not 0 <= at-int(decision['source_close_ns']) <= MAX_SIGNAL_DELAY_NS
                    or at-self._state.last_observed_ns > MAX_CONSUMER_DELAY_NS):
                return None
            return candidate

    def _publish(self, state: PerpSignalState, decisions: tuple[str, ...]) -> None:
        for encoded in decisions:
            decision = json.loads(encoded)
            if decision['entry_matched']:
                self._candidate = PerpEntrySignal(state.seed_hash, state.origin_reference_hash,
                    state.profile_json, state.definition_json, state.instrument, state.timeframe, encoded)

    def _run(self) -> None:
        stage = 'PERMISSION'
        try:
            while not self._stop.is_set():
                stage = 'PERMISSION'
                self._checked()
                stage = 'COLLECTION'
                try:
                    raw = self._source.poll(self._state)
                except GrvtCandleNotClosed as exc:
                    with self._lock:
                        self._candidate = None
                    stage = 'PERMISSION'
                    now = self._checked()
                    stage = 'COLLECTION'
                    if not can_retry_grvt_candle(exc.closes_at_ns, now,
                            last_observed_ns=self._state.last_observed_ns,
                            last_quote_ns=self._state.last_quote.event_time_ns):
                        raise
                    self._wait(self._stop, self._poll)
                    continue
                stage = 'PERMISSION'
                accepted = self._checked()
                # Account for time spent queued between collection and this
                # consumer. Preserve collected_at_ns and the venue timestamp.
                raw = type(raw).model_validate({**raw.payload(), 'accepted_at_ns':str(accepted)})
                stage = 'EVALUATION'
                result, record = record_signal_input(self._state, raw)
                stage = 'PERMISSION'
                self._checked()
                stage = 'ARCHIVE'
                self._append(record)
                stage = 'PERMISSION'
                at = self._checked()
                with self._lock:
                    self._state = result.state
                    if self._stop.is_set():
                        return
                    if result.state.status != 'RUNNING':
                        self._status = result.state.status
                        self._candidate = None
                        return
                    if at-int(raw.accepted_at_ns) > MAX_CONSUMER_DELAY_NS:
                        self._status = 'ARCHIVE_DELAY'
                        self._candidate = None
                        return
                    self._publish(result.state, result.decisions_json)
                self._wait(self._stop, self._poll)
        except Exception:
            with self._lock:
                if not self._stop.is_set():
                    self._status = {'ARCHIVE':'ARCHIVE_ERROR', 'COLLECTION':'SOURCE_ERROR',
                        'EVALUATION':'SOURCE_ERROR', 'PERMISSION':'PERMISSION_REVOKED'}[stage]
                self._candidate = None
        finally:
            self._stop.set()
            with self._lock:
                self._candidate = None
                if self._status == 'RUNNING':
                    self._status = 'STOPPED'
