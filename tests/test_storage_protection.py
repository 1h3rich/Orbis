"""Protect owned files only: real cipher copies, identity and failure boundaries."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest
import sqlcipher3
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage_protection import StorageProtection,ProtectionError,active_plain_sessions
from app.core.storage_protection_api import create_storage_protection_router,StorageProtectionMiddleware
from app.security.loopback import LocalLoopbackMiddleware
from app.lab.access import lab_format,lab_access
from app.lab.store import LabStore
from app.lab.engine import validate_candles
from app.lab.backup import lab_runtime_lease
from app.automation.store import AutomationStore
from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.derivatives.paper_store import PaperPerpStore
from tests.test_paper_dca import rule

PWD="owned 'contraseña' protection 123"


def prepared(tmp_path,**options):
    paths={name:tmp_path/(name.lower()+'.db') for name in ('LAB','DCA','SPOT','GRVT')}
    lab=LabStore(paths['LAB'])
    bars,_=validate_candles([{'t':1735689600+i*3600,'o':'100','h':'101','l':'99','c':'100','v':'10'} for i in range(4)],'1h',now=1735704000)
    lab.save_dataset('BTC/USDT','1h','owned CSV',bars,[])
    AutomationStore(paths['DCA']).create_rule('Owned DCA',rule())
    with SpotStrategyPaper(paths['SPOT'],paths['LAB']).connect():pass
    PaperPerpStore(paths['GRVT']).create_account('Owned futures','1000','500','800','5',2,'0.001','0')
    control=StorageProtection(paths,readiness=lambda:None,**options)
    paused=[];control.attach(lambda:paused.append(True))
    return control,paths,paused


def rows(path,password=None):
    with closing((sqlcipher3 if password else sqlite3).connect(path)) as db:
        if password:db.execute("PRAGMA key='"+password.replace("'","''")+"'")
        tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name:db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables}


def test_all_four_actual_migrations_preserve_books_and_start_locked(tmp_path):
    c,paths,paused=prepared(tmp_path)
    originals={name:rows(path) for name,path in paths.items()}
    identifier=str(uuid4())
    queued=c.queue(identifier,list(paths),PWD,PWD)
    assert queued['state']=='QUEUED' and c.frozen and not paused
    assert PWD not in json.dumps(c.status(),ensure_ascii=False)
    c.request_pause();assert paused==[True]
    with pytest.raises(ProtectionError,match='terminar'):c.perform_offline()
    c.server_stopped();assert c.perform_offline() is True
    result=c.job(identifier)
    assert result['state']=='SUCCEEDED' and not c.frozen
    assert {e['target'] for e in result['results']}==set(paths)
    assert all(e['state']=='ENCRYPTED' and e['backup'] for e in result['results'])
    for name,path in paths.items():
        assert lab_format(path)=='encrypted_or_invalid'
        assert rows(path,PWD)==originals[name]
        with pytest.raises(sqlcipher3.DatabaseError):rows(path,'wrong owned test password')
        assert path.stat().st_mode&0o777==0o600
    assert lab_access.get(paths['LAB']) is None
    assert PWD not in repr(c._pending) and PWD not in json.dumps(result,ensure_ascii=False)
    assert c.perform_offline() is False


def test_lost_response_same_id_is_inert_and_changed_content_is_rejected(tmp_path):
    c,paths,paused=prepared(tmp_path);identifier=str(uuid4())
    first=c.queue(identifier,['LAB'],PWD,PWD)
    assert c.queue(identifier,['LAB'],PWD,PWD)==first
    with pytest.raises(ProtectionError,match='otro contenido'):
        c.queue(identifier,['DCA'],PWD,PWD)
    c.server_stopped();c.perform_offline();final=c.job(identifier)
    assert c.queue(identifier,['LAB'],PWD,PWD)==final
    c.request_pause();assert not paused


def test_failure_in_second_file_reports_partial_without_rollback_or_secret(tmp_path):
    c,paths,_=prepared(tmp_path);actual=c._migrate
    def fail(name,path,key):
        if name=='DCA':raise ValueError('must never echo '+key)
        return actual(name,path,key)
    c._migrate=fail
    identifier=str(uuid4());c.queue(identifier,['LAB','DCA','SPOT'],PWD,PWD);c.server_stopped();c.perform_offline()
    result=c.job(identifier)
    assert result['state']=='PARTIAL'
    assert [e['state'] for e in result['results']]==['ENCRYPTED','FAILED','NOT_ATTEMPTED']
    assert lab_format(paths['LAB'])=='encrypted_or_invalid' and lab_format(paths['DCA'])=='plain'
    assert PWD not in json.dumps(result,ensure_ascii=False)


def test_runtime_lease_still_prevents_migration_and_service_can_reopen(tmp_path):
    c,paths,_=prepared(tmp_path);identifier=str(uuid4())
    c.queue(identifier,['LAB'],PWD,PWD);c.server_stopped()
    with lab_runtime_lease(paths['LAB']):c.perform_offline()
    assert c.job(identifier)['state']=='FAILED' and lab_format(paths['LAB'])=='plain'
    c.attach(lambda:None);assert c.status()['ready']


def test_changed_file_after_queue_is_not_encrypted(tmp_path):
    c,paths,_=prepared(tmp_path);identifier=str(uuid4());c.queue(identifier,['LAB'],PWD,PWD)
    moved=paths['LAB'].with_suffix('.previous');paths['LAB'].rename(moved)
    with sqlite3.connect(paths['LAB']) as db:db.execute('CREATE TABLE unrelated (n INTEGER)')
    before=paths['LAB'].read_bytes();c.server_stopped();c.perform_offline()
    assert c.job(identifier)['state']=='FAILED' and paths['LAB'].read_bytes()==before


@pytest.mark.parametrize('target',['LAB','DCA','SPOT','GRVT'])
def test_foreign_aliases_and_links_cannot_target_excluded_data(tmp_path,target):
    forbidden=tmp_path/'excluded.db'
    with sqlite3.connect(forbidden) as db:db.execute('CREATE TABLE owned_private (id INTEGER)')
    c,paths,_=prepared(tmp_path/'stores',forbidden_paths=(forbidden,))
    paths[target].unlink();paths[target].hardlink_to(forbidden)
    before=forbidden.read_bytes()
    with pytest.raises(ProtectionError,match='excluido'):c.queue(str(uuid4()),[target],PWD,PWD)
    assert forbidden.read_bytes()==before


@pytest.mark.parametrize('targets,password,repeated',[
    (['REAL'],PWD,PWD),(['LAB','LAB'],PWD,PWD),([],PWD,PWD),
    (['LAB'],'short','short'),(['LAB'],PWD,'different password'),(['LAB'],PWD+'\n',PWD+'\n')])
def test_bad_selection_or_password_never_enters_pending_state(tmp_path,targets,password,repeated):
    c,_,_=prepared(tmp_path)
    with pytest.raises(ProtectionError):c.queue(str(uuid4()),targets,password,repeated)
    assert not c.frozen and c.status()['latest_job'] is None


def test_plain_activity_check_is_readonly_and_does_not_create_missing_stores(tmp_path):
    paths={name:tmp_path/(name+'.db') for name in ('LAB','DCA','SPOT','GRVT')}
    assert active_plain_sessions(paths) is False and not any(p.exists() for p in paths.values())
    with sqlite3.connect(paths['DCA']) as db:
        db.execute('CREATE TABLE paper_dca_sessions (status TEXT)');db.execute("INSERT INTO paper_dca_sessions VALUES ('ACTIVE')")
    before=paths['DCA'].read_bytes()
    assert active_plain_sessions(paths) is True and paths['DCA'].read_bytes()==before


def test_api_redacts_entire_bad_secret_body_and_freezes_data_routes(tmp_path):
    c,paths,paused=prepared(tmp_path);app=FastAPI();app.add_middleware(LocalLoopbackMiddleware)
    app.add_middleware(StorageProtectionMiddleware,control=c)
    app.include_router(create_storage_protection_router(c))
    @app.get('/data')
    def data():return {'ok':True}
    client=TestClient(app)
    base='/system/storage-protection'
    assert client.get(base,headers={'Origin':'https://outside.invalid'}).status_code==403
    for bad in ({'passphrase':PWD},{'id':'bad','targets':['LAB'],'passphrase':PWD,'repeated':PWD,'confirmation':'PROTEGER DATOS'}, {'passphrase':PWD,'unexpected':PWD}):
        response=client.post(base+'/jobs',json=bad)
        assert response.status_code==422 and PWD not in json.dumps(response.json(),ensure_ascii=False)
    identifier=str(uuid4());body={'id':identifier,'targets':['LAB'],'passphrase':PWD,'repeated':PWD,'confirmation':'PROTEGER DATOS'}
    response=client.post(base+'/jobs',json=body);assert response.status_code==202,response.text
    assert paused==[True] and PWD not in response.text
    assert client.get('/data').status_code==503
    assert client.get(base+'/jobs/'+identifier).status_code==200
    assert client.get('/data',headers={'Origin':'https://outside.invalid'}).status_code==403
    c.server_stopped();c.perform_offline()
    assert client.get('/data').status_code==200 and client.get(base+'/jobs/'+identifier).json()['state']=='SUCCEEDED'


def test_unmanaged_server_does_not_advertise_or_execute_protection(tmp_path):
    c,paths,_=prepared(tmp_path);c.server_stopped()
    assert c.status()['supported'] is False and c.status()['ready'] is False
    with pytest.raises(ProtectionError,match='lanzador'):c.queue(str(uuid4()),['LAB'],PWD,PWD)
    assert lab_format(paths['LAB'])=='plain' and not c.frozen
