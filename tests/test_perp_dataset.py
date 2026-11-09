"""Exact independent public datasets, temporal coverage and encrypted recovery."""
from app.lab.schema import CURRENT_SCHEMA_VERSION
from copy import deepcopy
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.access import lab_access
from app.lab.api import create_lab_router
from app.lab.backup import verify_lab_backup
from app.lab.engine import LabError,validate_candles
from app.lab.perp_dataset import prepare_perp_dataset,read_perp_dataset
from app.lab.perp_store import PerpDatasetStore
from app.lab.secure_backup import create_secure_lab_backup,verify_secure_lab_backup
from app.lab.store import LabStore,content_hash
from app.marketdata.adapters.grvt import GrvtAdapter,GrvtHistoryError
from tests.perp_dataset_fixture import START,END,NOW,SYMBOL,INSTRUMENT,PASSWORD,candle,source,prepared,rules


def test_mark_pagination_is_distinct_from_trade_and_has_no_invented_volume():
    calls=[]
    def post(url,body):
        calls.append(deepcopy(body));assert body['type']=='MARK'
        indices=(3,2) if not body['cursor'] else (2,1,0)
        return {'result':[candle(i,kind='MARK') for i in indices],
                'next':'older' if not body['cursor'] else ''}
    rows=GrvtAdapter(http_post=post).get_mark_history(SYMBOL,'1h',START,END,now=END)
    assert rows[0]=={'t':START,'o':'107','h':'109','l':'105','c':'108'}
    assert [row['t'] for row in rows]==list(range(START,END,3600))
    assert all('v' not in row for row in rows)
    assert calls[0]['start_time']==str(START*10**9) and calls[0]['end_time']==str(END*10**9-1)
    assert calls[1]['cursor']=='older'


@pytest.mark.parametrize('change',[
    {'instrument':'ETH_USDT_Perp'}, {'open_time':str((START+1)*10**9)},
    {'open_time':'١٧٣٥٦٨٩٦٠٠٠٠٠٠٠٠٠٠٠'}, {'close_time':'9'*100},
    {'open_time':True}, {'close_time':str(START*10**9-1)},
    {'close_time':str((START+3600)*10**9+1)}, {'open':107.1},
    {'high':'0'}, {'low':'NaN'}, {'close':'1e999999'}, {'high':'1'},
])
def test_mark_rejects_imprecise_inconsistent_or_out_of_range_values(change):
    wrong={**candle(0,kind='MARK'),**change}
    adapter=GrvtAdapter(http_post=lambda *_:{'result':[wrong],'next':''})
    with pytest.raises(GrvtHistoryError):adapter.get_mark_history(SYMBOL,'1h',START,END,now=END)


@pytest.mark.parametrize('mode',['gap','cursor','changed','network'])
def test_mark_never_returns_partial_history(mode):
    calls=[]
    def post(_url,body):
        calls.append(body)
        if mode=='network':raise RuntimeError('offline')
        if mode=='gap':return {'result':[candle(3,kind='MARK')],'next':''}
        if len(calls)==1:return {'result':[candle(3,kind='MARK'),candle(2,kind='MARK')],'next':'x'}
        if mode=='cursor':return {'result':[candle(2,kind='MARK')],'next':'x'}
        return {'result':[{**candle(2,kind='MARK'),'close':'107'},candle(1,kind='MARK'),candle(0,kind='MARK')],'next':''}
    with pytest.raises(GrvtHistoryError):
        GrvtAdapter(http_post=post).get_mark_history(SYMBOL,'1h',START,END,now=END)


@pytest.mark.parametrize('symbol,timeframe,start,end,now',[
    ('BTC/USDT','1h',START,END,END),(SYMBOL,'1w',START,END,END),
    (SYMBOL,'1h',START,END,END-1),(SYMBOL,'1h',True,END,END),
    (SYMBOL,'1h',START,START+5001*3600,START+5001*3600),
])
def test_mark_invalid_requests_do_not_reach_network(symbol,timeframe,start,end,now):
    adapter=GrvtAdapter(http_post=lambda *_:pytest.fail('No network expected'))
    with pytest.raises(GrvtHistoryError):adapter.get_mark_history(symbol,timeframe,start,end,now=now)


