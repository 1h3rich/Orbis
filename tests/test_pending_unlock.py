"""Manual lock and the latest authentication intent beat older in-flight work."""
from threading import Event, Thread, current_thread

import pytest
import sqlcipher3

from app.automation.access import AutomationAccess
from app.automation.spot_paper_access import SpotPaperAccess
from app.derivatives.paper_access import PaperPerpAccess
from app.lab.access import LabAccess


PASSWORD = 'owned pending unlock test password'
FACTORIES = [LabAccess, AutomationAccess, SpotPaperAccess, PaperPerpAccess]


def held_verification(monkeypatch, barriers):
    original = sqlcipher3.connect

    class Connection:
        def __init__(self, *args, **kwargs):
            self.inner = original(*args, **kwargs)

        def execute(self, sql, *args, **kwargs):
            barrier = barriers.get(current_thread().name)
            if barrier and (sql.startswith('SELECT count(*)') or sql == 'PRAGMA quick_check'):
                entered, release = barrier
                entered.set()
                assert release.wait(15), 'Owned authentication barrier not released'
            return self.inner.execute(sql, *args, **kwargs)

        def close(self):
            self.inner.close()

    monkeypatch.setattr(sqlcipher3, 'connect', Connection)


@pytest.mark.parametrize('factory', FACTORIES)
@pytest.mark.parametrize('action', ['LOCK', 'NEWER_UNLOCK', 'LOCK_THEN_UNLOCK', 'BAD_NEWER_UNLOCK'])
def test_pending_authentication_never_undoes_lock_or_newer_intent(tmp_path, monkeypatch, factory, action):
    path = tmp_path / 'owned.db'
    access = factory()
    access.create(path, PASSWORD)
    access.lock(path)
    entered, release = Event(), Event()
    held_verification(monkeypatch, {'old-auth': (entered, release)})
    errors = []

    def old_auth():
        try:
            access.unlock(path, PASSWORD, session_seconds=604800)
        except ValueError as exc:
            errors.append(str(exc))

    worker = Thread(target=old_auth, name='old-auth')
    worker.start()
    try:
        assert entered.wait(15)
        if action in ('LOCK', 'LOCK_THEN_UNLOCK'):
            assert not access.lock(path)['unlocked']
        if action in ('NEWER_UNLOCK', 'LOCK_THEN_UNLOCK'):
            assert access.unlock(path, PASSWORD)['session_seconds'] == 900
        elif action == 'BAD_NEWER_UNLOCK':
            with pytest.raises(ValueError, match='contraseña o formato'):
                access.unlock(path, 'wrong owned pending password')
        epoch = access.epoch(path) if hasattr(access, 'epoch') else None
        release.set()
        worker.join(15)
        assert not worker.is_alive() and len(errors) == 1 and 'cancelado' in errors[0]
        expected_unlocked = action in ('NEWER_UNLOCK', 'LOCK_THEN_UNLOCK')
        status = access.status(path)
        assert status['unlocked'] is expected_unlocked
        assert status['session_seconds'] == (900 if expected_unlocked else 0)
        assert not access._unlock_attempts
        if hasattr(access, 'epoch'):
            assert access.epoch(path) is epoch
        assert path.read_bytes()[:16] != b'SQLite format 3\x00'
    finally:
        release.set()
        worker.join(15)
        access.lock(path)


@pytest.mark.parametrize('factory', FACTORIES)
def test_old_finally_cannot_cancel_newer_authentication_still_being_verified(tmp_path, monkeypatch, factory):
    path = tmp_path / 'owned.db'
    access = factory()
    access.create(path, PASSWORD)
    access.lock(path)
    first_entered, first_release, next_entered, next_release = (Event() for _ in range(4))
    held_verification(monkeypatch, {'first': (first_entered, first_release), 'next': (next_entered, next_release)})
    errors = []

    def authenticate(duration):
        try:
            access.unlock(path, PASSWORD, session_seconds=duration)
        except ValueError as exc:
            errors.append(str(exc))

    first = Thread(target=authenticate, args=(604800,), name='first')
    next_worker = Thread(target=authenticate, args=(3600,), name='next')
    first.start()
    try:
        assert first_entered.wait(15)
        next_worker.start()
        assert next_entered.wait(15)
        first_release.set()
        first.join(15)
        assert not first.is_alive() and len(errors) == 1 and 'cancelado' in errors[0]
        assert not access.status(path)['unlocked'] and len(access._unlock_attempts) == 1
        next_release.set()
        next_worker.join(15)
        assert not next_worker.is_alive() and len(errors) == 1
        assert access.status(path)['session_seconds'] == 3600
        assert not access._unlock_attempts
    finally:
        first_release.set()
        next_release.set()
        first.join(15)
        if next_worker.ident is not None:
            next_worker.join(15)
        access.lock(path)
