"""Actual process → local API → pause → cipher → reopen the same ledger."""
import json,time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.database.legacy_runtime import legacy_database_format
from tests.manual_paper_gui_fixture import launched_manual,paper_roundtrip,PASSWORD


def wait_job(owned,identifier):
    deadline=time.monotonic()+25;last=None
    while time.monotonic()<deadline:
        assert owned.process.poll() is None,owned.log.read_text()[-3000:]
        try:
            response=owned.client.get('/paper/legacy/protection/jobs/'+identifier)
            if response.status_code==200:
                last=response.json()
                if last['state'] in ('SUCCEEDED','FAILED','ENCRYPTED_REVIEW_REQUIRED','REOPEN_REVIEW_REQUIRED'):return last
        except httpx.HTTPError:pass
        time.sleep(.05)
    raise AssertionError(('Owned job not terminal',last,owned.log.read_text()[-3000:]))


def test_default_gui_start_missing_creates_cipher_and_reopens_its_actual_operations(tmp_path):
    with launched_manual(tmp_path,initial='missing') as owned:
        client=owned.client
        state=client.get('/paper/legacy/status').json()
        assert state['format']=='missing' and not state['unlocked'] and state['gui_access']
        assert not owned.path.exists() and client.get('/strategies').status_code==423
        bad={'passphrase':PASSWORD,'repeated':PASSWORD+'different','confirmation':'CREAR PAPER CIFRADO'}
        assert client.post('/paper/legacy/create',json=bad).status_code==422 and not owned.path.exists()
        body={**bad,'repeated':PASSWORD}
        assert client.post('/paper/legacy/create',json=body,headers={'Origin':'https://foreign.invalid'}).status_code==403
        assert not owned.path.exists()
        response=client.post('/paper/legacy/create',json=body)
        assert response.status_code==200,response.text
        assert response.json()['unlocked'] and legacy_database_format(owned.path)=='encrypted_or_invalid'
        original=paper_roundtrip(client)
        assert client.post('/paper/legacy/lock').json()['unlocked'] is False
        assert client.get('/paper/operations').status_code==423
        assert client.post('/paper/legacy/unlock',json={'passphrase':PASSWORD}).status_code==200
        assert client.get('/paper/operations').json()==original[4]
        assert client.post('/paper/buy',json=original[0]).json()==original[1]
        assert len(client.get('/paper/operations').json())==2
        assert PASSWORD.encode() not in Path(f'/proc/{owned.process.pid}/environ').read_bytes()
        assert PASSWORD.encode() not in Path(f'/proc/{owned.process.pid}/cmdline').read_bytes()
    with launched_manual(tmp_path,initial='existing') as owned:
        client=owned.client
        assert not client.get('/paper/legacy/status').json()['unlocked']
        assert client.get('/paper/operations').status_code==423
        assert client.post('/paper/legacy/unlock',json={'passphrase':PASSWORD}).status_code==200
        assert client.get('/paper/operations').json()==original[4]
        assert client.get('/paper/scenarios/EUR').json()==original[5]
        assert not (tmp_path/'real.db').exists()


def test_plain_manual_protection_drains_real_server_and_returns_same_process_locked(tmp_path):
    with launched_manual(tmp_path) as owned:
        client=owned.client;pid=owned.process.pid
        original=paper_roundtrip(client)
        assert client.get('/paper/legacy/protection').json()['ready']
        identifier=str(uuid4())
        body={'id':identifier,'passphrase':PASSWORD,'repeated':PASSWORD,'confirmation':'CIFRAR PAPER'}
        result=client.post('/paper/legacy/protection/jobs',json=body)
        assert result.status_code==202,result.text
        saved=wait_job(owned,identifier)
        assert saved['state']=='SUCCEEDED' and saved['active_database_encrypted'],saved
        assert owned.process.pid==pid and not owned.process.poll()
        assert not client.get('/paper/legacy/status').json()['unlocked']
        assert client.get('/paper/operations').status_code==423
        assert legacy_database_format(owned.path)=='encrypted_or_invalid'
        assert Path(saved['backup']).is_file() and Path(saved['backup']).read_bytes()[:16]!=b'SQLite format 3\x00'
        assert client.post('/paper/legacy/unlock',json={'passphrase':'wrong owned graphical password'}).status_code==400
        assert client.post('/paper/legacy/unlock',json={'passphrase':PASSWORD}).status_code==200
        assert client.get('/paper/operations').json()==original[4]
        assert client.get('/paper/scenarios/EUR').json()==original[5]
        assert client.post('/paper/buy',json=original[0]).json()==original[1]
        assert client.post('/paper/sell',json=original[2]).json()==original[3]
        assert len(client.get('/paper/operations').json())==2
        replay=client.post('/paper/legacy/protection/jobs',json=body)
        assert replay.status_code==202 and replay.json()==saved
        assert not client.get('/system/storage-protection').json()['busy']
        assert not (tmp_path/'real.db').exists()


def test_password_bodies_are_bounded_redacted_and_do_not_accept_external_targets(tmp_path):
    with launched_manual(tmp_path) as owned:
        client=owned.client
        endpoint='/paper/legacy/protection/jobs'
        body={'id':str(uuid4()),'passphrase':PASSWORD,'repeated':PASSWORD,'confirmation':'CIFRAR PAPER'}
        for bad in ({'passphrase':PASSWORD},{**body,'target':'REAL'},{**body,'passphrase':False},
                    {**body,'confirmation':'PROTEGER DATOS'}):
            response=client.post(endpoint,json=bad)
            assert response.status_code==422 and PASSWORD not in response.text
        response=client.post(endpoint,content=json.dumps({**body,'repeated':'x'*20000}),headers={'Content-Type':'application/json'})
        assert response.status_code==413 and PASSWORD not in response.text
        for path in ('/paper/legacy/create','/paper/legacy/unlock','/paper/legacy/backup/encrypted'):
            response=client.post(path,json={'passphrase':PASSWORD,'unexpected':PASSWORD})
            assert response.status_code==422 and PASSWORD not in response.text
            response=client.post(path,content=json.dumps({'passphrase':PASSWORD,'unexpected':'x'*20000}),
                headers={'Content-Type':'application/json'})
            assert response.status_code==413 and PASSWORD not in response.text
        assert legacy_database_format(owned.path)=='plain' and not client.get('/system/storage-protection').json()['busy']
