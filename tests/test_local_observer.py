"""El observador no pierde activaciones entre consultar y dormir."""

from inspect import signature
from threading import Event, Thread
import time

from app.core.local_observer import LocalObserver
from app.derivatives.signal_risk_worker import SignalRiskWorker


def test_wake_during_schedule_does_not_sleep_past_due_item():
    completed = Event()
    calls = {"items": 0}
    holder = {}

    def items():
        calls["items"] += 1
        if calls["items"] == 2:
            holder["worker"].wake()
            return []
        return [{"id": "due", "at": 0}] if calls["items"] >= 3 else []

    worker = LocalObserver(name="test-observer", items=items,
        due_at=lambda row: row["at"],
        run_item=lambda _: (completed.set() or {"poll_result": "OBSERVED"}),
        clock_ns=time.time_ns, retry_exceptions=(),
        execution="TEST", idle_seconds=60)
    holder["worker"] = worker
    worker.start()
    try:
        assert completed.wait(1), "wake durante el ciclo no debe perderse"
    finally:
        worker.stop()


def test_default_stop_waits_for_in_flight_paper_poll():
    # app.main llama stop() sin argumentos antes de liberar las leases PAPER.
    assert signature(LocalObserver.stop).parameters["timeout"].default is None
    started, release, stopped = Event(), Event(), Event()

    def poll(_item_id):
        started.set()
        release.wait()
        return {"poll_result": "OBSERVED"}

    worker = LocalObserver(name="test-drain", items=lambda: [{"id": "due", "at": 0}],
                           due_at=lambda row: row["at"], run_item=poll,
                           clock_ns=time.time_ns, retry_exceptions=(), execution="TEST")
    worker.start()
    stopper = Thread(target=lambda: (worker.stop(), stopped.set()), daemon=True)
    try:
        assert started.wait(1)
        stopper.start()
        assert not stopped.wait(0.05), "Se liberaría la lease con un poll todavía activo"
    finally:
        release.set()
        stopper.join(timeout=2)
        worker.stop()
    assert stopped.is_set()
    assert not worker.snapshot()["running"]


def test_grvt_risk_worker_also_drains_before_shutdown():
    assert signature(SignalRiskWorker.stop).parameters["timeout"].default is None
    release, stopped = Event(), Event()
    worker = SignalRiskWorker(monitor=object())
    worker._thread = Thread(target=release.wait, daemon=True)
    worker._thread.start()
    stopper = Thread(target=lambda: (worker.stop(), stopped.set()), daemon=True)
    try:
        stopper.start()
        assert not stopped.wait(0.05)
    finally:
        release.set()
        stopper.join(timeout=2)
        worker.stop()
    assert stopped.is_set()
    assert not worker.snapshot()["running"]


def test_stop_is_safe_when_thread_never_started():
    observer = LocalObserver(name="test-never-started", items=lambda: [],
                             due_at=lambda row: 0, run_item=lambda _: {},
                             clock_ns=time.time_ns, retry_exceptions=(), execution="TEST")
    observer._thread = Thread(target=lambda: None)
    observer.stop()

    risk = SignalRiskWorker(monitor=object())
    risk._thread = Thread(target=lambda: None)
    risk.stop()
