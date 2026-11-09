"""El trabajador de parada sólo corre bajo arranque explícito y sin red real."""

from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from app.live_execution.journal import stop_spot_account
from app.live_execution.mexc_live_cancel import MexcStoppedSupervisionStep
from app.live_execution.mexc_stop_supervisor import MexcStopSupervisor, MexcStopSupervisorError
from app.portfolio.vault import RealVault


ACCOUNT = "a" * 64
PASSWORD = "supervisor isolated password"


class View:
    def __init__(self, account_ref=ACCOUNT):
        self.account_ref = account_ref

    def account_reference(self):
        return self.account_ref


def vault_at(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    return vault


def test_supervisor_requires_stop_vault_and_exact_view_reference(tmp_path):
    vault = vault_at(tmp_path)
    cycle = lambda *_args: MexcStoppedSupervisionStep("NO_ACTIONABLE_OWN_ORDER", None, 0)
    worker = MexcStopSupervisor(vault, object(), View(), ACCOUNT, cycle=cycle)
    with pytest.raises(MexcStopSupervisorError, match="no está detenida"):
        worker.start()
    stop_spot_account(vault, ACCOUNT, now=datetime.now(timezone.utc))
    wrong = MexcStopSupervisor(vault, object(), View("b" * 64), ACCOUNT, cycle=cycle)
    with pytest.raises(MexcStopSupervisorError, match="no pertenece"):
        wrong.start()
    vault.lock()
    with pytest.raises(MexcStopSupervisorError, match="bloqueada"):
        worker.start()


def test_supervisor_stops_cooperatively_and_never_runs_second_cycle(tmp_path):
    vault = vault_at(tmp_path)
    stop_spot_account(vault, ACCOUNT, now=datetime.now(timezone.utc))
    entered, release = Event(), Event()
    calls = []

    def cycle(_vault, _trade, _view, account_ref):
        calls.append(account_ref)
        entered.set()
        assert release.wait(2)
        return MexcStoppedSupervisionStep("NO_ACTIONABLE_OWN_ORDER", None, 1)

    worker = MexcStopSupervisor(vault, object(), View(), ACCOUNT,
                                interval_seconds=0.25, cycle=cycle)
    worker.start()
    assert entered.wait(2)
    assert worker.state().running is True
    worker.request_stop()
    assert worker.state().stop_requested is True
    assert worker.join(0.01) is False
    release.set()
    assert worker.join(2) is True
    state = worker.state()
    assert state.running is False and state.status == "STOPPED"
    assert state.cycles == 1 and calls == [ACCOUNT]
    with pytest.raises(MexcStopSupervisorError, match="ya fue iniciado"):
        worker.start()
    vault.lock()


def test_supervisor_error_is_redacted_and_does_not_retry(tmp_path):
    vault = vault_at(tmp_path)
    stop_spot_account(vault, ACCOUNT, now=datetime.now(timezone.utc))
    calls = []

    def cycle(*_args):
        calls.append(True)
        raise RuntimeError("private upstream fixture-secret")

    worker = MexcStopSupervisor(vault, object(), View(), ACCOUNT,
                                interval_seconds=0.25, cycle=cycle)
    worker.start()
    assert worker.join(2) is True
    state = worker.state()
    assert state.status == "ERROR_NEEDS_REVIEW" and state.cycles == 0
    assert calls == [True] and "fixture-secret" not in repr(state)
    vault.lock()


def test_stalled_cycle_is_reported_and_never_retried(tmp_path):
    vault = vault_at(tmp_path)
    stop_spot_account(vault, ACCOUNT, now=datetime.now(timezone.utc))
    entered, release = Event(), Event()
    now = [0.0]
    calls = []

    def cycle(*_args):
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return MexcStoppedSupervisionStep("NO_ACTIONABLE_OWN_ORDER", None, 1)

    worker = MexcStopSupervisor(vault, object(), View(), ACCOUNT,
                                interval_seconds=0.25, max_silence_seconds=10,
                                clock=lambda: now[0], cycle=cycle)
    worker.start()
    assert entered.wait(2)
    assert worker.state().stalled is False
    now[0] = 11.0
    assert worker.state().running is True
    assert worker.state().stalled is True
    release.set()
    assert worker.join(2) is True
    state = worker.state()
    assert state.running is False
    assert state.status == "STALLED_NEEDS_REVIEW"
    assert state.stop_requested is True
    assert state.cycles == 1 and calls == [True]
    vault.lock()


def test_supervisor_exits_after_vault_lock_without_another_cycle(tmp_path):
    vault = vault_at(tmp_path)
    stop_spot_account(vault, ACCOUNT, now=datetime.now(timezone.utc))
    first = Event()
    calls = []

    def cycle(*_args):
        calls.append(True)
        first.set()
        return MexcStoppedSupervisionStep("NO_ACTIONABLE_OWN_ORDER", None, 0)

    worker = MexcStopSupervisor(vault, object(), View(), ACCOUNT,
                                interval_seconds=0.5, cycle=cycle)
    worker.start()
    assert first.wait(2)
    vault.lock()
    assert worker.join(2) is True
    assert worker.state().status == "VAULT_LOCKED"
    assert calls == [True]


def test_supervisor_rejects_fast_lock_and_reunlock_of_same_vault(tmp_path):
    vault = vault_at(tmp_path)
    stop_spot_account(vault, ACCOUNT, now=datetime.now(timezone.utc))
    first = Event()
    calls = []

    def cycle(*_args):
        calls.append(True)
        first.set()
        return MexcStoppedSupervisionStep("NO_ACTIONABLE_OWN_ORDER", None, 0)

    worker = MexcStopSupervisor(vault, object(), View(), ACCOUNT,
                                interval_seconds=0.5, cycle=cycle)
    worker.start()
    assert first.wait(2)
    vault.lock()
    vault.unlock(PASSWORD)
    assert worker.join(2) is True
    assert worker.state().status == "VAULT_SESSION_REPLACED"
    assert calls == [True]
    vault.lock()


def test_vault_lock_waits_for_inflight_supervision_cycle(tmp_path):
    vault = vault_at(tmp_path)
    stop_spot_account(vault, ACCOUNT, now=datetime.now(timezone.utc))
    entered, release, locking, locked = Event(), Event(), Event(), Event()

    def cycle(*_args):
        entered.set()
        assert release.wait(2)
        return MexcStoppedSupervisionStep("NO_ACTIONABLE_OWN_ORDER", None, 0)

    worker = MexcStopSupervisor(vault, object(), View(), ACCOUNT,
                                interval_seconds=0.5, cycle=cycle)
    worker.start()
    assert entered.wait(2)

    def lock_vault():
        locking.set()
        vault.lock()
        locked.set()

    locker = Thread(target=lock_vault)
    locker.start()
    assert locking.wait(2)
    assert not locked.wait(0.05)
    release.set()
    locker.join(2)
    assert locked.is_set()
    assert worker.join(2) is True
    assert worker.state().cycles == 1
