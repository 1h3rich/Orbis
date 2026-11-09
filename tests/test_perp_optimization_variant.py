"""The selected derivative variant uses the exact optimizer executions."""
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.lab import perp_optimization_store as archive
from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments
from app.lab.schema import CURRENT_SCHEMA_VERSION
from app.lab.secure_backup import create_secure_lab_backup, verify_secure_lab_backup
from app.lab.store import LabStore, content_hash
from tests.test_perp_optimization import PLAN
from tests.test_perp_validation_store import PASSWORD, seeded


def make_grid(tmp_path, monkeypatch, *, ephemeral=False):
    lab, _, data, profile = seeded(tmp_path, monkeypatch, ephemeral=ephemeral)
    store = archive.PerpOptimizations(lab)
    return lab, store, data, profile, store.create(data['id'], profile['id'], PLAN)


def test_lab14_variant_api_keeps_source_and_exact_archived_runs(tmp_path, monkeypatch):
    lab, store, data, profile, saved = make_grid(tmp_path, monkeypatch, ephemeral=True)
    identifier, digest = (saved['optimization'][key] for key in ('id', 'hash'))
    source_id = profile['profile']['strategy_version_id']
    source = lab.version(source_id)
    # Another edit can exist after the original. Materialize from the exact
    # optimizer input and allocate the next version within the same strategy.
    lab.add_version(source['strategy_id'], {**source['definition'], 'fee_rate':'0.001'})
    with lab.connect() as db:
        db.execute('DROP TABLE perp_optimization_variants')
        db.execute('PRAGMA user_version=14')
    lab_access.lock(lab.path)
    lab_access.unlock(lab.path, PASSWORD)
    assert store.read(identifier) == saved
    api = FastAPI()
    api.include_router(create_lab_router(lab.path))
    endpoint = '/lab/perp/optimizations/' + identifier
    with TestClient(api) as client:
        assert client.post(endpoint+'/variant', json={'expected_hash':digest},
                           headers={'Origin':'https://outside.invalid'}).status_code == 403
        assert client.post(endpoint+'/variant', json={'expected_hash':'0'*64}).status_code == 422
        assert client.post(endpoint+'/variant', json={'expected_hash':True}).status_code == 422
        response = client.post(endpoint+'/variant', json={'expected_hash':digest})
        assert response.status_code == 201, response.text
        assert response.headers['Cache-Control'] == 'no-store'
        variant = response.json()
        assert variant == client.post(endpoint+'/variant', json={'expected_hash':digest}).json()
        assert variant['version'] == 3 and variant['strategy_id'] == source['strategy_id']
        assert variant['execution_authority'] == 'NONE'
        assert client.get(endpoint).json() == {**saved, 'variant':variant}
        assert client.get(endpoint+'/export').json()['variant'] == variant
        assert store.create(data['id'], profile['id'], PLAN) == {**saved, 'variant':variant}
        result = saved['result']
        selected = result['candidates'][result['selected_index']]
        for key, score, window in (
                ('development_run_id', selected['development'], (0, 60)),
                ('reserve_run_id', result['out_of_sample'], (60, 120))):
            run = client.get('/lab/perp/runs/'+variant[key]).json()
            execution = run['execution']['result']
            assert tuple(execution['window'][key] for key in ('entry_start_index','end_index')) == window
            assert content_hash(execution) == score['result_hash']
            assert execution['trades'] and not execution['real_authority'] and not execution['paper_handoff_available']
            assert run['run']['profile']['strategy_version_id'] == variant['version_id']
        assert lab.version(source_id) == source
        definition = lab.version(variant['version_id'])['definition']
        assert definition == {**source['definition'], 'stop_loss':selected['stop_loss'],
                              'take_profit':selected['take_profit']}
        backup = create_secure_lab_backup(lab, 'owned strategy variant backup key')
        report = verify_secure_lab_backup(tmp_path/'lab-backups'/backup['name'], 'owned strategy variant backup key')
        assert report['schema_version'] == CURRENT_SCHEMA_VERSION
        assert report['counts']['perp_optimization_variants'] == 1
        lab_access.lock(lab.path)
        assert client.post(endpoint+'/variant', json={'expected_hash':digest}).status_code == 423
        lab_access.unlock(lab.path, PASSWORD)
        assert archive.PerpOptimizations(LabStore(lab.path)).read(identifier)['variant'] == variant
        lab_access.lock(lab.path)


def test_variant_is_atomic_across_two_runs_and_concurrent_retries(tmp_path, monkeypatch):
    lab, store, _, _, saved = make_grid(tmp_path, monkeypatch)
    identifier, digest = (saved['optimization'][key] for key in ('id', 'hash'))
    original = archive.write_run
    calls = []

    def fail_second(*args):
        calls.append(1)
        if len(calls) == 2:
            raise LabError('owned failure before reserve commit')
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(archive, 'write_run', fail_second)
        with pytest.raises(LabError, match='owned failure'):
            store.materialize(identifier, digest)
    with lab.connect() as db:
        assert db.execute('SELECT count(*) FROM strategy_versions').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM perp_profiles').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM perp_runs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM perp_optimization_variants').fetchone()[0] == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.materialize, identifier, digest) for _ in range(2)]
        first, second = [future.result(timeout=20) for future in futures]
    assert first == second
    with lab.connect() as db:
        assert db.execute('SELECT count(*) FROM strategy_versions').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM perp_profiles').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM perp_runs').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM perp_optimization_variants').fetchone()[0] == 1


def test_variant_refuses_eligibility_failure_and_old_session(tmp_path, monkeypatch):
    lab, store, data, profile, saved = make_grid(tmp_path, monkeypatch, ephemeral=True)
    empty = store.create(data['id'], profile['id'], archive.PerpOptimizationPlan(
        stop_losses=['0.5'], take_profits=['0.5']))['optimization']
    with pytest.raises(LabError, match='ninguna'):
        store.materialize(empty['id'], empty['hash'])
    original = archive.replay_perp_optimization

    def switch_session(*args):
        result = original(*args)
        lab_access.lock(lab.path)
        lab_access.unlock(lab.path, PASSWORD)
        return result

    monkeypatch.setattr(archive, 'replay_perp_optimization', switch_session)
    with pytest.raises(LabError, match='sesión LAB cambió'):
        store.materialize(saved['optimization']['id'], saved['optimization']['hash'])
    with lab.connect() as db:
        assert db.execute('SELECT count(*) FROM perp_optimization_variants').fetchone()[0] == 0
    lab_access.lock(lab.path)


def test_valid_backtest_from_wrong_window_cannot_replace_linked_reserve(tmp_path, monkeypatch):
    lab, store, data, _, saved = make_grid(tmp_path, monkeypatch)
    identifier, digest = (saved['optimization'][key] for key in ('id', 'hash'))
    variant = store.materialize(identifier, digest)
    other = PerpExperiments(lab).run(data['id'], variant['profile_id'])
    with lab.connect() as db:
        db.execute('UPDATE perp_optimization_variants SET reserve_run_id=? WHERE optimization_id=?',
                   (other['id'], identifier))
    with pytest.raises(LabError, match='tramos'):
        store.read(identifier)
    with pytest.raises(LabError, match='tramos'):
        store.materialize(identifier, digest)
    with pytest.raises(LabError, match='tramos'):
        create_secure_lab_backup(lab, 'owned strategy variant backup key')
