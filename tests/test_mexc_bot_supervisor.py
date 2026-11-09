"""Permiso efímero y parada justo antes de una acción externa controlada."""

from threading import Event

from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from app.portfolio.vault import RealVault
from tests.test_mexc_execution_state import PASSWORD


def worker_case(tmp_path, cycle, **kwargs):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD,create=True)
    worker = MexcBotSupervisor(vault,object(),object(),object(),"fixture",deadline=101,
                                interval_seconds=0.1,cycle=cycle,**kwargs)
    return vault, worker


def test_expired_permission_stops_without_renewing_it_or_publishing_secrets(tmp_path):
    now, calls = {"value":100}, []

    def cycle(*_,assert_active,**__):
        assert_active()
        calls.append(True)
        now["value"] = 102
        return {"action":"WAIT","reason":"WAITING_ENTRY"}

    exited = Event()
    vault, worker = worker_case(tmp_path,cycle,monotonic=lambda:now["value"],on_exit=exited.set)
    worker.start()
    assert worker.join(2) and exited.is_set()
    assert len(calls) == 1 and worker.state()["status"] == "PERMISSION_REVOKED_OR_EXPIRED"
    assert worker._view is worker._trade is worker._paper is None
    vault.lock()


def test_stop_during_preflight_prevents_the_next_post_and_exits_cleanly(tmp_path):
    entered, proceed, posted = Event(), Event(), []

    def cycle(*_,assert_active,**__):
        entered.set()
        assert proceed.wait(2)
        assert_active()
        posted.append(True)
        return {"action":"BUY","reason":"ENTRY_RULE"}

    vault, worker = worker_case(tmp_path,cycle,monotonic=lambda:100)
    worker.start()
    assert entered.wait(2)
    worker.request_stop()
    proceed.set()
    assert worker.join(2) and posted == []
    assert worker.state()["stop_requested"] and worker.state()["running"] is False
    vault.lock()


def test_provider_exception_is_not_returned_in_worker_state(tmp_path):
    def cycle(*_,**__):
        raise RuntimeError("private fixture-secret should not be returned")

    vault, worker = worker_case(tmp_path,cycle,monotonic=lambda:100)
    worker.start()
    assert worker.join(2)
    assert worker.state()["status"] == "ERROR_NEEDS_REVIEW"
    assert "fixture-secret" not in str(worker.state())
    vault.lock()