def test_composed_dataset_uses_real_adapter_contracts_and_exact_namespaces(monkeypatch):
    adapter,calls=source(monkeypatch)
    data=prepare_perp_dataset(SYMBOL,'1h',START,END,adapter=adapter,clock=lambda:NOW)
    payload=data.payload()
    assert payload['trade']['bars'][0]['c']=='101'
    assert payload['mark']['bars'][0]['c']=='108'
    assert [body.get('type') for url,body in calls if url.endswith('/kline')]==['TRADE','MARK']
    assert len(calls)==5
    assert payload['risk_rules_scope']=='CURRENT_SNAPSHOT_NOT_HISTORICAL'
    assert payload['historical_execution_model']=='NOT_RUN'
    assert payload['captured_start_ns']==str(NOW)
    assert payload['rules']['fetched_at_ns']==str(NOW)
    assert payload['funding'][0]['funding_time']==str((START-3600)*10**9)
    assert payload['funding'][1]['funding_time']==str(START*10**9)
    assert payload['funding'][-1]['funding_time']==str((END-3600)*10**9)
    assert payload['funding'][1]['funding_rate']=='-0.02'  # percent points, no conversion here
    assert read_perp_dataset(json.loads(json.dumps(payload))).payload()==payload


@pytest.mark.parametrize('kind', ['mark_gap','funding_gap','funding_duplicate','funding_anchor',
    'funding_future','snapshot_time','rules_scope','kind','max_size','float','namespace'])
def test_composed_archive_rejects_broken_provenance_or_coverage(monkeypatch,kind):
    payload=prepared(monkeypatch).payload()
    if kind=='mark_gap':payload['mark']['bars'].pop(1)
    elif kind=='funding_gap':payload['funding'].pop(2)
    elif kind=='funding_duplicate':payload['funding'].insert(2,payload['funding'][1])
    elif kind=='funding_anchor':payload['funding'].pop(0)
    elif kind=='funding_future':payload['funding'][-1]['funding_time']=str(END*10**9)
    elif kind=='snapshot_time':payload['rules']['fetched_at_ns']=str(NOW-1)
    elif kind=='rules_scope':payload['risk_rules_scope']='HISTORICAL_VERIFIED'
    elif kind=='kind':payload['mark']['price_kind']='TRADE'
    elif kind=='max_size':payload['rules']['max_position_size']='101'
    elif kind=='float':payload['rules']['risk_brackets'][0]['initial_margin_rate']=0.02
    elif kind=='namespace':payload['trade']['source']='grvt-perp-mark-public'
    with pytest.raises(LabError):read_perp_dataset(payload)


def test_margin_adapter_does_not_mask_float_maximum_as_decimal_text():
    adapter=GrvtAdapter(http_post=lambda *_:{**rules(),'max_position_size':100.1})
    assert adapter.get_margin_rules(SYMBOL) is None


