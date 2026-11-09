"""LIMIT validation keeps encrypted originals and cannot authorize MARKET PAPER."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.perp_observed_origin import verified_observed_origin
from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_profile import read_profile
from app.lab.perp_validation import PerpValidationPlan
from app.lab.perp_validation_store import PerpValidations
from app.lab.secure_backup import create_secure_lab_backup, verify_secure_lab_backup
from app.lab.store import LabStore, content_hash, encoded
from app.research.perp_reference import PerpLimitResearchReference, PerpResearchReference
from tests.test_perp_limit_archive import limit_payload
from tests.test_perp_validation_store import HOLDOUT, PASSWORD, seeded


def prepared(tmp_path, monkeypatch, *, ephemeral=False):
    lab, archive, data, market = seeded(tmp_path, monkeypatch, ephemeral=ephemeral)
    payload = limit_payload(market)
    payload['entry_order']['offset'] = '0.01'
    profile = PerpExperiments(lab).save_profile(read_profile(payload))
    return lab, archive, data, market, profile


@pytest.mark.parametrize('plan', [HOLDOUT,
    PerpValidationPlan(method='ROLLING_FIXED_VERSION', folds=3),
    PerpValidationPlan(method='PATH_SENSITIVITY')])
def test_limit_encrypted_windows_reopen_backup_reference_and_market_coexistence(tmp_path, monkeypatch, plan):
    lab, archive, data, market, profile = prepared(tmp_path, monkeypatch)
    old = archive.create(data['id'], market['id'], plan)
    original = archive.read(old['id'])
    saved = archive.create(data['id'], profile['id'], plan)
    assert archive.create(data['id'], profile['id'], plan)['id'] == saved['id']
    bundle = archive.read(saved['id'])
    assert bundle['schema'] == 'orbis.lab.perp.validation.bundle.v2'
    assert bundle['execution']['schema'] == 'orbis.lab.perp.validation.v2'
    assert bundle['execution']['result']['engine'] == 'perp-validation-2.0'
    assert bundle['execution']['result']['simulation_engine'] == 'perp-ohlc-2.0'
    inspected = archive.inspect_window(saved['id'], 1)
    child = PerpExperiments(lab).read(inspected['run']['id'])
    assert child['schema'] == 'orbis.lab.perp.bundle.v2'
    assert content_hash(child['execution']['result']) == bundle['execution']['result']['windows'][1]['result_hash']
    assert archive.inspect_window(saved['id'], 1)['run']['id'] == inspected['run']['id']
    assert archive.read(old['id']) == original and archive.page()['total'] == 2
    backup = create_secure_lab_backup(lab, 'owned LIMIT validation backup')
    checked = verify_secure_lab_backup(lab.path.parent/'lab-backups'/backup['name'], 'owned LIMIT validation backup')
    from app.lab.schema import CURRENT_SCHEMA_VERSION
    assert checked['schema_version'] == CURRENT_SCHEMA_VERSION and checked['counts']['perp_validations'] == 2
    reopened = PerpValidations(LabStore(lab.path, passphrase=PASSWORD))
    assert reopened.read(saved['id']) == bundle and reopened.read(old['id']) == original
    if plan.method == 'PATH_SENSITIVITY':
        with pytest.raises(ValueError, match='fuera de muestra'):
            reopened.reference(saved['id'])
    else:
        reference = reopened.reference(saved['id'])
        parsed = PerpLimitResearchReference.model_validate(reference)
        assert reference['schema'] == 'orbis.research.perp.reference.v2'
        assert parsed.execution_authority == 'NONE'
        assert parsed.profile.payload() == profile['profile']
        assert parsed.sample_bars == bundle['execution']['result']['reserve_bars']
        assert archive.reference(saved['id']) == reference
        for value in (reference, parsed):
            with pytest.raises(ValueError):
                PerpResearchReference.model_validate(value)
        assert verified_observed_origin(lab, saved['id']).reference() == parsed
    assert PASSWORD not in json.dumps(bundle)


@pytest.mark.parametrize('change', ['CHILD_HASH', 'WINDOW_METRIC', 'VERSION_SWAP', 'ORDER_PARAMS'])
def test_rehashed_limit_validation_changes_fail_replay_reference_inspection_and_backup(tmp_path, monkeypatch, change):
    lab, archive, data, market, profile = prepared(tmp_path, monkeypatch)
    saved = archive.create(data['id'], profile['id'], HOLDOUT)
    with lab.connect() as db:
        raw = json.loads(db.execute('SELECT payload_json FROM perp_validations WHERE id=?', (saved['id'],)).fetchone()[0])
        window = raw['result']['windows'][1]
        if change == 'CHILD_HASH':
            window['result_hash'] = '0'*64
        elif change == 'WINDOW_METRIC':
            window['metrics']['final_equity'] = '999999'
        elif change == 'ORDER_PARAMS':
            window['profile']['entry_order']['valid_bars'] += 1
        else:
            raw.update(schema='orbis.lab.perp.validation.v1', engine='perp-validation-1.0', simulation_engine='perp-ohlc-1.0')
            raw['result'].update(schema='orbis.lab.perp.validation.result.v1', engine='perp-validation-1.0', simulation_engine='perp-ohlc-1.0')
        db.execute('UPDATE perp_validations SET payload_json=?,hash=? WHERE id=?', (encoded(raw), content_hash(raw), saved['id']))
    with pytest.raises(LabError): archive.read(saved['id'])
    with pytest.raises(LabError): archive.reference(saved['id'])
    with pytest.raises(LabError): archive.inspect_window(saved['id'], 1)
    with pytest.raises(LabError): create_secure_lab_backup(lab, 'owned LIMIT validation backup')


def test_limit_validation_private_api_export_and_lock(tmp_path, monkeypatch):
    lab, archive, data, market, profile = prepared(tmp_path, monkeypatch, ephemeral=True)
    app = FastAPI(); app.include_router(create_lab_router(lab.path))
    body = {'dataset_id':data['id'], 'profile_id':profile['id'], 'plan':HOLDOUT.payload()}
    with TestClient(app) as client:
        assert client.post('/lab/perp/validations', json=body, headers={'Origin':'https://external.invalid'}).status_code == 403
        created = client.post('/lab/perp/validations', json=body)
        assert created.status_code == 201, created.text
        url = '/lab/perp/validations/'+created.json()['id']
        bundle = client.get(url)
        assert bundle.status_code == 200 and bundle.json() == client.get(url+'/export').json()
        reference = client.get(url+'/reference').json()
        assert reference['schema'] == 'orbis.research.perp.reference.v2' and reference['execution_authority'] == 'NONE'
        lab_access.lock(lab.path)
        assert client.get(url).status_code == client.get(url+'/reference').status_code == 423


@pytest.mark.parametrize('operation', ['create', 'read', 'inspect'])
def test_limit_work_started_in_an_old_unlock_session_cannot_finish(tmp_path, monkeypatch, operation):
    lab, archive, data, market, profile = prepared(tmp_path, monkeypatch, ephemeral=True)
    saved = archive.create(data['id'], profile['id'], HOLDOUT)
    from app.lab.perp_validation_store import calculate_perp_validation as original
    def compute(*args, **kwargs):
        result = original(*args, **kwargs)
        lab_access.lock(lab.path); lab_access.unlock(lab.path, PASSWORD)
        return result
    monkeypatch.setattr('app.lab.perp_validation_store.calculate_perp_validation', compute)
    with pytest.raises(LabError, match='cambió|bloqueó'):
        if operation == 'create':
            archive.create(data['id'], profile['id'], PerpValidationPlan(method='ROLLING_FIXED_VERSION', folds=3))
        elif operation == 'read':
            archive.read(saved['id'])
        else:
            archive.inspect_window(saved['id'], 1)
    assert archive.page()['total'] == 1 and PerpExperiments(lab).page()['total'] == 0
