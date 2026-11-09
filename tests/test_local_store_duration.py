"""Long-lived research/PAPER keys, with independent stores and fixed expiry."""
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.access import AutomationAccess, automation_access
from app.automation.api import create_automation_router
from app.automation.spot_paper_access import SpotPaperAccess, spot_paper_access
from app.automation.spot_strategy_paper import create_spot_strategy_router
from app.lab.access import LabAccess, lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.security.local_session import MAX_SESSION_SECONDS


PASSWORD = 'owned local store duration password'
WEEK = 7 * 86400


@pytest.mark.parametrize('factory', [LabAccess, AutomationAccess, SpotPaperAccess])
def test_long_key_fixed_expiry_lock_default_and_independent_files(tmp_path, factory):
    clock = [0.0]
    access = factory(clock=lambda: clock[0])
    first, second = tmp_path / 'short.db', tmp_path / 'long.db'
    assert access.create(first, PASSWORD)['session_seconds'] == 900
    assert access.create(second, PASSWORD, session_seconds=WEEK)['session_seconds'] == WEEK
    clock[0] = 901
    assert access.get(first) is None and access.get(second) == PASSWORD
    assert access.status(second)['remaining_seconds'] == WEEK - 901
    assert not factory().status(second)['unlocked']  # A fresh process has no key.
    access.lock(second)
    assert access.get(second) is None
    assert access.unlock(second, PASSWORD)['session_seconds'] == 900
    assert access.unlock(second, PASSWORD, session_seconds=WEEK)['session_seconds'] == WEEK
    for stamp in (WEEK, WEEK + 900):
        clock[0] = stamp
        assert access.get(second) == PASSWORD
        assert access.status(second)['remaining_seconds'] == WEEK + 901 - stamp
    clock[0] = WEEK + 901
    assert access.get(second) is None and not access.status(second)['unlocked']
    assert first.read_bytes()[:16] != b'SQLite format 3\x00'
    assert second.read_bytes()[:16] != b'SQLite format 3\x00'


@pytest.mark.parametrize('duration', [True, False, 0, -1, '900', 900.0, MAX_SESSION_SECONDS + 1])
@pytest.mark.parametrize('factory', [LabAccess, AutomationAccess, SpotPaperAccess])
def test_bad_internal_duration_never_creates_storage(tmp_path, factory, duration):
    path = tmp_path / 'paper.db'
    with pytest.raises(ValueError, match='plazo'):
        factory().create(path, PASSWORD, session_seconds=duration)
    assert not path.exists()


@pytest.mark.parametrize('target', ['lab', 'dca', 'spot'])
def test_api_duration_strict_local_and_explicit_on_every_unlock(tmp_path, target):
    path = tmp_path / 'store.db'
    app = FastAPI()
    if target == 'lab':
        router, prefix, access = create_lab_router(path), '/lab', lab_access
    elif target == 'dca':
        router, prefix, access = create_automation_router(path, tmp_path / 'lab.db'), '/automation/dca', automation_access
    else:
        router, prefix, access = create_spot_strategy_router(path, tmp_path / 'lab.db'), '/paper/spot/strategies', spot_paper_access
    app.include_router(router)
    try:
        with TestClient(app) as client:
            for duration in (True, 899, '900', 900.0, None, MAX_SESSION_SECONDS + 1):
                for action in ('create', 'unlock'):
                    response = client.post(prefix + '/' + action, json={'passphrase': PASSWORD, 'session_seconds': duration})
                    assert response.status_code == 422
                assert not path.exists()
            payload = {'passphrase': PASSWORD, 'session_seconds': MAX_SESSION_SECONDS}
            assert client.post(prefix + '/create', json=payload, headers={'Origin': 'https://outside.invalid'}).status_code == 403
            assert not path.exists()
            made = client.post(prefix + '/create', json=payload)
            assert made.status_code == 200, made.text
            assert made.json()['session_seconds'] == MAX_SESSION_SECONDS
            assert made.headers['cache-control'] == 'no-store' and PASSWORD not in made.text
            assert not client.post(prefix + '/lock').json()['unlocked']
            opened = client.post(prefix + '/unlock', json={'passphrase': PASSWORD})
            assert opened.status_code == 200 and opened.json()['session_seconds'] == 900
            assert not (tmp_path / 'real.db').exists()
    finally:
        access.lock(path)


