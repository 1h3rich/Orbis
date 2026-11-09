"""Owned SQLCipher books: exact replay, backups, epoch changes and tampering."""
from app.lab.schema import CURRENT_SCHEMA_VERSION
import json
import sqlite3
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.backup import verify_lab_backup
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_profile import PerpProfile,read_profile
from app.lab.perp_rules import RuleObservation,RuleObservationStore
from app.lab.perp_store import PerpDatasetStore
from app.lab.secure_backup import create_secure_lab_backup,verify_secure_lab_backup
from app.lab.store import LabStore,content_hash,encoded
from tests.perp_simulation_fixture import dataset,definition,profile
from tests.test_perp_rules import shifted


PASSWORD="owned 'perpetual' archive password"


def seeded(tmp_path,monkeypatch,*,ephemeral=False):
    path=tmp_path/'lab.db'
    if ephemeral:
        lab_access.create(path,PASSWORD);lab=LabStore(path)
    else:lab=LabStore(path,passphrase=PASSWORD)
    strategy=definition();version=lab.create_strategy('Owned LONG',strategy)
    data=PerpDatasetStore(lab).save(dataset(monkeypatch))
    configured=profile(strategy).payload();configured['strategy_version_id']=version['version_id']
    archive=PerpExperiments(lab);saved=archive.save_profile(PerpProfile.model_validate(configured))
    return lab,archive,data,saved


def test_encrypted_replay_dedup_backup_and_reopen_preserve_every_result(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch)
    saved=archive.run(data['id'],configured['id'])
    assert saved['verification']=='COMPUTED_AND_SAVED'
    assert archive.run(data['id'],configured['id'])['id']==saved['id']
    bundle=archive.read(saved['id'])
    assert bundle['run']['verification']=='REPLAY_VERIFIED'
    assert bundle['execution']['result']['metrics']['cash']=='1193.826000000000000000'
    assert archive.page()['items'][0]['verification']=='HASH_AND_ORIGINALS_ONLY'
    backup=create_secure_lab_backup(lab,'owned independent perp copy key')
    report=verify_secure_lab_backup(lab.path.parent/'lab-backups'/backup['name'],'owned independent perp copy key')
    assert report['schema_version']==CURRENT_SCHEMA_VERSION
    assert report['counts']['perp_profiles']==report['counts']['perp_runs']==1
    reopened=PerpExperiments(LabStore(lab.path,passphrase=PASSWORD))
    assert reopened.read(saved['id'])==bundle
    assert lab.path.read_bytes()[:16]!=b'SQLite format 3\x00'


def alternate_capture(lab, data, *, symbol=None):
    _, original = PerpDatasetStore(lab).read(data['id'])
    captured = RuleObservation.from_dataset(original)
    def change(raw):
        raw['specification']['min_notional']='100000'
        raw['rules']['risk_brackets'][0].update(initial_margin_rate='0.4',max_leverage=2)
        if symbol is not None:
            raw['symbol']=symbol
            for field in ('rules','specification'):
                raw[field]['instrument']=symbol.replace('/','_').replace(':PERP','_Perp')
    return RuleObservationStore(lab).save(shifted(captured,captured.observed_at_ns+1,update=change))


@pytest.mark.parametrize('engine',['MARKET','LIMIT','PARTIAL_MARKET','PARTIAL_LIMIT'])
def test_rule_comparison_replays_original_and_isolates_alternate_rules(tmp_path,monkeypatch,engine):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch)
    body=configured['profile']
    if 'LIMIT' in engine:
        body.update(schema='orbis.lab.perp.profile.v2',execution='LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE',
                    end_action='CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE',
                    entry_order={'reference':'CLOSED_SIGNAL_TRADE_CLOSE','offset':'0','valid_bars':3})
    if 'PARTIAL' in engine:
        body.update(schema='orbis.lab.perp.profile.v3',partial_targets=[{'target':'0.02','fraction':'0.5'}])
    configured=archive.save_profile(read_profile(body))
    saved=archive.run(data['id'],configured['id'])
    before=archive.read(saved['id'])
    alternative=alternate_capture(lab,data)
    material=archive.compare_rules(saved['id'],alternative['hash'])
    assert material['scope']=='EXPERIMENTAL_CAPTURED_RULES_NOT_EFFECTIVE_HISTORY'
    assert material['original_run']=={'id':saved['id'],'hash':saved['hash']}
    assert material['dataset_hash']==data['hash'] and material['profile_hash']==configured['hash']
    assert material['original']['metrics']==before['execution']['result']['metrics']
    assert material['alternative']['observation_hash']==alternative['hash']
    assert material['alternative']['observation']==alternative['observation']
    assert material['original']['metrics']['trades']==1 and material['alternative']['metrics']['trades']==0
    assert material['original']['result_hash']!=material['alternative']['result_hash']
    difference=Decimal('1000')-Decimal(material['original']['metrics']['final_equity'])
    assert Decimal(material['final_equity_difference'])==difference
    assert material['hash']==content_hash({key:value for key,value in material.items() if key!='hash'})
    assert archive.read(saved['id'])==before and archive.page()['total']==1
    assert PerpExperiments(LabStore(lab.path,passphrase=PASSWORD)).compare_rules(
        saved['id'],alternative['hash'])==material


