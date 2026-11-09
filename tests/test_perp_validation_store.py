"""Current encrypted research, exact replay, materialization and permission separation."""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.backup import verify_lab_backup
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_profile import PerpProfile
from app.lab.perp_store import PerpDatasetStore
from app.lab.perp_validation import PerpValidationPlan
from app.lab.perp_validation_store import PerpValidations
from app.lab.schema import CURRENT_SCHEMA_VERSION
from app.lab.secure_backup import create_secure_lab_backup,verify_secure_lab_backup
from app.lab.store import LabStore, content_hash, encoded
from app.research.perp_reference import PerpResearchReference
from tests.perp_validation_fixture import validation_inputs


PASSWORD='owned perpetual validation archive key'
HOLDOUT=PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT',train_fraction='0.5')


def seeded(tmp_path,monkeypatch,*,ephemeral=False):
    path=tmp_path/'lab.db'
    if ephemeral:
        lab_access.create(path,PASSWORD);lab=LabStore(path)
    else:lab=LabStore(path,passphrase=PASSWORD)
    data,strategy,profile=validation_inputs(monkeypatch)
    version=lab.create_strategy('Owned validation LONG',strategy)
    profile=PerpProfile.model_validate({**profile.payload(),'strategy_version_id':version['version_id']})
    stored_data=PerpDatasetStore(lab).save(data)
    stored_profile=PerpExperiments(lab).save_profile(profile)
    return lab,PerpValidations(lab),stored_data,stored_profile


@pytest.mark.parametrize('plan',[HOLDOUT,PerpValidationPlan(method='ROLLING_FIXED_VERSION',folds=3),PerpValidationPlan(method='PATH_SENSITIVITY')])
def test_encrypted_create_dedup_replay_materialize_backup_and_reopen(tmp_path,monkeypatch,plan):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch)
    saved=archive.create(data['id'],profile['id'],plan)
    assert saved['verification']=='COMPUTED_AND_SAVED'
    assert archive.create(data['id'],profile['id'],plan)['id']==saved['id']
    bundle=archive.read(saved['id'])
    assert bundle['validation']['verification']=='REPLAY_VERIFIED'
    assert archive.page()['items'][0]['verification']=='HASH_AND_ORIGINALS_ONLY'
    index=1  # reserve or second path scenario
    inspected=archive.inspect_window(saved['id'],index)
    child=PerpExperiments(lab).read(inspected['run']['id'])
    assert content_hash(child['execution']['result'])==bundle['execution']['result']['windows'][index]['result_hash']
    assert archive.inspect_window(saved['id'],index)['run']['id']==inspected['run']['id']
    assert child['execution']['result']['real_authority'] is False
    copy=create_secure_lab_backup(lab,'owned validation backup secret')
    report=verify_secure_lab_backup(lab.path.parent/'lab-backups'/copy['name'],'owned validation backup secret')
    assert report['schema_version']==CURRENT_SCHEMA_VERSION and report['counts']['perp_validations']==1
    reopened=PerpValidations(LabStore(lab.path,passphrase=PASSWORD))
    assert reopened.read(saved['id'])==bundle
    if plan.method=='PATH_SENSITIVITY':
        with pytest.raises(ValueError,match='fuera de muestra'):reopened.reference(saved['id'])
    else:
        reference=archive.reference(saved['id'])
        assert reopened.reference(saved['id'])==reference
        parsed=PerpResearchReference.model_validate(reference)
        assert parsed.execution_authority=='NONE' and parsed.profile.strategy_version_id==profile['profile']['strategy_version_id']
        assert parsed.sample_bars==bundle['execution']['result']['reserve_bars']
    assert PASSWORD not in json.dumps(bundle)


@pytest.mark.parametrize('rehash',[False,True])
def test_tampered_report_or_child_digest_never_passes_replay_or_backup(tmp_path,monkeypatch,rehash):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch)
    saved=archive.create(data['id'],profile['id'],HOLDOUT)
    with lab.connect() as db:
        raw=json.loads(db.execute('SELECT payload_json FROM perp_validations WHERE id=?',(saved['id'],)).fetchone()[0])
        raw['result']['windows'][1]['metrics']['final_equity']='999999'
        db.execute('UPDATE perp_validations SET payload_json=?,hash=? WHERE id=?',
            (encoded(raw),content_hash(raw) if rehash else saved['hash'],saved['id']))
    with pytest.raises(LabError):archive.read(saved['id'])
    with pytest.raises(LabError):archive.reference(saved['id'])
    with pytest.raises(LabError):archive.inspect_window(saved['id'],1)
    with pytest.raises(LabError):create_secure_lab_backup(lab,'owned validation backup secret')


@pytest.mark.parametrize('operation',['create','read','inspect'])
def test_reauthentication_same_key_rejects_old_validation_work(tmp_path,monkeypatch,operation):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch,ephemeral=True)
    saved=archive.create(data['id'],profile['id'],HOLDOUT)
    from app.lab.perp_validation_store import calculate_perp_validation as original
    def compute(*args,**kwargs):
        result=original(*args,**kwargs)
        lab_access.lock(lab.path);lab_access.unlock(lab.path,PASSWORD)
        return result
    monkeypatch.setattr('app.lab.perp_validation_store.calculate_perp_validation',compute)
    with pytest.raises(LabError,match='cambió|bloqueó'):
        if operation=='create':archive.create(data['id'],profile['id'],PerpValidationPlan(method='ROLLING_FIXED_VERSION',folds=3))
        elif operation=='read':archive.read(saved['id'])
        else:archive.inspect_window(saved['id'],1)
    assert archive.page()['total']==1
    assert PerpExperiments(lab).page()['total']==0


