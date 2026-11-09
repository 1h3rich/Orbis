import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab import perp_optimization_store as archive
from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.lab.schema import CURRENT_SCHEMA_VERSION
from app.lab.secure_backup import create_secure_lab_backup, verify_secure_lab_backup
from app.lab.store import LabStore, content_hash, encoded
from tests.test_perp_optimization import PLAN
from tests.test_perp_validation_store import HOLDOUT, PASSWORD, seeded


def test_encrypted_migration_api_reopen_and_backup(tmp_path, monkeypatch):
    lab, validations, data, profile = seeded(tmp_path, monkeypatch, ephemeral=True)
    old = validations.create(data['id'], profile['id'], HOLDOUT)
    old_bundle = validations.read(old['id'])
    with lab.connect() as db:
        db.execute('DROP TABLE perp_optimization_variants')
        db.execute('DROP TABLE perp_optimizations')
        db.execute('PRAGMA user_version=13')
    lab_access.lock(lab.path)
    lab_access.unlock(lab.path, PASSWORD)
    api = FastAPI()
    api.include_router(create_lab_router(lab.path))
    endpoint = '/lab/perp/optimizations'
    body = {'dataset_id': data['id'], 'profile_id': profile['id'], 'plan': PLAN.payload()}
    with TestClient(api) as client:
        assert client.post(endpoint, json=body, headers={'Origin':'https://external.invalid'}).status_code == 403
        assert client.post(endpoint, json={**body,'plan':{**body['plan'],'stop_losses':[0.1]}}).status_code == 422
        made = client.post(endpoint, json=body)
        assert made.status_code == 201, made.text
        saved = made.json()
        assert saved == client.post(endpoint, json=body).json()
        assert saved['result']['selected_index'] is not None
        path = endpoint + '/' + saved['optimization']['id']
        assert client.get(path).json() == saved
        assert client.get(endpoint+'?limit=1').json()['items'] == [saved['optimization']]
        assert client.get(endpoint+'?offset=1').json()['items'] == []
        download = client.get(path+'/export')
        assert download.headers['Cache-Control'] == 'no-store'
        assert 'attachment;' in download.headers['Content-Disposition']
        bundle = download.json()
        assert bundle['result'] == saved['result']
        assert content_hash(bundle['dataset']) == data['hash']
        assert content_hash(bundle['profile']) == profile['hash']
        assert content_hash(bundle['definition']) == bundle['result']['strategy_hash']
        assert PASSWORD not in download.text
        assert client.post(path+'/activate', json={}).status_code == 404
        assert validations.read(old['id']) == old_bundle
        with lab.connect() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_SCHEMA_VERSION
            assert db.execute('SELECT count(*) FROM strategy_versions').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM perp_runs').fetchone()[0] == 0
        backup = create_secure_lab_backup(lab, 'owned optimization backup key')
        report = verify_secure_lab_backup(tmp_path/'lab-backups'/backup['name'], 'owned optimization backup key')
        assert report['counts']['perp_optimizations'] == 1
        lab_access.lock(lab.path)
        for url in (path, path+'/export', endpoint):
            assert client.get(url).status_code == 423
        lab_access.unlock(lab.path, PASSWORD)
        assert archive.PerpOptimizations(LabStore(lab.path)).read(saved['optimization']['id']) == saved
        lab_access.lock(lab.path)


def test_rehashed_result_is_rejected_on_read_and_backup(tmp_path, monkeypatch):
    lab, _, data, profile = seeded(tmp_path, monkeypatch)
    store = archive.PerpOptimizations(lab)
    saved = store.create(data['id'], profile['id'], PLAN)
    identifier = saved['optimization']['id']
    with lab.connect() as db:
        row = db.execute('SELECT payload_json FROM perp_optimizations WHERE id=?', (identifier,)).fetchone()
        result = json.loads(row['payload_json'])
        result['out_of_sample']['metrics']['net_equity_change'] = '9999'
        db.execute('UPDATE perp_optimizations SET payload_json=?,hash=? WHERE id=?',
                   (encoded(result), content_hash(result), identifier))
    with pytest.raises(LabError, match='reproduce'):
        store.read(identifier)
    with pytest.raises(LabError, match='reproduce'):
        create_secure_lab_backup(lab, 'owned optimization backup key')


@pytest.mark.parametrize('operation', ['create', 'read'])
def test_same_password_reauthentication_fences_running_grid(tmp_path, monkeypatch, operation):
    lab, _, data, profile = seeded(tmp_path, monkeypatch, ephemeral=True)
    store = archive.PerpOptimizations(lab)
    if operation == 'read':
        saved = store.create(data['id'], profile['id'], PLAN)
    original = archive.calculate_perp_optimization

    def interrupt(*args):
        result = original(*args)
        lab_access.lock(lab.path)
        lab_access.unlock(lab.path, PASSWORD)
        return result

    monkeypatch.setattr(archive, 'calculate_perp_optimization', interrupt)
    with pytest.raises(LabError, match='cambió|bloque'):
        if operation == 'create':
            store.create(data['id'], profile['id'], PLAN)
        else:
            store.read(saved['optimization']['id'])
    assert store.page(0, 10)['total'] == (1 if operation == 'read' else 0)
    lab_access.lock(lab.path)