def test_encrypted_dataset_roundtrip_and_backup_preserve_exact_inputs(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';lab_access.create(path,PASSWORD);lab=LabStore(path)
    data=prepared(monkeypatch);archive=PerpDatasetStore(lab)
    saved=archive.save(data)
    assert archive.save(data)==saved and archive.list()==[saved]
    assert saved['trade_count']==4 and saved['mark_count']==4 and saved['funding_count']==4
    assert saved['component_hashes']['trade']!=saved['component_hashes']['mark']
    assert lab.datasets()==[]
    backup=create_secure_lab_backup(lab,'independent backup test passphrase')
    report=verify_secure_lab_backup(path.parent/'lab-backups'/backup['name'],'independent backup test passphrase')
    assert report['schema_version']==CURRENT_SCHEMA_VERSION and report['counts']['perp_datasets']==1
    assert path.read_bytes()[:16]!=b'SQLite format 3\x00'
    lab_access.lock(path)
    lab_access.unlock(path,PASSWORD)
    header,loaded=PerpDatasetStore(LabStore(path)).read(saved['id'])
    assert header['hash']==content_hash(data.payload()) and loaded.payload()==data.payload()


def test_migration_9_to_10_is_additive_and_does_not_reinterpret_old_data(tmp_path):
    path=tmp_path/'lab.db';lab=LabStore(path)
    bars,_=validate_candles([{'t':START+i*3600,'o':'100','h':'101','l':'99','c':'100','v':'10'} for i in range(4)],'1h',now=END)
    original=lab.save_dataset('BTC/USDT','1h','csv',bars,[])
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE perp_optimization_variants')
        db.execute('DROP TABLE perp_optimizations')
        db.execute('DROP TABLE perp_rule_observations')
        db.execute('DROP TABLE perp_validations')
        db.execute('DROP TABLE perp_runs');db.execute('DROP TABLE perp_profiles')
        db.execute('DROP TABLE perp_datasets');db.execute('PRAGMA user_version=9')
        before=db.execute('SELECT * FROM datasets').fetchall()
    assert verify_lab_backup(path)['schema_version']==9
    assert lab.dataset(original['id'])[0]['hash']==original['hash']
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==CURRENT_SCHEMA_VERSION
        assert db.execute('SELECT * FROM datasets').fetchall()==before
        assert db.execute('SELECT count(*) FROM perp_datasets').fetchone()[0]==0


@pytest.mark.parametrize('rehash',[False,True])
def test_backup_checks_payload_semantics_as_well_as_hash(tmp_path,monkeypatch,rehash):
    path=tmp_path/'lab.db';lab_access.create(path,PASSWORD);lab=LabStore(path)
    saved=PerpDatasetStore(lab).save(prepared(monkeypatch))
    with lab.connect() as db:
        row=db.execute('SELECT payload_json FROM perp_datasets WHERE id=?',(saved['id'],)).fetchone()
        raw=json.loads(row[0]);raw['mark']['price_kind']='TRADE'
        db.execute('UPDATE perp_datasets SET payload_json=?,hash=? WHERE id=?',
            (json.dumps(raw),content_hash(raw) if rehash else saved['hash'],saved['id']))
    with pytest.raises(LabError):PerpDatasetStore(lab).read(saved['id'])
    with pytest.raises(LabError):create_secure_lab_backup(lab,'independent backup test passphrase')


def api(path):
    app=FastAPI();app.include_router(create_lab_router(path));return TestClient(app)


def test_perp_api_download_export_lock_and_empty_boundary(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';client=api(path)
    body={'symbol':SYMBOL,'timeframe':'1h','start':START,'end':END}
    assert client.get('/lab/perp/datasets').json()==[] and not path.exists()
    assert client.post('/lab/perp/datasets/download',json=body).status_code==423
    assert client.post('/lab/create',json={'passphrase':PASSWORD}).status_code==200
    adapter,_=source(monkeypatch)
    monkeypatch.setattr('app.marketdata.adapters.grvt.GrvtAdapter',lambda:adapter)
    response=client.post('/lab/perp/datasets/download',json=body)
    assert response.status_code==200,response.text
    saved=response.json()
    assert client.get('/lab/perp/datasets').json()==[saved]
    assert client.get('/lab/datasets').json()==[]
    exported=client.get('/lab/perp/datasets/'+saved['id']+'/export')
    assert exported.status_code==200 and 'attachment;' in exported.headers['content-disposition']
    bundle=exported.json();assert bundle['dataset']['rules']['fetched_at_ns']==str(NOW)
    assert bundle['hash']==content_hash(bundle['dataset'])
    assert client.get('/lab/perp/datasets',headers={'Origin':'https://outside.invalid'}).status_code==403
    client.post('/lab/lock')
    assert client.get('/lab/perp/datasets').status_code==423
    assert client.get('/lab/perp/datasets/'+saved['id']+'/export').status_code==423
    assert client.post('/lab/unlock',json={'passphrase':PASSWORD}).status_code==200
    assert client.get('/lab/perp/datasets').json()==[saved]


def test_lock_during_external_download_does_not_commit_after_revocation(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';client=api(path);client.post('/lab/create',json={'passphrase':PASSWORD})
    def hook(url,body):
        if body.get('type')=='MARK':lab_access.lock(path)
    adapter,_=source(monkeypatch,hook)
    monkeypatch.setattr('app.marketdata.adapters.grvt.GrvtAdapter',lambda:adapter)
    response=client.post('/lab/perp/datasets/download',json={'symbol':SYMBOL,'timeframe':'1h','start':START,'end':END})
    assert response.status_code==422,response.text
    lab_access.unlock(path,PASSWORD)
    assert PerpDatasetStore(LabStore(path)).list()==[]


def test_reunlock_same_key_during_download_does_not_restore_old_request(tmp_path,monkeypatch):
    path=tmp_path/'lab.db';client=api(path);client.post('/lab/create',json={'passphrase':PASSWORD})
    def hook(url,body):
        if body.get('type')=='MARK':
            lab_access.lock(path);lab_access.unlock(path,PASSWORD)
    adapter,_=source(monkeypatch,hook)
    monkeypatch.setattr('app.marketdata.adapters.grvt.GrvtAdapter',lambda:adapter)
    response=client.post('/lab/perp/datasets/download',json={'symbol':SYMBOL,'timeframe':'1h','start':START,'end':END})
    assert response.status_code==422,response.text
    assert PerpDatasetStore(LabStore(path)).list()==[]


def test_last_risk_tier_may_omit_optional_cap(monkeypatch):
    payload=prepared(monkeypatch).payload()
    del payload['rules']['risk_brackets'][-1]['notional_cap']
    assert read_perp_dataset(payload).rules.risk_brackets[-1].notional_cap is None


@pytest.mark.parametrize('value',[True,1.0,'1'])
def test_interval_is_not_coerced_before_coverage_validation(monkeypatch,value):
    payload=prepared(monkeypatch).payload()
    payload['funding'][0]['funding_interval_hours']=value
    with pytest.raises(LabError):read_perp_dataset(payload)