def test_reauthentication_between_verified_report_and_child_creation_is_fenced(tmp_path,monkeypatch):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch,ephemeral=True)
    saved=archive.create(data['id'],profile['id'],HOLDOUT)
    original=archive.experiments.save_profile
    def switch(*args,**kwargs):
        lab_access.lock(lab.path);lab_access.unlock(lab.path,PASSWORD)
        return original(*args,**kwargs)
    monkeypatch.setattr(archive.experiments,'save_profile',switch)
    with pytest.raises(LabError,match='sesión LAB cambió'):archive.inspect_window(saved['id'],1)
    assert PerpExperiments(lab).page()['total']==0


def test_modified_original_during_calculation_is_not_committed(tmp_path,monkeypatch):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch)
    from app.lab.perp_validation_store import calculate_perp_validation as original
    def compute(*args,**kwargs):
        result=original(*args,**kwargs)
        with lab.connect() as db:
            raw=json.loads(db.execute('SELECT definition_json FROM strategy_versions WHERE id=?',(profile['profile']['strategy_version_id'],)).fetchone()[0])
            raw['stop_loss']='0.4'
            db.execute('UPDATE strategy_versions SET definition_json=? WHERE id=?',(encoded(raw),profile['profile']['strategy_version_id']))
        return result
    monkeypatch.setattr('app.lab.perp_validation_store.calculate_perp_validation',compute)
    with pytest.raises(LabError,match='Perfil'):archive.create(data['id'],profile['id'],HOLDOUT)
    with lab.connect() as db:assert db.execute('SELECT count(*) FROM perp_validations').fetchone()[0]==0


def test_additive_11_to12_keeps_existing_runs(tmp_path,monkeypatch):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch)
    run=PerpExperiments(lab).run(data['id'],profile['id'])
    original=PerpExperiments(lab).read(run['id'])
    with lab.connect() as db:
        db.execute('DROP TABLE perp_optimization_variants')
        db.execute('DROP TABLE perp_optimizations')
        db.execute('DROP TABLE perp_rule_observations')
        db.execute('DROP TABLE perp_validations');db.execute('PRAGMA user_version=11')
    assert PerpExperiments(lab).read(run['id'])==original
    assert archive.page()['total']==0
    with lab.connect() as db:assert db.execute('PRAGMA user_version').fetchone()[0]==CURRENT_SCHEMA_VERSION


def test_reference_rejects_changed_hash_profile_windows_and_authority(tmp_path,monkeypatch):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch)
    saved=archive.create(data['id'],profile['id'],HOLDOUT)
    reference=archive.reference(saved['id'])
    for key,value in [('sha256','0'*64),('sample_bars',1),('execution_authority','REAL')]:
        with pytest.raises(ValueError):PerpResearchReference.model_validate({**reference,key:value})


def test_local_api_strict_origin_locked_and_invalid_window(tmp_path,monkeypatch):
    lab,archive,data,profile=seeded(tmp_path,monkeypatch,ephemeral=True)
    app=FastAPI();app.include_router(create_lab_router(lab.path))
    body={'dataset_id':data['id'],'profile_id':profile['id'],'plan':HOLDOUT.payload()}
    with TestClient(app) as client:
        assert client.post('/lab/perp/validations',json=body,headers={'Origin':'https://external.invalid'}).status_code==403
        assert client.post('/lab/perp/validations',json={**body,'plan':{'method':'ROLLING_FIXED_VERSION','folds':True}}).status_code==422
        made=client.post('/lab/perp/validations',json=body);assert made.status_code==201,made.text
        identifier=made.json()['id'];url='/lab/perp/validations/'+identifier
        bundle=client.get(url);assert bundle.status_code==200
        assert client.get(url+'/export').json()==bundle.json()
        assert client.get(url+'/reference').json()['execution_authority']=='NONE'
        assert client.post(url+'/windows/1/inspect').status_code==201
        assert client.post(url+'/windows/-1/inspect').status_code==422
        assert client.get('/lab/perp/validations?limit=1').json()['total']==1
        lab_access.lock(lab.path)
        assert client.get(url).status_code==client.get(url+'/reference').status_code==423
        assert client.post(url+'/windows/1/inspect').status_code==423
        assert client.post('/lab/perp/validations',json=body).status_code==423


def test_missing_archive_empty_and_plain_store_rejected(tmp_path,monkeypatch):
    missing=tmp_path/'missing.db';archive=PerpValidations(LabStore(missing))
    assert archive.page()['total']==0 and not missing.exists()
    lab=LabStore(tmp_path/'plain.db')
    with lab.connect():pass
    with pytest.raises(LabError,match='cifrado'):
        PerpValidations(lab).create('x','y',HOLDOUT)