@pytest.mark.parametrize('lab_duration,dca_duration', [(WEEK, WEEK), (900, WEEK), (WEEK, 900)])
def test_dca_next_day_requires_both_selected_keys_and_preserves_money_when_locked(tmp_path, monkeypatch, lab_duration, dca_duration):
    from app.automation.migration import encrypt_active_automation
    from app.automation.paper_worker import PaperDcaWorker
    from app.lab.migration import encrypt_active_lab
    from tests.test_paper_dca import harness
    paper, _, state, store, lab, version, dataset, _ = harness(tmp_path)
    session = paper.create(version['version_id'], dataset['id'])
    encrypt_active_lab(lab, 'CIFRAR LAB', PASSWORD)
    encrypt_active_automation(store, 'CIFRAR DCA', PASSWORD)
    clock = [0.0]
    monkeypatch.setattr(lab_access, '_clock', lambda: clock[0])
    monkeypatch.setattr(automation_access, '_clock', lambda: clock[0])
    lab_access.unlock(lab.path, PASSWORD, session_seconds=lab_duration)
    automation_access.unlock(store.path, PASSWORD, session_seconds=dca_duration)
    try:
        before = paper.detail(session['id'])
        execution = session['next_exec_t'] + 1
        clock[0] = execution - state['now']
        assert clock[0] > 900
        state['now'] = execution
        worker = PaperDcaWorker(paper)
        if dca_duration == 900:
            with pytest.raises(LabError, match='no se pudo abrir'):
                worker.run_once()
            automation_access.unlock(store.path, PASSWORD, session_seconds=WEEK)
        else:
            result = worker.run_once()
            assert result[session['id']] == ('OBSERVED' if lab_duration == WEEK else 'RETRY')
        after = paper.detail(session['id'])
        if lab_duration == WEEK and dca_duration == WEEK:
            assert Decimal(after['state']['cash']) < Decimal(before['state']['cash'])
            assert Decimal(after['state']['units']) > 0 and after['events']
        else:
            assert after['state'] == before['state'] and after['events'] == before['events']
            assert state['calls'] == 0 and state['quote_calls'] == 0
        assert not (tmp_path / 'real.db').exists()
    finally:
        automation_access.lock(store.path)
        lab_access.lock(lab.path)


@pytest.mark.parametrize('duration', [900, WEEK])
def test_spot_next_hour_uses_long_key_and_expired_key_does_not_buy(tmp_path, monkeypatch, duration):
    from app.automation.spot_paper_migration import encrypt_active_spot_paper
    from app.automation.spot_strategy_paper import SpotPaperLocked
    from app.automation.spot_strategy_worker import SpotStrategyWorker
    from tests.test_spot_strategy_paper import START, STEP, harness
    paper, state, _, dataset, strategy = harness(tmp_path)
    session = paper.create(strategy['version_id'], dataset['id'])
    encrypt_active_spot_paper(paper, 'CIFRAR SPOT PAPER', PASSWORD)
    key_clock = [0.0]
    monkeypatch.setattr(spot_paper_access, '_clock', lambda: key_clock[0])
    spot_paper_access.unlock(paper.path, PASSWORD, session_seconds=duration)
    try:
        before = paper.detail(session['id'])
        next_time = START + 4 * STEP + 1
        key_clock[0] = next_time - state['now']
        assert key_clock[0] > 900
        state['now'] = next_time
        worker = SpotStrategyWorker(paper)
        if duration == 900:
            with pytest.raises(SpotPaperLocked):
                worker.run_once()
            spot_paper_access.unlock(paper.path, PASSWORD, session_seconds=WEEK)
            after = paper.detail(session['id'])
            assert after['state'] == before['state'] and after['events'] == before['events']
            assert state['candle_calls'] == 0 and state['quote_calls'] == 0
        else:
            assert worker.run_once()[session['id']] == 'OBSERVED'
            after = paper.detail(session['id'])
            assert Decimal(after['state']['units']) > 0 and after['events']
        assert not (tmp_path / 'real.db').exists()
    finally:
        spot_paper_access.lock(paper.path)
