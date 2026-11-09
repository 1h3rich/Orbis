"""Pinned DCA reserve persistence, cipher boundary, races and restoration."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation import api as automation_api, verification
from app.automation.access import automation_access
from app.automation.api import create_automation_router
from app.automation.dca import AVERAGE_ENGINE_VERSION, SMA_ENGINE_VERSION
from app.automation.migration import encrypt_active_automation, restore_secure_automation, verify_automation_database
from app.automation.store import AutomationStore
from app.automation.validation import VALIDATION_ENGINE_VERSION, chronological_dca_holdout
from app.automation.verification import verify_dca_validation
from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.store import LabStore, content_hash
from tests.test_dca_validation import DAY, START, bars, rule

PASSWORD = 'owned dca reserve API password'


@pytest.fixture
def prepared(tmp_path):
    path, lab_path = tmp_path / 'automation.db', tmp_path / 'lab.db'
    app = FastAPI()
    app.include_router(create_automation_router(path, lab_path))
    app.include_router(create_lab_router(lab_path))
    client = TestClient(app)
    assert client.post('/lab/create', json={'passphrase': PASSWORD}).status_code == 200
    assert client.post('/automation/dca/create', json={'passphrase': PASSWORD}).status_code == 200
    store, lab = AutomationStore(path), LabStore(lab_path)
    dataset = lab.save_dataset('BTC/USDT', '1d', 'mexc-spot-public', bars(), [], verified_public=True)
    version = client.post('/automation/dca/rules', json={'name': 'Reserva', 'definition': rule()}).json()
    yield client, store, lab, dataset, version
    automation_access.lock(path)
    lab_access.lock(lab_path)


def create(prepared):
    client, _, _, dataset, version = prepared
    return client.post('/automation/dca/validations', json={
        'dataset_id': dataset['id'], 'version_id': version['version_id']})


def _observed_reserve(prepared, *, gate=None):
    from app.automation.paper_dca import PaperDca, create_paper_dca_router

    client, store, lab, dataset, _ = prepared
    definition = rule(budget_total='1000', monthly_limit='300', max_exposure='1',
                      fee_rate='0', slippage_rate='0', dips=[], **(gate or {}))
    version = store.create_rule('PAPER y reserva', definition)
    origin = {'dataset_id': dataset['id'], 'version_id': version['version_id']}
    reserve = client.post('/automation/dca/validations', json=origin)
    assert reserve.is_success, reserve.text
    now = [START + 100 * DAY + 1]
    calls = []
    rows = [bar.json() for bar in bars(102)]

    class Feed:
        def get_candles(self, symbol, *, timeframe, limit, since, before):
            calls.append('candles')
            return [row for row in rows if since <= row['t'] < before][:limit]

        def get_paper_quote(self, symbol):
            calls.append('quote')
            return {'symbol': symbol, 'source': 'mexc-spot-public',
                    'event_time_ns': now[0] * 10**9, 'ask': '100', 'last': '100',
                    'bid': '99' if now[0] < START + 102 * DAY else '98.5'}

    paper = PaperDca(store.path, lab.path, adapter_factory=Feed, clock_ns=lambda: now[0] * 10**9)
    client.app.include_router(create_paper_dca_router(store.path, lab.path, paper=paper))
    session = paper.create(**origin)
    path = '/automation/dca/paper/sessions/' + session['id'] + '/reserve-comparison'
    return paper, session, reserve.json(), now, calls, path


def test_paper_dca_reserve_marks_cash_and_units_at_archived_bid_after_reopen(prepared):
    client, store, lab, _, _ = prepared
    paper, session, reserve, now, calls, path = _observed_reserve(prepared)
    params = {'validation_id': reserve['id']}
    before = client.get(path, params=params)
    assert before.is_success, before.text
    assert before.json()['paper']['observed_days'] == 0 and before.json()['paper']['mark_bid'] is None
    for offset in (0, DAY):
        now[0] = session['next_exec_t'] + offset + 1
        assert paper.poll(session['id'])['poll_result'] == 'OBSERVED'
    archived = paper.detail(session['id'])
    compared = client.get(path, params=params)
    assert compared.is_success, compared.text
    assert compared.headers['Cache-Control'] == 'no-store'
    report = compared.json()
    assert report['execution_authority'] == 'NONE'
    assert report['comparison_basis'] == 'DISTINCT_WINDOWS_AND_EXECUTION'
    assert report['reserve']['result_hash'] == reserve['result_hash']
    assert report['reserve']['end_t'] <= session['started_at_ns'] // 10**9
    assert report['reserve']['days'] == 30
    assert report['paper']['observed_days'] == 2 and report['paper']['mark_bid'] == '98.5'
    assert report['paper']['metrics'] == {
        'initial_capital':'1000','final_equity':'997.0','net_profit':'-3.0',
        'return_on_initial_capital_pct':'-0.300','remaining_cash':'800','invested':'200',
        'fees':'0','units':'2','max_drawdown_pct':'0.300','buys':2}
    assert paper.detail(session['id']) == archived
    assert calls == ['candles','quote'] * 2
    assert client.get(path, params=params, headers={'Origin':'https://outside.invalid'}).status_code == 403
    client.post('/automation/dca/lock')
    client.post('/lab/lock')
    assert client.get(path, params=params).status_code == 503
    assert client.post('/automation/dca/unlock', json={'passphrase': PASSWORD}).is_success
    assert client.post('/lab/unlock', json={'passphrase': PASSWORD}).is_success
    assert client.get(path, params=params).json() == report


@pytest.mark.parametrize('fault', ['event', 'quote', 'reserve', 'version', 'session-lock'])
def test_paper_dca_reserve_rejects_unverifiable_evidence(prepared, monkeypatch, fault):
    client, store, lab, _, _ = prepared
    gate = {'ma_adjustment':{'kind':'ema','period':3,'condition':'above',
                            'matched_factor':'2','unmatched_factor':'0.5'}}
    paper, session, reserve, now, calls, path = _observed_reserve(prepared, gate=gate)
    now[0] = session['next_exec_t'] + 1
    assert paper.poll(session['id'])['poll_result'] == 'OBSERVED'
    params = {'validation_id':reserve['id']}
    initial = client.get(path, params=params)
    assert initial.is_success, initial.text
    if fault == 'event':
        with store.connect() as db:
            row = db.execute('SELECT event_json FROM paper_dca_events WHERE session_id=? AND sequence_no=1',
                             (session['id'],)).fetchone()
            event = json.loads(row[0])
            event['requested'] = '999'
            db.execute('UPDATE paper_dca_events SET event_json=? WHERE session_id=? AND sequence_no=1',
                       (json.dumps(event), session['id']))
    elif fault == 'quote':
        with store.connect() as db:
            db.execute('UPDATE paper_dca_events SET quote_event_ns=quote_event_ns-1 WHERE session_id=?',
                       (session['id'],))
    elif fault == 'reserve':
        with store.connect() as db:
            row = db.execute('SELECT result_json FROM dca_validation_runs WHERE id=?', (reserve['id'],)).fetchone()
            payload = json.loads(row[0])
            payload['out_of_sample']['results']['conditional']['metrics']['final_equity'] = '999'
            payload['result_hash'] = content_hash({k:v for k,v in payload.items() if k not in ('definition_hash','result_hash')})
            db.execute('UPDATE dca_validation_runs SET result_json=? WHERE id=?', (json.dumps(payload), reserve['id']))
    elif fault == 'version':
        other = create(prepared)
        assert other.is_success
        params = {'validation_id':other.json()['id']}
    else:
        from app.automation import paper_dca
        replay = paper_dca.replay_paper_comparison

        def lock_during_replay(*args, **kwargs):
            result = replay(*args, **kwargs)
            lab_access.lock(lab.path)
            return result

        monkeypatch.setattr(paper_dca, 'replay_paper_comparison', lock_during_replay)
    refused = client.get(path, params=params)
    assert refused.status_code == (503 if fault == 'session-lock' else 422), refused.text
    assert 'paper' not in refused.json() and calls == ['candles','quote']


@pytest.mark.parametrize("engine,fields", [
    (SMA_ENGINE_VERSION, {'sma_gate': {'period': 3, 'condition': 'above'}}),
    (AVERAGE_ENGINE_VERSION, {'ma_adjustment': {'kind': 'ema', 'period': 3, 'condition': 'above',
        'matched_factor': '2', 'unmatched_factor': '0.5'}}),
])
def test_encrypted_sma_run_and_reserve_reopen_with_original_engine(prepared, engine, fields):
    client, store, lab, dataset, _ = prepared
    version = client.post('/automation/dca/rules', json={
        'name': 'Media móvil', 'definition': rule(**fields)}).json()
    inputs = {'version_id': version['version_id'], 'dataset_id': dataset['id']}
    run = client.post('/automation/dca/runs', json=inputs).json()
    reserve = client.post('/automation/dca/validations', json=inputs).json()
    assert run['engine_version'] == reserve['dca_engine_version'] == engine
    assert {key: run['rule'][key] for key in fields} == fields
    # The first reserve signal at the cut can already use the three known closes.
    first = reserve['out_of_sample']['results']['conditional']['events'][0]
    assert first['execution_time'] == reserve['test_start_t']
    if engine == SMA_ENGINE_VERSION:
        assert first['sma_value'] == '106'
    else:
        from app.automation.dca import seed_dca_ema
        full_prefix = bars()[:reserve['split_index']]
        expected = seed_dca_ema(run['rule'], (bar.c for bar in full_prefix))
        assert first['ma_value'] == str(expected.current)
        assert first['amount_factor'] == '2'
    store.add_version(version['id'], rule())
    client.post('/automation/dca/lock')
    client.post('/lab/lock')
    reopened = FastAPI()
    reopened.include_router(create_automation_router(store.path, lab.path))
    reopened.include_router(create_lab_router(lab.path))
    other = TestClient(reopened)
    assert other.post('/automation/dca/unlock', json={'passphrase': PASSWORD}).status_code == 200
    assert other.post('/lab/unlock', json={'passphrase': PASSWORD}).status_code == 200
    for family, artifact in (('runs', run), ('validations', reserve)):
        response = other.get(f'/automation/dca/{family}/{artifact["id"]}/verify')
        assert response.status_code == 200, response.text
        assert response.json()['verified']


def test_encrypted_reserve_old_version_summary_and_reopen(prepared):
    client, store, lab, dataset, version = prepared
    response = create(prepared)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['dataset_hash'] == dataset['hash']
    assert saved['definition_hash'] == content_hash(version['definition'])
    assert saved['engine_version'] == VALIDATION_ENGINE_VERSION
    assert not store.runs()
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM paper_dca_sessions').fetchone()[0] == 0
    store.add_version(version['id'], rule(amount='80'))
    endpoint = '/automation/dca/validations/' + saved['id']
    shown = client.get(endpoint + '?require_verified=true&view=summary')
    assert shown.status_code == 200, shown.text
    assert shown.json()['version_id'] == version['version_id']
    assert shown.json()['verification']['verified'] and all(shown.json()['verification']['checks'].values())
    assert 'events' not in shown.json()['out_of_sample']['results']['conditional']
    assert client.get('/automation/dca/validations').json()[0]['id'] == saved['id']
    assert store.path.read_bytes()[:16] != b'SQLite format 3\0'
    assert lab.path.read_bytes()[:16] != b'SQLite format 3\0'
    client.post('/automation/dca/lock')
    client.post('/lab/lock')
    assert client.get(endpoint).status_code == 423
    reopened = FastAPI()
    reopened.include_router(create_automation_router(store.path, lab.path))
    reopened.include_router(create_lab_router(lab.path))
    other = TestClient(reopened)
    assert other.post('/automation/dca/unlock', json={'passphrase': PASSWORD}).status_code == 200
    assert other.post('/lab/unlock', json={'passphrase': PASSWORD}).status_code == 200
    assert other.get(endpoint).json() == saved
    assert other.get(endpoint + '/verify').json()['verified']


@pytest.mark.parametrize('rehash', [False, True])
def test_changed_result_is_not_certified_even_with_updated_hash(prepared, rehash):
    client, store, _, _, _ = prepared
    saved = create(prepared).json()
    with store.connect() as db:
        payload = json.loads(db.execute('SELECT result_json FROM dca_validation_runs WHERE id=?', (saved['id'],)).fetchone()[0])
        payload['out_of_sample']['results']['conditional']['metrics']['net_profit'] = '99999'
        if rehash:
            payload['result_hash'] = content_hash({key: value for key, value in payload.items()
                                                if key not in ('definition_hash', 'result_hash')})
        db.execute('UPDATE dca_validation_runs SET result_json=? WHERE id=?', (json.dumps(payload), saved['id']))
    assert client.get('/automation/dca/validations/' + saved['id'] + '?require_verified=true').status_code == 409
    checked = client.get('/automation/dca/validations/' + saved['id'] + '/verify').json()
    assert not checked['verified']
    assert checked['checks']['replay' if rehash else 'stored_result'] is False


@pytest.mark.parametrize('change', ['version', 'dataset'])
def test_save_does_not_pin_inputs_changed_during_calculation(prepared, monkeypatch, change):
    _, store, lab, dataset, version = prepared
    original = automation_api.chronological_dca_holdout
    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'version':
            with store.connect() as db:
                db.execute('UPDATE dca_versions SET definition_json=? WHERE id=?',
                           (json.dumps(rule(amount='80')), version['version_id']))
        else:
            with lab.connect() as db:
                db.execute("UPDATE datasets SET source='changed' WHERE id=?", (dataset['id'],))
        return result
    monkeypatch.setattr(automation_api, 'chronological_dca_holdout', race)
    assert create(prepared).status_code == 422
    assert store.validations() == []


@pytest.mark.parametrize('change', ['version', 'dataset', 'saved'])
def test_verified_view_rejects_changes_during_replay(prepared, monkeypatch, change):
    client, store, lab, dataset, version = prepared
    saved = create(prepared).json()
    original = verification.chronological_dca_holdout
    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'version':
            with store.connect() as db:
                db.execute('UPDATE dca_versions SET definition_json=? WHERE id=?',
                           (json.dumps(rule(amount='80')), version['version_id']))
        elif change == 'dataset':
            with lab.connect() as db:
                db.execute("UPDATE datasets SET source='changed' WHERE id=?", (dataset['id'],))
        else:
            with store.connect() as db:
                db.execute("UPDATE dca_validation_runs SET created_at='changed' WHERE id=?", (saved['id'],))
        return result
    monkeypatch.setattr(verification, 'chronological_dca_holdout', race)
    assert client.get('/automation/dca/validations/' + saved['id'] + '?require_verified=true').status_code == 409


def test_reserve_respects_local_boundary_and_requires_saved_definition(prepared):
    client, _, _, dataset, version = prepared
    body = {'dataset_id': dataset['id'], 'version_id': version['version_id']}
    assert client.post('/automation/dca/validations', json=body,
                       headers={'origin': 'https://outside.invalid'}).status_code == 403
    assert client.post('/automation/dca/validations', json={**body, 'leverage': 5}).status_code == 422
    assert client.post('/automation/dca/validations', json={**body, 'train_fraction': '0.9'}).status_code == 422
    assert client.post('/automation/dca/validations', json={**body, 'version_id': 'missing'}).status_code == 422


def test_migration_cipher_backup_and_restore_include_reserve(tmp_path):
    store, lab = AutomationStore(tmp_path / 'automation.db'), LabStore(tmp_path / 'lab.db')
    dataset = lab.save_dataset('BTC/USDT', '1d', 'fixture', bars(), [])
    version = store.create_rule('Legacy plan', rule())
    # Emulate a genuine v5 store, preserving its original rule/version bytes.
    with store.connect() as db:
        db.execute('DROP TABLE dca_validation_runs')
        db.execute('PRAGMA user_version=5')
    result = chronological_dca_holdout(bars(), version['definition'], '1d')
    saved = store.save_validation(version['version_id'], dataset['id'], dataset['hash'],
        result, VALIDATION_ENGINE_VERSION, lab_path=lab.path,
        expected_definition_hash=content_hash(version['definition']))
    assert store.version(version['version_id'])['definition'] == version['definition']
    migrated = encrypt_active_automation(store, 'CIFRAR DCA', PASSWORD)
    encrypted = AutomationStore(store.path, passphrase=PASSWORD)
    assert verify_automation_database(store.path, PASSWORD)['counts']['dca_validation_runs'] == 1
    assert verify_dca_validation(encrypted, lab, encrypted.validation(saved['id']))['verified']
    encrypted.add_version(version['id'], rule(amount='80'))
    restore_secure_automation(store, migrated['backup'], 'RESTAURAR DCA', PASSWORD)
    restored = AutomationStore(store.path, passphrase=PASSWORD)
    assert restored.validation(saved['id']) == saved
    assert len(restored.versions(version['id'])) == 1
    assert verify_dca_validation(restored, lab, saved)['verified']
