"""Versioned LIMIT profiles/runs coexist with original encrypted experiments."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_profile import read_profile
from app.lab.perp_simulation import LIMIT_ENGINE
from app.lab.secure_backup import create_secure_lab_backup, verify_secure_lab_backup
from app.lab.store import LabStore, content_hash, encoded
from tests.test_perp_experiments import seeded, PASSWORD


def limit_payload(saved):
    return {**saved['profile'], 'schema':'orbis.lab.perp.profile.v2',
        'execution':'LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE',
        'end_action':'CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE',
        'entry_order':{'reference':'CLOSED_SIGNAL_TRADE_CLOSE', 'offset':'0.02', 'valid_bars':3}}


def test_limit_and_market_archives_replay_deduplicate_and_reopen_together(tmp_path, monkeypatch):
    lab, archive, data, configured = seeded(tmp_path, monkeypatch)
    market = archive.run(data['id'], configured['id'])
    original = archive.read(market['id'])
    selected = archive.save_profile(read_profile(limit_payload(configured)))
    saved = archive.run(data['id'], selected['id'])
    bundle = archive.read(saved['id'])
    assert saved['engine'] == LIMIT_ENGINE and saved['id'] != market['id']
    assert bundle['schema'] == 'orbis.lab.perp.bundle.v2'
    assert bundle['execution']['schema'] == 'orbis.lab.perp.run.v2'
    assert bundle['execution']['result']['trades'][0]['entry_price'] == '98.00'
    assert archive.run(data['id'], selected['id'])['id'] == saved['id']
    assert archive.read(market['id']) == original
    assert archive.page()['total'] == 2 and len(archive.profiles()) == 2
    backup = create_secure_lab_backup(lab, 'owned independent limit backup')
    checked = verify_secure_lab_backup(lab.path.parent/'lab-backups'/backup['name'], 'owned independent limit backup')
    assert checked['counts']['perp_profiles'] == checked['counts']['perp_runs'] == 2
    reopened = PerpExperiments(LabStore(lab.path, passphrase=PASSWORD))
    assert reopened.read(saved['id']) == bundle and reopened.read(market['id']) == original
    assert lab.path.read_bytes()[:16] != b'SQLite format 3\x00'


@pytest.mark.parametrize('change', ['ENGINE_PROFILE_MISMATCH', 'REHASHED_RESULT'])
def test_changed_limit_result_or_market_identity_is_rejected(tmp_path, monkeypatch, change):
    lab, archive, data, configured = seeded(tmp_path, monkeypatch)
    selected = archive.save_profile(read_profile(limit_payload(configured)))
    saved = archive.run(data['id'], selected['id'])
    with lab.connect() as db:
        row = db.execute('SELECT payload_json FROM perp_runs WHERE id=?', (saved['id'],)).fetchone()
        raw = json.loads(row[0])
        if change == 'ENGINE_PROFILE_MISMATCH':
            raw.update(schema='orbis.lab.perp.run.v1', engine='perp-ohlc-1.0')
            raw['result']['engine'] = 'perp-ohlc-1.0'
        else:
            raw['result']['metrics']['cash'] = '999999'
        db.execute('UPDATE perp_runs SET payload_json=?,hash=? WHERE id=?',
            (encoded(raw), content_hash(raw), saved['id']))
    with pytest.raises(LabError):
        archive.read(saved['id'])


def test_limit_profile_api_is_strict_recoverable_and_explicit_about_pending_handoff(tmp_path, monkeypatch):
    lab, archive, data, configured = seeded(tmp_path, monkeypatch, ephemeral=True)
    app = FastAPI()
    app.include_router(create_lab_router(lab.path))
    body = {'version_id':configured['profile']['strategy_version_id'], 'margin_mode':'ISOLATED',
        'leverage':'5', 'trade_path':'O_H_L_C', 'mark_path':'O_H_L_C',
        'entry_order':{'offset':'0.02', 'valid_bars':3}}
    with TestClient(app) as client:
        assert client.post('/lab/perp/profiles', json=body, headers={'Origin':'https://remote.invalid'}).status_code == 403
        malformed = {**body, 'entry_order':{'offset':'0.02', 'valid_bars':True}}
        assert client.post('/lab/perp/profiles', json=malformed).status_code == 422
        made = client.post('/lab/perp/profiles', json=body)
        assert made.status_code == 201, made.text
        profile = made.json()
        assert profile['profile']['schema'] == 'orbis.lab.perp.profile.v2'
        created = client.post('/lab/perp/runs', json={'dataset_id':data['id'], 'profile_id':profile['id']})
        assert created.status_code == 201, created.text
        identifier = created.json()['id']
        bundle = client.get('/lab/perp/runs/'+identifier).json()
        assert bundle == client.get('/lab/perp/runs/'+identifier+'/export').json()
        assert bundle['run']['engine'] == LIMIT_ENGINE
        assert PASSWORD not in json.dumps(bundle)
        validated = client.post('/lab/perp/validations', json={
            'dataset_id':data['id'], 'profile_id':profile['id'], 'plan':{'method':'PATH_SENSITIVITY'}})
        assert validated.status_code == 201, validated.text
        assert validated.json()['simulation_engine'] == LIMIT_ENGINE
        assert client.get('/lab/perp/validations').json()['total'] == 1
        reference = client.get('/lab/perp/validations/'+validated.json()['id']+'/reference')
        assert reference.status_code == 422 and 'fuera de muestra' in reference.json()['detail']
        lab_access.lock(lab.path)
        assert client.get('/lab/perp/runs/'+identifier).status_code == 423