def test_rule_comparison_enforces_origin_symbol_integrity_and_unlock_epoch(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch,ephemeral=True)
    saved=archive.run(data['id'],configured['id'])
    alternative=alternate_capture(lab,data)
    other=alternate_capture(lab,data,symbol='ETH/USDT:PERP')
    app=FastAPI()
    app.include_router(create_lab_router(lab.path))
    client=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',31000))
    url=f"/lab/perp/runs/{saved['id']}/compare-rules"
    body={'observation_hash':alternative['hash']}
    assert client.post(url,json=body,headers={'Origin':'https://outside.invalid'}).status_code==403
    assert client.post(url,json={**body,'leverage':'2'}).status_code==422
    assert client.post(url,json={'observation_hash':'a'*64}).status_code==422
    foreign=client.post(url,json={'observation_hash':other['hash']})
    assert foreign.status_code==422 and 'otro mercado' in foreign.text
    result=client.post(url,json=body)
    assert result.status_code==200 and result.headers['cache-control']=='no-store',result.text
    lab_access.lock(lab.path)
    assert client.post(url,json=body).status_code==423
    lab_access.unlock(lab.path,PASSWORD)
    from app.lab.perp_experiments import simulate_perpetual as original
    def compute(*args,**kwargs):
        computed=original(*args,**kwargs)
        if kwargs.get('rule_observation') is not None:
            lab_access.lock(lab.path);lab_access.unlock(lab.path,PASSWORD)
        return computed
    monkeypatch.setattr('app.lab.perp_experiments.simulate_perpetual',compute)
    rotated=client.post(url,json=body)
    assert rotated.status_code==422 and 'sesión LAB cambió' in rotated.text
    with lab.connect() as db:
        db.execute("UPDATE perp_rule_observations SET payload_json='{}' WHERE hash=?",(alternative['hash'],))
    tampered=client.post(url,json=body)
    assert tampered.status_code==422 and 'no verificable' in tampered.text
    assert archive.page()['total']==1


def test_rule_window_reports_only_points_for_the_requested_subwindow(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch,ephemeral=True)
    saved=archive.run(data['id'],configured['id'],entry_start_index=1,end_index=3)
    original=archive.read(saved['id'])
    _,dataset=PerpDatasetStore(lab).read(data['id'])
    assumption=RuleObservation.from_dataset(dataset)
    start=dataset.trade.bars[1].t*10**9
    end=dataset.trade.bars[3].t*10**9
    store=RuleObservationStore(lab)
    def capture(at, *, altered=False, first=None):
        def values(payload):
            if altered:
                payload['specification']['min_notional']='90000'
            if first is not None:
                payload['rules']['fetched_at_ns']=str(first)
        return store.save(shifted(assumption,at,update=values))

    empty=archive.rules_in_window(saved['id'],saved['hash'])
    assert empty['within']['capture_count']==0 and empty['before'] is None
    assert empty['after']['hash']==empty['assumed']['hash'] and empty['assumed']['location']=='AFTER'
    before=capture(start-1)
    at_start=capture(start)
    capture(start,first=start-1)  # Same normalized values at the same completion time.
    different=capture(start+1,altered=True)
    capture(end-1)
    capture(end-1,altered=True)  # Distinct values at an indistinguishable instant.
    after=capture(end,altered=True)

    app=FastAPI();app.include_router(create_lab_router(lab.path))
    client=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',31000))
    url=f"/lab/perp/runs/{saved['id']}/rule-window"
    params={'run_hash':saved['hash']}
    assert client.get(url,params=params,headers={'Origin':'https://outside.invalid'}).status_code==403
    assert client.get(url,params={'run_hash':'f'*64}).status_code==422
    response=client.get(url,params=params)
    assert response.status_code==200 and response.headers['cache-control']=='no-store',response.text
    report=response.json()
    assert report['effective_history']=='UNVERIFIED' and report['scope']=='OBSERVED_AT_CAPTURE_ONLY'
    assert report['window']=={'from_ns':str(start),'to_ns':str(end)}
    assert report['before']['hash']==before['hash'] and report['after']['hash']==after['hash']
    within=report['within']
    assert (within['capture_count'],within['matching_instants'],within['different_instants'],
            within['ambiguous_instants'])==(5,1,1,1)
    assert within['first']['observed_at_ns']==at_start['observed_at_ns'] and within['first']['capture_count']==2
    assert within['first']['changes']==[] and within['last']['changes'] is None and within['last']['hash'] is None
    assert within['latest_difference']['hash']==different['hash']
    assert within['latest_difference']['changes']==['specification']
    assert archive.compare_rules(saved['id'],different['hash'])['alternative']['observation_hash']==different['hash']
    assert archive.read(saved['id'])==original and archive.page()['total']==1

    lab_access.lock(lab.path)
    assert client.get(url,params=params).status_code==423
    lab_access.unlock(lab.path,PASSWORD)
    assert client.get(url,params=params).json()==report
    with monkeypatch.context() as patch:
        patch.setattr('app.lab.perp_rules.WINDOW_CAP',3)
        assert client.get(url,params=params).status_code==422
    from app.lab.perp_experiments import rules_for_window
    def change_epoch(*args):
        result=rules_for_window(*args)
        lab_access.lock(lab.path);lab_access.unlock(lab.path,PASSWORD)
        return result
    with monkeypatch.context() as patch:
        patch.setattr('app.lab.perp_experiments.rules_for_window',change_epoch)
        assert 'sesión LAB cambió' in client.get(url,params=params).text
    with lab.connect() as db:
        db.execute("UPDATE perp_rule_observations SET payload_json='{}' WHERE hash=?",(different['hash'],))
    corrupt=client.get(url,params=params)
    assert corrupt.status_code==422 and 'no verificable' in corrupt.text


