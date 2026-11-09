"""One explicitly started PAPER observer. No cold resume or REAL authority.

The application service must own the PAPER runtime lease before starting this
worker and retain it until stop() joins. Network calls hold no archive/SQL lock.
Stopping this observer is an interruption, separate from a strategy STOP that
keeps observing an open position in CLOSE_ONLY.
"""
from __future__ import annotations

from threading import Event,RLock,Thread
import time
from uuid import uuid4

from app.derivatives.perp_observed_archive import ObservedArchive,identifier
from app.derivatives.perp_observed_source import can_retry_grvt_candle
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_wire import GapEvent
from app.marketdata.grvt_candle_time import GrvtCandleNotClosed


class ObservedSessionWorker:
    def __init__(self,archive: ObservedArchive,session_id: str,source,*,poll_seconds: float=2.,clock_ns=time.time_ns):
        if type(poll_seconds) not in (float,int) or not 0<poll_seconds<=5:
            raise ObservedError('Espera de observación no válida')
        self.archive=archive;self.session_id=identifier(session_id);self.source=source
        self.clock_ns=clock_ns;self.poll_seconds=poll_seconds
        self._stop=Event();self._lock=RLock();self._thread: Thread|None=None
        self._started=False;self._phase='NOT_STARTED';self._last_error: str|None=None
        self._last_receipt: int|None=None;self._last_quote: int|None=None;self._interruption_saved=False

    def start(self) -> None:
        with self._lock:
            if self._started:return
            state=self.archive.current_state(self.session_id)
            if state.status not in ('RUNNING','CLOSE_ONLY'):raise ObservedError('Sesión finalizada o sin cobertura')
            self.archive.claim_observer(self.session_id,self)
            if self._stop.is_set():
                self._phase='CANCELLED';self._interrupt('ACTIVATION_CANCELLED')
                self.archive.detach(self.session_id)
                raise ObservedError('Observación cancelada antes de arrancar')
            self._started=True;self._phase='STARTING'
            self._thread=Thread(target=self._loop,name='orbis-perp-observed-'+self.session_id[:8],daemon=True)
            try:self._thread.start()
            except Exception:
                self._phase='START_FAILED';self.archive.detach(self.session_id)
                raise

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self,timeout: float|None=None) -> None:
        self.request_stop()
        with self._lock:thread=self._thread
        if thread is not None:thread.join(timeout)

    def snapshot(self) -> dict:
        with self._lock:
            running=bool(self._thread and self._thread.is_alive())
            now=self.clock_ns()
            fresh=bool(running and self._last_receipt is not None and self._last_quote is not None
                       and 0<=now-self._last_receipt<=20_000_000_000
                       and 0<=now-self._last_quote<=15_000_000_000)
            return {'session_id':self.session_id,'running':running,'coverage_current':fresh,
                'phase':self._phase,'last_received_at_ns':str(self._last_receipt) if self._last_receipt else None,
                'last_quote_time_ns':str(self._last_quote) if self._last_quote else None,
                'last_error':self._last_error,'interruption_saved':self._interruption_saved,
                'source':'GRVT_PUBLIC_REST_OBSERVED_POINTS','environment':'PAPER','execution_authority':'NONE'}

    def _interrupt(self,reason: str) -> None:
        try:
            state=self.archive.current_state(self.session_id)
            if state.status not in ('RUNNING','CLOSE_ONLY'):return
            event=GapEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='GAP',
                received_at_ns=str(self.clock_ns()),reason=reason)
            self.archive.append(self.session_id,event,request_id=str(uuid4()))
            with self._lock:self._interruption_saved=True
        except Exception as exc:
            # A locked vault cannot accept an interruption record. Inactivity
            # remains visible from runtime/cache, without claiming a saved GAP.
            with self._lock:self._last_error='INTERRUPTION_NOT_SAVED:'+type(exc).__name__

    def _loop(self) -> None:
        reason='SERVICE_SHUTDOWN'
        try:
            while not self._stop.is_set():
                state=self.archive.current_state(self.session_id)
                if state.status not in ('RUNNING','CLOSE_ONLY'):break
                with self._lock:self._phase='COLLECTING'
                try:
                    event=self.source.poll(state)
                except GrvtCandleNotClosed as exc:
                    if not can_retry_grvt_candle(exc.closes_at_ns,self.clock_ns(),
                            last_observed_ns=state.last_received_ns,last_quote_ns=state.last_quote.event_time_ns):
                        raise
                    with self._lock:
                        self._phase='WAITING_CANDLE';self._last_error=str(exc)
                    self._stop.wait(self.poll_seconds)
                    continue
                if self._stop.is_set():break
                result=self.archive.accept_quote(self.session_id,event,request_id=str(uuid4()))
                current=self.archive.current_state(self.session_id)
                with self._lock:
                    self._phase='OBSERVING' if current.status in ('RUNNING','CLOSE_ONLY') else current.status
                    self._last_error=None
                    self._last_receipt=current.last_received_ns
                    self._last_quote=current.last_quote.event_time_ns
                if result['status'] not in ('RUNNING','CLOSE_ONLY'):break
                self._stop.wait(self.poll_seconds)
        except Exception as exc:
            reason='PUBLIC_SOURCE_OR_ARCHIVE_FAILED'
            with self._lock:self._last_error=type(exc).__name__;self._phase='FAILED'
        finally:
            self._interrupt(reason)
            self.archive.detach(self.session_id)
            with self._lock:
                if self._phase not in ('FAILED','GAP','HALTED','LIQUIDATED','STOPPED'):self._phase='INTERRUPTED'
