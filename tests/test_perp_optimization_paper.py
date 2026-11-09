"""Verified optimized holdout to Paper, using isolated encrypted fixtures."""
import json
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.derivatives.perp_observed_origin import verified_observed_origin
from app.derivatives.perp_observed_service import ObservedPaperService
from app.lab import perp_validation_store as validation
from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_optimization_store import PerpOptimizations
from app.lab.perp_profile import read_profile
from app.lab.perp_store import PerpDatasetStore
from app.lab.secure_backup import create_secure_lab_backup, verify_secure_lab_backup
from app.lab.store import content_hash, encoded
from tests.perp_observed_archive_fixture import owned_archive
from tests.test_perp_optimization import PLAN
from tests.test_perp_optimization_variant import make_grid
from tests.test_perp_validation_store import PASSWORD, seeded


def build_from_origin(lab, origin):
    reference = origin.reference()
    optimizations = PerpOptimizations(lab)
    archive = optimizations.create(reference.dataset_id, reference.profile_id, PLAN)
    record = archive['optimization']
    variant = optimizations.materialize(record['id'], record['hash'])
    return record, archive['result'], variant


def test_preparation_uses_exact_optimized_reserve_and_starts_no_observer(tmp_path, monkeypatch):
    lab, _, paper, original, _, _ = owned_archive(tmp_path, monkeypatch)
    record, report, variant = build_from_origin(lab, original)
    validations = validation.PerpValidations(lab)
    fixed = validations.create(record['dataset_id'], variant['profile_id'],
                               validation.PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT', train_fraction='0.5'))
    app = FastAPI()
    app.include_router(create_lab_router(lab.path))
    route = '/lab/perp/optimizations/' + record['id'] + '/paper-validation'
    with TestClient(app) as client:
        body = {'expected_hash':record['hash']}
        assert client.post(route, json=body, headers={'Origin':'https://outside.invalid'}).status_code == 403
        assert client.post(route, json={'expected_hash':'0'*64}).status_code == 422
        response = client.post(route, json=body)
        assert response.status_code == 201, response.text
        summary = response.json()
        assert response.headers['Cache-Control'] == 'no-store'
        assert client.post(route, json=body).json() == summary
        assert summary['profile_id'] == variant['profile_id'] and summary['hash'] != fixed['hash']
        linked = summary['optimization_origin']
        assert linked['optimization_hash'] == record['hash'] and linked['reserve_run_id'] == variant['reserve_run_id']
        identifier = summary['id']
        bundle = validations.read(identifier)
        assert bundle['execution']['result'] == validations.read(fixed['id'])['execution']['result']
        assert bundle['execution']['schema'] == 'orbis.lab.perp.validation.optimized.v1'
        assert bundle['strategy']['version_id'] == variant['version_id']
        exported = client.get('/lab/perp/validations/'+identifier+'/export').json()
        assert exported['execution']['optimization_origin'] == linked
        reference = validations.reference(identifier)
        assert reference['artifact_hash'] == summary['hash']
        assert reference['windows'][0]['result_hash'] == report['out_of_sample']['result_hash']
        backup = create_secure_lab_backup(lab, 'owned optimized validation backup key')
        verified = verify_secure_lab_backup(tmp_path/'lab-backups'/backup['name'], 'owned optimized validation backup key')
        assert verified['counts']['perp_validations'] == 3
        service = ObservedPaperService(lab, paper, source_factory=lambda: pytest.fail('market read in preparation'))
        service.start()
        try:
            review = service.prepare(identifier, preparation_id=str(uuid4()))
            assert review['phase'] == 'READY' and review['optimization_origin'] == linked
            assert review['reference'] == reference
            assert review['exit_parameters_pct']['take_profit'] == '1.00'
            assert review['session_created'] is False and review['execution_authority'] == 'NONE'
            assert service.snapshot()['running_workers'] == 0
        finally:
            service.stop()
        lab_access.lock(lab.path)
        assert client.post(route, json=body).status_code == 423


def test_optimized_handoff_rejects_wrong_link_and_rechecks_before_activation(tmp_path, monkeypatch):
    lab, store, data, _, made = make_grid(tmp_path, monkeypatch)
    record = made['optimization']
    validations = validation.PerpValidations(lab)
    with pytest.raises(LabError, match='Guarda primero'):
        validations.create_from_optimization(record['id'], record['hash'])
    variant = store.materialize(record['id'], record['hash'])
    saved = validations.create_from_optimization(record['id'], record['hash'])
    origin = verified_observed_origin(lab, saved['id'])
    with origin.hold_current():
        pass
    unrelated = PerpExperiments(lab).run(data['id'], variant['profile_id'])
    with lab.connect() as db:
        db.execute('UPDATE perp_optimization_variants SET reserve_run_id=? WHERE optimization_id=?',
                   (unrelated['id'], record['id']))
    with pytest.raises(LabError, match='tramos'):
        with origin.hold_current():
            pytest.fail('could start Paper after link tampering')
    with pytest.raises(LabError, match='tramos'):
        validations.read(saved['id'])


def test_optimized_handoff_rejects_rehashed_claim_and_revoked_session(tmp_path, monkeypatch):
    lab, store, _, _, made = make_grid(tmp_path, monkeypatch, ephemeral=True)
    record = made['optimization']
    store.materialize(record['id'], record['hash'])
    validations = validation.PerpValidations(lab)
    original = validation.calculate_perp_optimization

    def expire(*args):
        result = original(*args)
        lab_access.lock(lab.path)
        lab_access.unlock(lab.path, PASSWORD)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(validation, 'calculate_perp_optimization', expire)
        with pytest.raises(LabError, match='sesión LAB cambió'):
            validations.create_from_optimization(record['id'], record['hash'])
    assert validations.page()['total'] == 0
    saved = validations.create_from_optimization(record['id'], record['hash'])
    with lab.connect() as db:
        raw = json.loads(db.execute('SELECT payload_json FROM perp_validations WHERE id=?',(saved['id'],)).fetchone()[0])
        raw['optimization_origin']['reserve_run_id'] = raw['optimization_origin']['development_run_id']
        db.execute('UPDATE perp_validations SET payload_json=?,hash=? WHERE id=?',
                   (encoded(raw), content_hash(raw), saved['id']))
    with pytest.raises(LabError, match='reserva original'):
        verified_observed_origin(lab, saved['id'])
    with pytest.raises(LabError, match='reserva original'):
        create_secure_lab_backup(lab, 'owned optimized validation backup key')
    lab_access.lock(lab.path)


def test_out_of_bounds_winner_is_rejected_with_validation_error(tmp_path, monkeypatch):
    lab, store, _, _, made = make_grid(tmp_path, monkeypatch)
    record = made['optimization']
    store.materialize(record['id'], record['hash'])
    with lab.connect() as db:
        raw = json.loads(db.execute('SELECT payload_json FROM perp_optimizations WHERE id=?',
                                   (record['id'],)).fetchone()[0])
        raw['selected_index'] = len(raw['candidates'])
        digest = content_hash(raw)
        db.execute('UPDATE perp_optimizations SET payload_json=?,hash=? WHERE id=?',
                   (encoded(raw), digest, record['id']))
    with pytest.raises(LabError, match='Selección guardada'):
        validation.PerpValidations(lab).create_from_optimization(record['id'], digest)


def test_liquidated_reserve_is_ineligible_for_paper(tmp_path, monkeypatch):
    lab, _, archived_data, profile = seeded(tmp_path, monkeypatch)
    datasets = PerpDatasetStore(lab)
    _, dataset = datasets.read(archived_data['id'])
    raw = dataset.payload()
    for bar in raw['mark']['bars'][60:]:
        bar['l'] = '10'
    changed = datasets.save(read_perp_dataset(raw))
    configured = read_profile({**profile['profile'], 'trade_path':'O_L_H_C', 'mark_path':'O_L_H_C'})
    observed_profile = PerpExperiments(lab).save_profile(configured)
    store = PerpOptimizations(lab)
    report = store.create(changed['id'], observed_profile['id'], PLAN)
    assert report['result']['out_of_sample']['metrics']['liquidated']
    record = report['optimization']
    store.materialize(record['id'], record['hash'])
    validations = validation.PerpValidations(lab)
    with pytest.raises(LabError, match='liquidación'):
        validations.create_from_optimization(record['id'], record['hash'])
    assert validations.page()['total'] == 0
