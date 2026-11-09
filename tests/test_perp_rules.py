"""Local public rules archive, exact chronology, encryption and backup."""
from copy import deepcopy
import json

import pytest

from app.lab.access import lab_access
from app.lab.engine import LabError
from app.lab.perp_rules import RuleObservation, RuleObservationStore, capture_rule_observation, insert_observation
from app.lab.perp_store import PerpDatasetStore
from app.lab.schema import CURRENT_SCHEMA_VERSION
from app.lab.secure_backup import create_secure_lab_backup,verify_secure_lab_backup
from app.lab.store import LabStore,content_hash
from tests.perp_dataset_fixture import END,NOW,PASSWORD,START,SYMBOL,prepared,source
from tests.test_perp_dataset import api


def shifted(original, at, *, update=None):
    payload=deepcopy(original.payload())
    for field in ('rules','specification'):
        payload[field]['fetched_at_ns']=str(at)
    if update is not None:
        update(payload)
    return RuleObservation.model_validate(payload)


def test_captures_are_immutable_comparable_and_reopen_with_exact_backup(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';lab_access.create(path,PASSWORD)
    lab=LabStore(path);datasets=PerpDatasetStore(lab);archive=RuleObservationStore(lab)
    dataset=prepared(monkeypatch);saved=datasets.save(dataset);original=RuleObservation.from_dataset(dataset)
    before=archive.page(SYMBOL)
    assert before['items'][0]['observation']==original.payload()
    assert before['items'][0]['changes'] is None and before['scope']=='OBSERVED_AT_CAPTURE_ONLY'
    assert archive.save(original)['hash']==before['items'][0]['hash']

    def representation(payload):
        payload['rules']['max_position_size']='100.000'
        payload['specification']['tick_size']='0.0100'

    equal=shifted(original,NOW+1,update=representation)
    equal_saved=archive.save(equal)
    changed=shifted(equal,NOW+2,update=lambda p:p['rules']['risk_brackets'][0].update(initial_margin_rate='0.04'))
    changed_saved=archive.save(changed)
    instrument=shifted(changed,NOW+3,update=lambda p:p['specification'].update(min_notional='7'))
    archive.save(instrument)
    page=archive.page(SYMBOL)
    assert [item['changes'] for item in page['items']]==[['specification'],['rules'],[],None]
    assert page['items'][1]['hash']==changed_saved['hash'] and page['items'][2]['hash']==equal_saved['hash']
    assert datasets.read(saved['id'])[1].payload()==dataset.payload()
    assert dataset.risk_rules_scope=='CURRENT_SNAPSHOT_NOT_HISTORICAL'
    assert archive.page('ETH/USDT:PERP')['items']==[]
    with pytest.raises(LabError,match='cifrado'):
        RuleObservationStore(LabStore(tmp_path/'plain.db')).save(original)
    assert not (tmp_path/'plain.db').exists()
    backup=create_secure_lab_backup(lab,'owned rules backup passphrase')
    restored=verify_secure_lab_backup(path.parent/'lab-backups'/backup['name'],'owned rules backup passphrase')
    assert restored['schema_version']==CURRENT_SCHEMA_VERSION and restored['counts']['perp_rule_observations']==4
    lab_access.lock(path)
    with pytest.raises(LabError,match='desbloqueado'):
        archive.page(SYMBOL)
    lab_access.unlock(path,PASSWORD)
    assert RuleObservationStore(LabStore(path)).page(SYMBOL)==page
    with lab.connect() as db:
        row=db.execute('SELECT * FROM perp_rule_observations WHERE hash=?',(changed_saved['hash'],)).fetchone()
        body=json.loads(row['payload_json'])
        body['scope']='HISTORICAL'
        db.execute('UPDATE perp_rule_observations SET payload_json=?,hash=? WHERE hash=?',
            (json.dumps(body),content_hash(body),row['hash']))
    with pytest.raises(LabError,match='no verificable'):
        archive.page(SYMBOL)
    with pytest.raises(LabError,match='no verificable'):
        create_secure_lab_backup(lab,'owned rules backup passphrase')


def test_keyset_pagination_keeps_same_instant_rows_and_the_previous_observation(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';lab_access.create(path,PASSWORD)
    lab=LabStore(path);archive=RuleObservationStore(lab);original=RuleObservation.from_dataset(prepared(monkeypatch))
    with lab.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for i in range(51):
            at=NOW+i//3
            insert_observation(db,shifted(original,at,update=lambda p:p['specification'].update(min_notional=str(5+i))))
    first=archive.page(SYMBOL)
    cursor=first['next_before']
    second=archive.page(SYMBOL,before=(int(cursor['ns']),cursor['hash']))
    items=first['items']+second['items']
    assert len(first['items'])==50 and second['next_before'] is None
    assert len(items)==len({item['hash'] for item in items})==51
    assert first['items'][-1]['same_instant'] and first['items'][-1]['changes'] is None
    assert items[-1]['changes'] is None and items[-1]['same_instant']


def test_encrypted_schema_12_opens_without_rewriting_old_datasets(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';lab=LabStore(path)
    lab_access.create(path,PASSWORD)
    saved=PerpDatasetStore(lab).save(prepared(monkeypatch))
    with lab.connect() as db:
        db.execute('DROP TABLE perp_optimization_variants')
        db.execute('DROP TABLE perp_optimizations')
        db.execute('DROP TABLE perp_rule_observations')
        db.execute('PRAGMA user_version=12')
        before=[tuple(row) for row in db.execute('SELECT * FROM perp_datasets')]
    lab_access.lock(path)
    lab_access.unlock(path,PASSWORD)
    with lab.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==CURRENT_SCHEMA_VERSION
        assert [tuple(row) for row in db.execute('SELECT * FROM perp_datasets')]==before
        assert db.execute('SELECT count(*) FROM perp_rule_observations').fetchone()[0]==0
    assert PerpDatasetStore(lab).list()==[saved]


def test_api_capture_uses_only_two_public_calls_and_respects_lab_epoch(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';client=api(path)
    endpoint='/lab/perp/rules'
    body={'symbol':SYMBOL}
    assert client.get(endpoint,params=body).status_code==423
    assert client.post('/lab/create',json={'passphrase':PASSWORD}).is_success
    adapter,calls=source(monkeypatch)
    monkeypatch.setattr('app.marketdata.adapters.grvt.GrvtAdapter',lambda:adapter)
    assert client.post(endpoint+'/capture',json={**body,'effective_at_ns':NOW}).status_code==422
    assert client.post(endpoint+'/capture',json=body,headers={'Origin':'https://outside.invalid'}).status_code==403
    assert not calls
    captured=client.post(endpoint+'/capture',json=body)
    assert captured.is_success and captured.headers['cache-control']=='no-store',captured.text
    assert [url.rsplit('/',1)[-1] for url,_ in calls]==['margin_rules','instrument']
    page=client.get(endpoint,params=body).json()
    assert page['items'][0]['hash']==captured.json()['hash'] and page['items'][0]['observed_at_ns']==str(NOW)
    assert client.get(endpoint,params={**body,'before_ns':str(NOW)}).status_code==422
    download=client.post('/lab/perp/datasets/download',json={**body,'timeframe':'1h','start':START,'end':END})
    assert download.is_success and client.get(endpoint,params=body).json()==page
    assert client.get('/lab/datasets').json()==[]

    def rotate(url,_):
        if url.endswith('/instrument'):
            lab_access.lock(path);lab_access.unlock(path,PASSWORD)

    adapter,_=source(monkeypatch,rotate)
    request=client.post(endpoint+'/capture',json=body)
    assert request.status_code==422 and 'sesión' in request.text
    assert client.get(endpoint,params=body).json()==page


def test_capture_rejects_snapshot_outside_request(monkeypatch):
    adapter,_=source(monkeypatch)
    with pytest.raises(LabError,match='verificables'):
        capture_rule_observation(SYMBOL,adapter=adapter,clock=lambda:NOW+1)