def test_lock_reunlock_with_same_key_cancels_old_computation(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch,ephemeral=True)
    from app.lab.perp_experiments import simulate_perpetual as original
    def compute(*args,**kwargs):
        result=original(*args,**kwargs)
        lab_access.lock(lab.path);lab_access.unlock(lab.path,PASSWORD)
        return result
    monkeypatch.setattr('app.lab.perp_experiments.simulate_perpetual',compute)
    with pytest.raises(LabError,match='cambió|bloqueó'):
        archive.run(data['id'],configured['id'])
    assert archive.page()['total']==0


def test_same_inode_original_edit_during_compute_is_rejected(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch)
    from app.lab.perp_experiments import simulate_perpetual as original
    def compute(*args,**kwargs):
        result=original(*args,**kwargs)
        with lab.connect() as db:
            db.execute('UPDATE strategy_versions SET definition_json=? WHERE id=?',
                (encoded(definition(stop_loss='0.04')),configured['profile']['strategy_version_id']))
        return result
    monkeypatch.setattr('app.lab.perp_experiments.simulate_perpetual',compute)
    with pytest.raises(LabError,match='Perfil'):
        archive.run(data['id'],configured['id'])
    with lab.connect() as db:assert db.execute('SELECT count(*) FROM perp_runs').fetchone()[0]==0


@pytest.mark.parametrize('rehash',[False,True])
def test_tampered_financial_result_is_rejected_even_with_new_digest(tmp_path,monkeypatch,rehash):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch)
    saved=archive.run(data['id'],configured['id'])
    with lab.connect() as db:
        raw=json.loads(db.execute('SELECT payload_json FROM perp_runs WHERE id=?',(saved['id'],)).fetchone()[0])
        raw['result']['metrics']['cash']='999999'
        db.execute('UPDATE perp_runs SET payload_json=?,hash=? WHERE id=?',
            (encoded(raw),content_hash(raw) if rehash else saved['hash'],saved['id']))
    with pytest.raises(LabError):archive.read(saved['id'])
    with pytest.raises(LabError):create_secure_lab_backup(lab,'owned independent perp copy key')


def test_schema10_to_current_is_additive_and_keeps_previous_books(tmp_path):
    path=tmp_path/'plain-owned.db';lab=LabStore(path)
    original=lab.create_strategy('Original',definition())
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE perp_optimization_variants')
        db.execute('DROP TABLE perp_optimizations')
        db.execute('DROP TABLE perp_rule_observations')
        db.execute('DROP TABLE perp_validations')
        db.execute('DROP TABLE perp_runs');db.execute('DROP TABLE perp_profiles')
        db.execute('PRAGMA user_version=10')
        rows=db.execute('SELECT * FROM strategy_versions').fetchall()
    assert verify_lab_backup(path)['schema_version']==10
    assert lab.version(original['version_id'])['definition']==definition()
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==CURRENT_SCHEMA_VERSION
        assert db.execute('SELECT * FROM strategy_versions').fetchall()==rows
    assert verify_lab_backup(path)['counts']['perp_runs']==0


