"""The LAB ladder remains reproducible and can provide a fictitious PAPER origin."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.perp_observed_origin import verified_observed_origin
from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_validation_store import PerpValidations
from app.lab.secure_backup import create_secure_lab_backup,verify_secure_lab_backup
from app.lab.store import LabStore,content_hash,encoded
from tests.test_perp_partial_simulation import TARGETS
from tests.test_perp_validation_store import HOLDOUT,PASSWORD,seeded


def test_api_saves_verifies_and_reopens_partial_profiles_and_reserves(tmp_path,monkeypatch):
    lab,archive,data,legacy=seeded(tmp_path,monkeypatch,ephemeral=True)
    original=archive.create(data['id'],legacy['id'],HOLDOUT)
    original_bundle=archive.read(original['id'])
    app=FastAPI();app.include_router(create_lab_router(lab.path))
    with TestClient(app) as client:
        body={'version_id':legacy['profile']['strategy_version_id'],'margin_mode':'ISOLATED',
            'leverage':'5','trade_path':'O_H_L_C','mark_path':'O_H_L_C',
            'entry_order':{'offset':'0','valid_bars':3},'partial_targets':TARGETS}
        invalid={**body,'partial_targets':[{'target':'0.9','fraction':'0.2'}]}
        assert client.post('/lab/perp/profiles',json=invalid).status_code==422
        created=client.post('/lab/perp/profiles',json=body)
        assert created.status_code==201,created.text
        profile=created.json()
        assert profile['profile']['schema']=='orbis.lab.perp.profile.v3'
        assert client.post('/lab/perp/profiles',json=body).json()==profile
        scope={'dataset_id':data['id'],'profile_id':profile['id']}
        result=client.post('/lab/perp/runs',json=scope)
        assert result.status_code==201,result.text
        run=PerpExperiments(lab).read(result.json()['id'])
        assert run['schema']=='orbis.lab.perp.bundle.v3'
        saved=archive.create(**scope,plan=HOLDOUT)
        bundle=archive.read(saved['id'])
        assert bundle['schema']=='orbis.lab.perp.validation.bundle.v3'
        assert bundle['execution']['simulation_engine']=='perp-ohlc-3.0'
        assert 'PARTIAL_TARGETS_EXPERIMENTAL_LAB_ONLY' in bundle['execution']['result']['warnings']
        child=archive.inspect_window(saved['id'],1)['run']
        assert content_hash(PerpExperiments(lab).read(child['id'])['execution']['result'])==bundle['execution']['result']['windows'][1]['result_hash']
        reference=client.get('/lab/perp/validations/'+saved['id']+'/reference')
        assert reference.status_code==200 and reference.json()['execution_authority']=='NONE'
        assert reference.json()['schema']=='orbis.research.perp.reference.v3'
        assert verified_observed_origin(lab,saved['id']).reference().sha256==reference.json()['sha256']
        assert archive.read(original['id'])==original_bundle
    backup=create_secure_lab_backup(lab,'owned independent partial backup')
    checked=verify_secure_lab_backup(lab.path.parent/'lab-backups'/backup['name'],'owned independent partial backup')
    assert checked['counts']['perp_validations']==2
    lab_access.lock(lab.path);lab_access.unlock(lab.path,PASSWORD)
    reopened=PerpValidations(LabStore(lab.path))
    assert reopened.read(saved['id'])==bundle
    with lab.connect() as db:
        raw=json.loads(db.execute('SELECT payload_json FROM perp_runs WHERE id=?',(run['run']['id'],)).fetchone()[0])
        raw['result']['partial_events'].append({'status':'FILLED','quantity':'999'})
        db.execute('UPDATE perp_runs SET payload_json=?,hash=? WHERE id=?',(encoded(raw),content_hash(raw),run['run']['id']))
    with pytest.raises(LabError,match='no se reproduce'):
        PerpExperiments(lab).read(run['run']['id'])
