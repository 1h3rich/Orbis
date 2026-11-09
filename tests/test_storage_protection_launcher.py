"""Real launcher -> maintenance pause -> own SQLCipher files -> same port/reentry."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from app.lab.access import lab_format
from tests.test_storage_protection import prepared,rows,PWD

ROOT=Path(__file__).resolve().parents[1]


@contextmanager
def launched(tmp_path,paths):
    main=tmp_path/'manual.db'
    with sqlite3.connect(main) as db:db.execute('CREATE TABLE owned_manual (id INTEGER PRIMARY KEY)')
    env={k:v for k,v in os.environ.items() if not k.startswith('ORBIS_')}
    env.update({'ORBIS_DATABASE_URL':'sqlite:///'+str(main),'ORBIS_LAB_PATH':str(paths['LAB']),
        'ORBIS_AUTOMATION_PATH':str(paths['DCA']),'ORBIS_SPOT_STRATEGY_PATH':str(paths['SPOT']),
        'ORBIS_PAPER_PERP_PATH':str(paths['GRVT']),'ORBIS_REAL_VAULT_PATH':str(tmp_path/'real.db')})
    output=(tmp_path/'launcher.log').open('w')
    process=subprocess.Popen([sys.executable,str(ROOT/'scripts/run_orbis_local.py'),'--port','0','--no-browser'],
        cwd=tmp_path,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT)
    try:
        deadline=time.monotonic()+20;url=None
        while time.monotonic()<deadline:
            assert process.poll() is None,(tmp_path/'launcher.log').read_text()
            lines=(tmp_path/'launcher.log').read_text().splitlines()
            announced=next((line for line in lines if line.startswith('ORBIS local: ')),None)
            if announced:
                url=announced.removeprefix('ORBIS local: ').removesuffix('/ui/');break
            time.sleep(.05)
        assert url,'No owned launcher address'
        client=httpx.Client(base_url=url,trust_env=False,timeout=2)
        while time.monotonic()<deadline:
            try:
                response=client.get('/system/storage-protection')
                if response.status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.05)
        else:raise AssertionError((tmp_path/'launcher.log').read_text())
        yield process,client,main
        client.close()
    finally:
        if process.poll() is None:process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        output.close()
        assert PWD not in (tmp_path/'launcher.log').read_text()


def test_real_launcher_protects_four_stores_then_unlocks_original_books(tmp_path):
    _,paths,_=prepared(tmp_path)
    before={name:rows(path) for name,path in paths.items()}
    with launched(tmp_path,paths) as (process,client,manual):
        state=client.get('/system/storage-protection').json()
        assert state['supported'] and state['ready'],state
        identifier=str(uuid4());body={'id':identifier,'targets':list(paths),'passphrase':PWD,'repeated':PWD,'confirmation':'PROTEGER DATOS'}
        response=client.post('/system/storage-protection/jobs',json=body)
        assert response.status_code==202,response.text
        deadline=time.monotonic()+30;result=None
        while time.monotonic()<deadline:
            assert process.poll() is None,(tmp_path/'launcher.log').read_text()
            try:
                response=client.get('/system/storage-protection/jobs/'+identifier)
                if response.status_code==200:
                    result=response.json()
                    if result['state'] in ('SUCCEEDED','PARTIAL','FAILED'):break
            except httpx.HTTPError:pass
            time.sleep(.1)
        assert result and result['state']=='SUCCEEDED',(result,(tmp_path/'launcher.log').read_text())
        assert lab_format(manual)=='plain' and not (tmp_path/'real.db').exists()
        assert all(lab_format(path)=='encrypted_or_invalid' for path in paths.values())
        assert all(rows(path,PWD)==before[name] for name,path in paths.items())
        assert client.get('/lab/datasets').status_code==423
        assert client.get('/paper/perp/accounts').status_code==423
        assert client.get('/system/storage-protection').json()['supported']
        for route in ('/lab/unlock','/automation/dca/unlock','/paper/spot/strategies/unlock','/paper/perp/unlock'):
            response=client.post(route,json={'passphrase':PWD});assert response.status_code==200,(route,response.text)
        assert client.get('/lab/datasets').json()[0]['symbol']=='BTC/USDT'
        assert client.get('/automation/dca/rules').json()[0]['name']=='Owned DCA'
        assert client.get('/paper/spot/strategies').json()==[]
        assert client.get('/paper/perp/accounts').json()[0]['name']=='Owned futures'
        # Replay is inert and does not queue another pause.
        response=client.post('/system/storage-protection/jobs',json=body)
        assert response.status_code==202 and response.json()==result
        assert client.get('/system/storage-protection').json()['busy'] is False
        assert PWD.encode() not in Path(f'/proc/{process.pid}/cmdline').read_bytes()
        assert PWD.encode() not in Path(f'/proc/{process.pid}/environ').read_bytes()


def test_launcher_refuses_open_real_vault_without_closing_it(tmp_path):
    _,paths,_=prepared(tmp_path)
    with launched(tmp_path,paths) as (process,client,manual):
        created=client.post('/real/vault/create',json={'passphrase':PWD})
        assert created.status_code==200,created.text
        state=client.get('/system/storage-protection').json()
        assert state['ready'] is False and 'REAL' in state['reason']
        body={'id':str(uuid4()),'targets':['LAB'],'passphrase':PWD,'repeated':PWD,'confirmation':'PROTEGER DATOS'}
        response=client.post('/system/storage-protection/jobs',json=body)
        assert response.status_code==409 and PWD not in response.text
        assert client.get('/real/vault/status').json()['unlocked'] is True
        assert all(lab_format(path)=='plain' for path in paths.values())
        response=client.post('/real/vault/lock',headers={'X-Orbis-Vault-Token':created.json()['token']})
        assert response.status_code==200
        assert client.get('/system/storage-protection').json()['ready'] is True


def test_launcher_requires_existing_paper_session_to_be_stopped_explicitly(tmp_path):
    from app.automation.paper_dca import PaperDca
    from app.automation.store import AutomationStore
    from app.lab.store import LabStore
    from app.lab.engine import validate_candles
    from tests.test_paper_dca import rule
    _,paths,_=prepared(tmp_path)
    day=int(time.time())//86400*86400
    raw=[{'t':day+(i-3)*86400,'o':'100','h':'101','l':'99','c':'100','v':'10'} for i in range(3)]
    bars,_=validate_candles(raw,'1d',now=int(time.time()))
    dataset=LabStore(paths['LAB']).save_dataset('BTC/USDT','1d','mexc-spot-public',bars,[],verified_public=True)
    version=AutomationStore(paths['DCA']).create_rule('Active owned DCA',rule())
    session=PaperDca(paths['DCA'],paths['LAB']).create(version['version_id'],dataset['id'])
    assert session['status']=='ACTIVE' and session['next_exec_t']>time.time()
    with launched(tmp_path,paths) as (process,client,manual):
        state=client.get('/system/storage-protection').json()
        assert state['ready'] is False and 'PAPER' in state['reason']
        body={'id':str(uuid4()),'targets':['LAB'],'passphrase':PWD,'repeated':PWD,'confirmation':'PROTEGER DATOS'}
        assert client.post('/system/storage-protection/jobs',json=body).status_code==409
        shown=client.get('/automation/dca/paper/sessions/'+session['id']).json()
        assert shown['status']=='ACTIVE' and shown['state']['units']=='0'
        assert client.post('/automation/dca/paper/sessions/'+session['id']+'/stop').status_code==200
        assert client.get('/system/storage-protection').json()['ready'] is True