def test_plain_storage_never_accepts_new_profile_or_run(tmp_path):
    lab=LabStore(tmp_path/'plain-owned.db');strategy=definition();version=lab.create_strategy('Owned',strategy)
    raw=profile(strategy).payload();raw['strategy_version_id']=version['version_id']
    with pytest.raises(LabError,match='cifrado'):
        PerpExperiments(lab).save_profile(PerpProfile.model_validate(raw))


def test_api_origin_strict_dto_replay_and_locked_access(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch,ephemeral=True)
    app=FastAPI();app.include_router(create_lab_router(lab.path))
    with TestClient(app) as client:
        body={'dataset_id':data['id'],'profile_id':configured['id']}
        assert client.post('/lab/perp/runs',json=body,headers={'Origin':'https://remote.invalid'}).status_code==403
        assert client.post('/lab/perp/runs',json={**body,'entry_start_index':True}).status_code==422
        result=client.post('/lab/perp/runs',json=body);assert result.status_code==201,result.text
        identifier=result.json()['id'];bundle=client.get('/lab/perp/runs/'+identifier)
        assert bundle.status_code==200 and bundle.json()['run']['verification']=='REPLAY_VERIFIED'
        assert client.get('/lab/perp/runs/'+identifier+'/export').json()==bundle.json()
        assert client.get('/lab/perp/runs?limit=1').json()['total']==1
        lab_access.lock(lab.path)
        assert client.get('/lab/perp/runs/'+identifier).status_code==423
        assert client.post('/lab/perp/runs',json=body).status_code==423


def test_missing_library_is_empty_without_creating_files(tmp_path):
    path=tmp_path/'missing.db';app=FastAPI();app.include_router(create_lab_router(path))
    with TestClient(app) as client:
        assert client.get('/lab/perp/profiles').json()==[]
        assert client.get('/lab/perp/runs').json()['total']==0
    assert not path.exists()


def test_reauthentication_during_replay_prevents_returning_old_session_result(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch,ephemeral=True)
    saved=archive.run(data['id'],configured['id'])
    from app.lab.perp_experiments import simulate_perpetual as original
    def compute(*args,**kwargs):
        result=original(*args,**kwargs)
        lab_access.lock(lab.path);lab_access.unlock(lab.path,PASSWORD)
        return result
    monkeypatch.setattr('app.lab.perp_experiments.simulate_perpetual',compute)
    with pytest.raises(LabError,match='cambió|bloqueó'):archive.read(saved['id'])


def test_saved_run_pagination_keeps_older_profiles_accessible(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch)
    identifiers=[]
    for mode,path in [('ISOLATED','O_H_L_C'),('CROSS','O_H_L_C'),('ISOLATED','O_L_H_C')]:
        raw=configured['profile'].copy();raw.update(margin_mode=mode,trade_path=path)
        selected=archive.save_profile(PerpProfile.model_validate(raw))
        identifiers.append(archive.run(data['id'],selected['id'])['id'])
    first,last=archive.page(limit=2),archive.page(offset=2,limit=2)
    assert first['total']==last['total']==3 and len(first['items'])==2 and len(last['items'])==1
    assert {i['id'] for i in first['items']+last['items']}==set(identifiers)
    assert archive.read(last['items'][0]['id'])['run']['verification']=='REPLAY_VERIFIED'


def test_api_builds_frozen_cross_profile_from_immutable_version(tmp_path,monkeypatch):
    lab,archive,data,configured=seeded(tmp_path,monkeypatch,ephemeral=True)
    app=FastAPI();app.include_router(create_lab_router(lab.path))
    with TestClient(app) as client:
        body={'version_id':configured['profile']['strategy_version_id'],'margin_mode':'CROSS',
              'leverage':'5','trade_path':'O_L_H_C','mark_path':'O_H_L_C'}
        made=client.post('/lab/perp/profiles',json=body);assert made.status_code==201,made.text()
        assert made.json()['profile']['strategy_hash']==configured['profile']['strategy_hash']
        response=client.post('/lab/perp/runs',json={'dataset_id':data['id'],'profile_id':made.json()['id']})
        assert response.status_code==201,response.text()
        bundle=client.get('/lab/perp/runs/'+response.json()['id']).json()
        assert bundle['run']['profile']['margin_mode']=='CROSS' and PASSWORD not in json.dumps(bundle)


