"""Explicit, fixed PAPER key lifetimes; own encrypted files and controlled clocks."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.paper_access import MAX_SESSION_SECONDS, PaperPerpAccess
from app.derivatives.paper_api import create_paper_perp_router
from app.derivatives.paper_math import PaperPerpError


PASSWORD = "owned PAPER duration password"


def test_default_expires_at_fifteen_minutes_without_renewal(tmp_path):
    clock = [10.0]
    access = PaperPerpAccess(clock=lambda: clock[0])
    path = tmp_path / 'paper.db'
    created = access.create(path, PASSWORD)
    assert created['session_seconds'] == 900 and created['remaining_seconds'] == 900
    original = access.epoch(path)
    for elapsed in (1, 300, 899):
        clock[0] = 10 + elapsed
        assert access.get(path) == PASSWORD and access.epoch(path) is original
        assert access.status(path)['remaining_seconds'] == 900 - elapsed
    clock[0] = 910
    assert access.get(path) is None and access.epoch(path) is None
    assert access.status(path)['session_seconds'] == 0


@pytest.mark.parametrize('duration', [3600, 14400, 86400, 604800, MAX_SESSION_SECONDS])
def test_selected_duration_survives_fifteen_minutes_but_expires_exactly(tmp_path, duration):
    clock = [0.0]
    access = PaperPerpAccess(clock=lambda: clock[0])
    path = tmp_path / 'paper.db'
    access.create(path, PASSWORD, session_seconds=duration)
    clock[0] = 901
    assert access.get(path) == PASSWORD
    assert access.status(path)['remaining_seconds'] == duration - 901
    clock[0] = duration - 1
    assert access.get(path) == PASSWORD
    assert access.status(path)['session_seconds'] == duration
    clock[0] = duration
    assert not access.status(path)['unlocked'] and access.get(path) is None
    assert path.read_bytes()[:16] != b'SQLite format 3\x00'


def test_lock_reauthentication_failure_and_default_never_extend_old_authority(tmp_path):
    clock = [0.0]
    path = tmp_path / 'paper.db'
    access = PaperPerpAccess(clock=lambda: clock[0])
    access.create(path, PASSWORD, session_seconds=MAX_SESSION_SECONDS)
    old = access.epoch(path)
    clock[0] = 1000
    with pytest.raises(PaperPerpError, match='contraseña o formato'):
        access.unlock(path, 'wrong owned password', session_seconds=MAX_SESSION_SECONDS)
    assert access.epoch(path) is old
    assert access.status(path)['remaining_seconds'] == MAX_SESSION_SECONDS - 1000
    access.lock(path)
    with pytest.raises(PaperPerpError, match='sesión GRVT PAPER cambió'):
        with access.guard(path, old):
            pytest.fail('The old epoch must be revoked')
    # No saved long-lived policy: an omitted duration always chooses the default.
    assert access.unlock(path, PASSWORD)['session_seconds'] == 900
    assert access.epoch(path) is not old
    cold = PaperPerpAccess(clock=lambda: clock[0])
    assert not cold.status(path)['unlocked'] and cold.epoch(path) is None
    assert cold.unlock(path, PASSWORD)['remaining_seconds'] == 900


@pytest.mark.parametrize('duration', [True, False, 0, -1, 900.0, '900', MAX_SESSION_SECONDS + 1])
def test_invalid_duration_does_not_create_or_change_access(tmp_path, duration):
    access = PaperPerpAccess()
    path = tmp_path / 'paper.db'
    with pytest.raises(PaperPerpError, match='plazo'):
        access.create(path, PASSWORD, session_seconds=duration)
    assert not path.exists() and access.epoch(path) is None


@pytest.mark.parametrize('duration', [True, False, 0, 899, 900.0, '900', None, MAX_SESSION_SECONDS + 1])
def test_http_rejects_unbounded_or_coerced_duration_without_creation(tmp_path, duration):
    path = tmp_path / 'paper.db'
    app = FastAPI()
    app.include_router(create_paper_perp_router(path))
    with TestClient(app) as client:
        for action in ('create', 'unlock'):
            response = client.post('/paper/perp/' + action,
                                   json={'passphrase': PASSWORD, 'session_seconds': duration})
            assert response.status_code == 422
        assert not path.exists()


def test_http_explicit_duration_is_local_not_persisted_and_not_real_permission(tmp_path):
    from app.derivatives.paper_access import paper_perp_access
    path = tmp_path / 'paper.db'
    calls = []
    app = FastAPI()
    app.include_router(create_paper_perp_router(path, on_access_change=lambda: calls.append(True)))
    try:
        with TestClient(app) as client:
            payload = {'passphrase': PASSWORD, 'session_seconds': MAX_SESSION_SECONDS}
            blocked = client.post('/paper/perp/create', json=payload,
                                  headers={'Origin': 'https://outside.invalid'})
            assert blocked.status_code == 403 and not path.exists() and not calls
            made = client.post('/paper/perp/create', json=payload)
            assert made.status_code == 200 and made.headers['cache-control'] == 'no-store'
            assert made.json()['session_seconds'] == MAX_SESSION_SECONDS and len(calls) == 1
            epoch = paper_perp_access.epoch(path)
            client.post('/paper/perp/lock').raise_for_status()
            assert paper_perp_access.get(path) is None
            opened = client.post('/paper/perp/unlock', json={'passphrase': PASSWORD})
            assert opened.json()['session_seconds'] == 900
            assert paper_perp_access.epoch(path) is not epoch
            long = client.post('/paper/perp/unlock', json={**payload, 'session_seconds': 14400})
            assert long.json()['session_seconds'] == 14400 and len(calls) == 4
            assert not (tmp_path / 'real.db').exists()
            assert PASSWORD not in long.text
    finally:
        paper_perp_access.lock(path)