def test_compare_perp_runs_replays_each_version_and_aligns_exact_equity(tmp_path,monkeypatch):
    lab,archive,data,base_profile=seeded(tmp_path,monkeypatch)
    base=archive.run(data['id'],base_profile['id'])
    strategy=lab.version(base_profile['profile']['strategy_version_id'])
    variant=definition(take_profit='0.04')
    version=lab.add_version(strategy['strategy_id'],variant)
    second=profile(variant,strategy_version_id=version['version_id'])
    second_profile=archive.save_profile(second)
    alternate=archive.run(data['id'],second_profile['id'])
    pair=(base['id'],base['hash'],alternate['id'],alternate['hash'])
    report=archive.compare_runs(*pair)
    assert report['difference_blockers']==[] and report['environment']=='LAB' and report['execution_authority']=='NONE'
    assert report['strategy_fields_changed']==['take_profit'] and report['execution_parameters_changed']==[]
    assert report['base']['strategy_version_id']!=report['other']['strategy_version_id']
    assert report['base']['dataset_hash']==report['other']['dataset_hash']==data['hash']
    left,right=(report[key]['metrics'] for key in ('base','other'))
    assert Decimal(report['deltas']['final_equity'])==Decimal(right['final_equity'])-Decimal(left['final_equity'])
    assert Decimal(report['deltas']['final_equity'])!=0
    assert report['deltas']['trades']==right['trades']-left['trades']
    assert report['equity']['interval_seconds']==3600
    assert len(report['equity']['base'])==len(report['equity']['other'])==5
    assert report['equity']['base'][-1]==left['final_equity'] and report['equity']['other'][-1]==right['final_equity']
    for key,run in (('base',base),('other',alternate)):
        bundle=archive.read(run['id'])
        samples=report['equity'][key]
        for index,sample in enumerate(samples[:-1]):
            cut=(report['equity']['start_t']+index*3600)*10**9
            history=[row['equity'] for row in bundle['execution']['result']['equity'] if int(row['time_ns'])<=cut]
            expected=history[-1] if history else bundle['strategy']['definition']['initial_capital']
            assert Decimal(sample)==Decimal(expected)
    assert report['hash']==content_hash({key:value for key,value in report.items() if key!='hash'})
    assert PerpExperiments(LabStore(lab.path,passphrase=PASSWORD)).compare_runs(*pair,expected_hash=report['hash'])==report
    with pytest.raises(LabError,match='comparación cambió'):
        archive.compare_runs(*pair,expected_hash='0'*64)
    reversed_report=archive.compare_runs(*pair[2:],*pair[:2])
    assert Decimal(reversed_report['deltas']['final_equity'])==-Decimal(report['deltas']['final_equity'])
    assert archive.page()['total']==2


def test_perp_compare_labels_incompatible_runs_and_rejects_edited_result(tmp_path,monkeypatch):
    lab,archive,data,base_profile=seeded(tmp_path,monkeypatch,ephemeral=True)
    base=archive.run(data['id'],base_profile['id'])
    other_profile=archive.save_profile(read_profile({**base_profile['profile'],'mark_path':'O_L_H_C'}))
    other=archive.run(data['id'],other_profile['id'],end_index=3)
    app=FastAPI();app.include_router(create_lab_router(lab.path))
    client=TestClient(app,base_url='http://127.0.0.1')
    path='/lab/perp/runs/compare'
    pair={'base_id':base['id'],'base_hash':base['hash'],'other_id':other['id'],'other_hash':other['hash']}
    assert client.post(path,json=pair,headers={'Origin':'https://outside.invalid'}).status_code==403
    assert client.post(path,json={**pair,'other_id':base['id']}).status_code==422
    response=client.post(path,json=pair)
    assert response.is_success and response.headers['cache-control']=='no-store',response.text
    result=response.json()
    assert result['difference_blockers']==['WINDOW','SIMULATION_ASSUMPTIONS']
    assert result['deltas'] is None and result['equity'] is None
    assert result['base']['metrics']['final_equity'] and result['other']['metrics']['final_equity']
    with lab.connect() as db:
        row=db.execute('SELECT payload_json FROM perp_runs WHERE id=?',(other['id'],)).fetchone()
        payload=json.loads(row['payload_json'])
        payload['result']['metrics']['final_equity']='9000'
        digest=content_hash(payload)
        db.execute('UPDATE perp_runs SET payload_json=?, hash=? WHERE id=?',(encoded(payload),digest,other['id']))
    assert client.post(path,json=pair).status_code==422
    altered=client.post(path,json={**pair,'other_hash':digest})
    assert altered.status_code==422 and 'no reproduce' in altered.json()['detail']
    lab_access.lock(lab.path)
    assert client.post(path,json=pair).status_code==423


def test_perp_compare_revokes_replay_when_lab_locks(tmp_path,monkeypatch):
    from app.lab import perp_experiments

    lab,archive,data,configured=seeded(tmp_path,monkeypatch,ephemeral=True)
    base=archive.run(data['id'],configured['id'])
    other=archive.run(data['id'],configured['id'],end_index=3)
    simulate=perp_experiments.simulate_perpetual

    def lock_after_replay(*args,**kwargs):
        computed=simulate(*args,**kwargs)
        lab_access.lock(lab.path)
        return computed

    monkeypatch.setattr(perp_experiments,'simulate_perpetual',lock_after_replay)
    with pytest.raises(LabError):
        archive.compare_runs(base['id'],base['hash'],other['id'],other['hash'])


@pytest.mark.parametrize('direction,entry,partials', [
    ('LONG', 'MARKET', False), ('SHORT', 'MARKET', False),
    ('LONG', 'LIMIT', True), ('SHORT', 'LIMIT', True),
])
def test_cost_comparison_keeps_the_model_and_recomputes_exact_costs(tmp_path,monkeypatch,direction,entry,partials):
    from app.lab.perp_simulation import simulate_perpetual

    lab = LabStore(tmp_path/'lab.db',passphrase=PASSWORD)
    strategy = definition(direction)
    version = lab.create_strategy('Owned '+direction,strategy)
    market = dataset(monkeypatch,direction)
    data = PerpDatasetStore(lab).save(market)
    original_profile = profile(strategy,strategy_version_id=version['version_id']).payload()
    if entry == 'LIMIT':
        original_profile.update(schema='orbis.lab.perp.profile.v2',
            execution='LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE',
            end_action='CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE',
            entry_order={'reference':'CLOSED_SIGNAL_TRADE_CLOSE','offset':'0','valid_bars':3})
    if partials:
        original_profile.update(schema='orbis.lab.perp.profile.v3',
                                partial_targets=[{'target':'0.02','fraction':'0.5'}])
    archive = PerpExperiments(lab)
    configured = archive.save_profile(read_profile(original_profile))
    saved = archive.run(data['id'],configured['id'])
    bundle = archive.read(saved['id'])
    args = (saved['id'],saved['hash'],'0.006','0.004')
    report = archive.compare_costs(*args)
    assert report['environment'] == 'LAB' and report['execution_authority'] == 'NONE'
    assert report['direction'] == direction and report['dataset_hash'] == data['hash']
    assert report['original_run'] == {'id':saved['id'],'hash':saved['hash']}
    assert report['strategy_version_id'] == version['version_id']
    assert report['controls']['execution'] == original_profile['execution']
    assert report['controls'].get('entry_order') == original_profile.get('entry_order')
    assert report['controls'].get('partial_targets') == original_profile.get('partial_targets')
    assert report['base']['result_hash'] == content_hash(bundle['execution']['result'])
    alternative = definition(direction,fee_rate='0.006',slippage_rate='0.004')
    alternative_profile = read_profile({**original_profile,'strategy_hash':content_hash(alternative)})
    computed = simulate_perpetual(market,alternative_profile,alternative,**bundle['execution']['window'])
    assert report['base']['window'] == report['alternative']['window']
    assert report['alternative']['result_hash'] == content_hash(computed)
    fills = [row for row in computed['journal'] if row['kind'] == 'FILL']
    assert len(fills) >= 2
    fees = sum(abs(Decimal(row['size_delta'])) * Decimal(row['price']) * Decimal('0.006') for row in fills)
    assert fees == Decimal(report['alternative']['metrics']['fees_paid'])
    assert Decimal(report['deltas']['fees_paid']) == fees - Decimal(report['base']['metrics']['fees_paid']) > 0
    assert Decimal(report['deltas']['final_equity']) < 0
    assert report['hash'] == content_hash({key:value for key,value in report.items() if key != 'hash'})
    assert PerpExperiments(LabStore(lab.path,passphrase=PASSWORD)).compare_costs(
        *args,expected_hash=report['hash']) == report
    with pytest.raises(LabError,match='comparación cambió'):
        archive.compare_costs(*args,expected_hash='0'*64)
    same = archive.compare_costs(saved['id'],saved['hash'],'0.001000','0.00')
    assert same['base'] == same['alternative'] and not any(Decimal(value) for value in same['deltas'].values())
    assert archive.read(saved['id']) == bundle
    with lab.connect() as db:
        for table in ('perp_runs','perp_profiles','strategy_versions'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1


def test_cost_comparison_with_halted_open_position_withholds_differences(tmp_path,monkeypatch):
    _,archive,data,configured = seeded(tmp_path,monkeypatch)
    saved = archive.run(data['id'],configured['id'])
    report = archive.compare_costs(saved['id'],saved['hash'],'0.001','0.99')
    assert report['base']['status'] == 'COMPLETE'
    alternative = report['alternative']
    assert alternative['status'] == 'HALTED_EXECUTION' and alternative['metrics']['open_position']
    assert alternative['halt_reason'].startswith('EXIT_PRE_TRADE_CANCELLED:')
    assert int(alternative['window']['processed_through_ns']) < int(alternative['window']['requested_end_ns'])
    assert report['deltas'] is None


def test_cost_comparison_api_checks_integrity_input_origin_and_session(tmp_path,monkeypatch):
    from app.lab import perp_experiments

    lab,archive,data,configured = seeded(tmp_path,monkeypatch,ephemeral=True)
    saved = archive.run(data['id'],configured['id'])
    app = FastAPI()
    app.include_router(create_lab_router(lab.path))
    client = TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',31000))
    path = f"/lab/perp/runs/{saved['id']}/compare-costs"
    body = {'run_hash':saved['hash'],'fee_rate':'0.003','slippage_rate':'0.001'}
    assert client.post(path,json=body,headers={'Origin':'https://outside.invalid'}).status_code == 403
    for change in ({'fee_rate':0.001},{'fee_rate':'1'},{'slippage_rate':'-0.001'},
                   {'fee_rate':'NaN'},{'fee_rate':'1e-41'},{'run_hash':'0'*64},{'leverage':'2'}):
        assert client.post(path,json={**body,**change}).status_code == 422,change
    response = client.post(path,json=body)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store',response.text
    assert client.post(path,json={**body,'expected_hash':response.json()['hash']}).json() == response.json()
    assert client.post(path,json={**body,'expected_hash':'f'*64}).status_code == 422
    lab_access.lock(lab.path)
    assert client.post(path,json=body).status_code == 423
    lab_access.unlock(lab.path,PASSWORD)
    original = perp_experiments.simulate_perpetual

    def rotate_on_alternative(market,execution,strategy,**window):
        computed = original(market,execution,strategy,**window)
        if strategy['fee_rate'] == '0.003':
            lab_access.lock(lab.path)
            lab_access.unlock(lab.path,PASSWORD)
        return computed

    with monkeypatch.context() as rotated:
        rotated.setattr(perp_experiments,'simulate_perpetual',rotate_on_alternative)
        assert client.post(path,json=body).status_code == 422
    with lab.connect() as db:
        row = db.execute('SELECT payload_json FROM perp_runs WHERE id=?',(saved['id'],)).fetchone()
        payload = json.loads(row['payload_json'])
        payload['result']['metrics']['fees_paid'] = '0'
        digest = content_hash(payload)
        db.execute('UPDATE perp_runs SET payload_json=?,hash=? WHERE id=?',(encoded(payload),digest,saved['id']))
    assert client.post(path,json=body).status_code == 422
    tampered = client.post(path,json={**body,'run_hash':digest})
    assert tampered.status_code == 422 and 'no reproduce' in tampered.json()['detail']


@pytest.mark.parametrize("direction",["LONG","SHORT"])
def test_margin_grid_replays_risk_and_keeps_original_strategy_and_window(tmp_path,monkeypatch,direction):
    from app.lab.perp_dataset import read_perp_dataset

    lab = LabStore(tmp_path/"lab.db",passphrase=PASSWORD)
    strategy = definition(direction)
    version = lab.create_strategy("Owned "+direction,strategy)
    raw = dataset(monkeypatch,direction).payload()
    raw["trade"]["bars"][2].update(o="100",h="101",l="99",c="100")
    if direction == "LONG":
        raw["mark"]["bars"][2].update(o="100",h="100",l="50",c="100")
    else:
        raw["mark"]["bars"][2].update(o="100",h="150",l="100",c="100")
    data = PerpDatasetStore(lab).save(read_perp_dataset(raw))
    archive = PerpExperiments(lab)
    original = archive.save_profile(profile(strategy,strategy_version_id=version["version_id"],
                                           trade_path="O_L_H_C",mark_path="O_L_H_C"))
    saved = archive.run(data["id"],original["id"],entry_start_index=1)
    args = saved["id"],saved["hash"]
    bundle = archive.read(saved["id"])
    report = archive.compare_margin(*args,["6","1","5"],["CROSS","ISOLATED"])
    assert report["environment"] == "LAB" and report["execution_authority"] == "NONE"
    assert report["original_run"] == {"id":args[0],"hash":args[1]}
    assert report["strategy_hash"] == content_hash(strategy)
    assert report["strategy_version_id"] == version["version_id"] and report["dataset_hash"] == data["hash"]
    assert report["controls"]["trade_path"] == report["controls"]["mark_path"] == "O_L_H_C"
    assert report["controls"]["fee_rate"] == strategy["fee_rate"]
    assert len(report["scenarios"]) == 6 and sum(row["base"] for row in report["scenarios"]) == 1
    assert report["scenarios"][0]["result_hash"] == content_hash(bundle["execution"]["result"])
    outcomes = {(row["margin_mode"],row["leverage"]):row for row in report["scenarios"]}
    for mode in ("ISOLATED","CROSS"):
        low,high,blocked = (outcomes[(mode,value)] for value in ("1","5","6"))
        assert low["complete"] and low["liquidation"] is None and low["metrics"]["trades"] == 1
        assert high["complete"] and high["liquidation"]["cause"] == "MARK"
        assert high["metrics"]["liquidated"] and high["liquidation"]["time_ns"].isdigit()
        mark = Decimal(high["liquidation"]["mark_price"])
        assert (50 < mark < 100) if direction == "LONG" else (100 < mark < 150)
        assert blocked["metrics"]["trades"] == 0 and blocked["liquidation"] is None
        assert blocked["entry_restrictions"] and Decimal(blocked["metrics"]["final_equity"]) == 1000
    isolated,cross = (outcomes[(mode,"5")] for mode in ("ISOLATED","CROSS"))
    assert int(isolated["liquidation"]["time_ns"]) < int(cross["liquidation"]["time_ns"])
    assert Decimal(isolated["metrics"]["final_equity"]) > Decimal(cross["metrics"]["final_equity"]) == 0
    assert all(row["window"] == bundle["execution"]["result"]["window"] for row in report["scenarios"])
    assert report["hash"] == content_hash({key:value for key,value in report.items() if key != "hash"})
    assert archive.read(saved["id"]) == bundle and archive.page()["total"] == 1 and len(archive.profiles()) == 1
    reopened = PerpExperiments(LabStore(lab.path,passphrase=PASSWORD))
    assert reopened.compare_margin(*args,["5e0","6.00","1.0"],["ISOLATED","CROSS"],expected_hash=report["hash"]) == report


def test_margin_grid_api_rejects_stale_review_and_revokes_old_lab_session(tmp_path,monkeypatch):
    from app.lab import perp_experiments

    lab,archive,data,configured = seeded(tmp_path,monkeypatch,ephemeral=True)
    saved = archive.run(data["id"],configured["id"])
    app = FastAPI()
    app.include_router(create_lab_router(lab.path))
    client = TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",31000))
    path = f"/lab/perp/runs/{saved['id']}/compare-margin"
    body = {"run_hash":saved["hash"],"leverages":["2","5"],"margin_modes":["ISOLATED","CROSS"]}
    assert client.post(path,json=body,headers={"Origin":"https://outside.invalid"}).status_code == 403
    for changes in ({"leverages":[2]}, {"leverages":["1","2","3","4"]}, {"leverages":["0.5"]},
                    {"leverages":["NaN"]}, {"margin_modes":["UNKNOWN"]},{"run_hash":"0"*64}):
        assert client.post(path,json={**body,**changes}).status_code == 422,changes
    response = client.post(path,json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store",response.text
    report = response.json()
    assert client.post(path,json={**body,"expected_hash":report["hash"]}).json() == report
    assert client.post(path,json={**body,"expected_hash":"0"*64}).status_code == 422
    with monkeypatch.context() as limited:
        limited.setattr(perp_experiments,"MAX_MARGIN_WORK",1)
        over = client.post(path,json=body)
        assert over.status_code == 422 and "Reduce" in over.text
    original = perp_experiments.simulate_perpetual

    def rotate(dataset,config,strategy,**window):
        computed = original(dataset,config,strategy,**window)
        if config.leverage == "2":
            lab_access.lock(lab.path)
            lab_access.unlock(lab.path,PASSWORD)
        return computed

    with monkeypatch.context() as rotated:
        rotated.setattr(perp_experiments,"simulate_perpetual",rotate)
        result = client.post(path,json=body)
        assert result.status_code == 422 and "sesión LAB cambió" in result.text
    lab_access.lock(lab.path)
    assert client.post(path,json=body).status_code == 423
