"""Real offline cipher migration with shared maintenance exclusion."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.manual_paper_protection import ManualPaperProtection
from app.core.storage_protection import ProtectionError,StorageProtection
from app.database.legacy_controller import LegacyPaperRuntime
from app.database.legacy_migration import encrypt_active_legacy_database
from app.database.legacy_runtime import LegacyDatabaseError,legacy_database_format
from app.database.legacy_backup import verify_legacy_encrypted_backup

PASSWORD='owned manual offline protection password'


def case(directory,*,migrate=None):
    path=directory/'manual.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY, quantity TEXT)')
        db.execute("INSERT INTO ledger VALUES (1,'123.000000000000000001')")
    runtime=LegacyPaperRuntime('sqlite:///'+str(path),path)
    paths={name:directory/(name+'.db') for name in ('LAB','DCA','SPOT','GRVT')}
    for target in paths.values():
        with sqlite3.connect(target) as db:db.execute('CREATE TABLE preserved (id INTEGER)')
    paused=[];state={'reason':None}
    shared=StorageProtection(paths,readiness=lambda:state['reason'])
    manual=ManualPaperProtection(runtime,shared,readiness=lambda:state['reason'],migrate=migrate,
        forbidden_paths=tuple(paths.values())+(directory/'real.db',))
    callback=lambda:paused.append('requested')
    shared.attach(callback);manual.attach(callback)
    return SimpleNamespace(runtime=runtime,shared=shared,manual=manual,paused=paused,state=state,paths=paths)


def test_queue_then_real_offline_protection_keeps_records_backup_and_locked_engine(tmp_path):
    owned=case(tmp_path);identifier=str(uuid4())
    try:
        engine=owned.runtime.engine
        assert owned.manual.status()['ready']
        queued=owned.manual.queue(identifier,PASSWORD,PASSWORD)
        assert queued['state']=='QUEUED' and owned.shared.frozen and not owned.paused
        assert PASSWORD not in json.dumps(queued)
        with pytest.raises(ProtectionError):owned.manual.perform_offline()
        owned.manual.request_pause();assert owned.paused==['requested']
        with pytest.raises(ProtectionError):owned.shared.queue(str(uuid4()),['LAB'],PASSWORD,PASSWORD)
        owned.manual.server_stopped();owned.shared.server_stopped()
        assert owned.manual.perform_offline()
        saved=owned.manual.job(identifier)
        assert saved['state']=='SUCCEEDED' and saved['active_database_encrypted']
        assert not owned.shared.frozen and not owned.manual.status()['busy']
        assert owned.runtime.engine is engine and not owned.runtime.status()['unlocked']
        assert legacy_database_format(owned.runtime.path)=='encrypted_or_invalid'
        verified=verify_legacy_encrypted_backup(Path(saved['backup']),PASSWORD)
        assert verified['tables']=={'ledger':1}
        with pytest.raises(LegacyDatabaseError):engine.connect()
        owned.runtime.unlock(PASSWORD)
        with engine.connect() as db:assert db.execute(text('SELECT quantity FROM ledger')).scalar_one()=='123.000000000000000001'
        assert owned.manual.queue(identifier,PASSWORD,PASSWORD)==saved
        owned.manual.request_pause();assert owned.paused==['requested']
        with pytest.raises(ProtectionError):owned.manual.queue(identifier,PASSWORD+'changed',PASSWORD+'changed')
        assert all(legacy_database_format(path)=='plain' for path in owned.paths.values())
    finally:owned.runtime.close()


def test_other_store_protection_and_foreign_maintenance_token_cannot_be_bypassed(tmp_path):
    owned=case(tmp_path)
    try:
        queued=owned.shared.queue(str(uuid4()),['LAB'],PASSWORD,PASSWORD)
        assert queued['state']=='QUEUED'
        with pytest.raises(ProtectionError):owned.manual.queue(str(uuid4()),PASSWORD,PASSWORD)
        owned.shared.release_external(object())
        assert owned.shared.frozen and not owned.manual.status()['ready']
        assert legacy_database_format(owned.runtime.path)=='plain'
    finally:owned.runtime.close()


@pytest.mark.parametrize('phase',['queue','offline'])
def test_changed_readiness_never_encrypts_or_reveals_secrets(tmp_path,phase):
    owned=case(tmp_path)
    try:
        identifier=str(uuid4())
        if phase=='queue':
            owned.state['reason']='Bloquea REAL'
            with pytest.raises(ProtectionError,match='REAL'):owned.manual.queue(identifier,PASSWORD,PASSWORD)
        else:
            owned.manual.queue(identifier,PASSWORD,PASSWORD)
            owned.state['reason']='PAPER tiene seguimiento activo'
            owned.manual.server_stopped()
            assert owned.manual.perform_offline()
            saved=owned.manual.job(identifier)
            assert saved['state']=='FAILED' and not saved['active_database_encrypted']
            assert PASSWORD not in json.dumps(saved)
        assert legacy_database_format(owned.runtime.path)=='plain' and not owned.shared.frozen
    finally:owned.runtime.close()


def test_a_failure_after_cipher_publication_stays_locked_and_requires_review(tmp_path):
    def interrupted(path,confirmation,password):
        encrypt_active_legacy_database(path,confirmation,password)
        raise RuntimeError('Owned failure must not leak '+password)
    owned=case(tmp_path,migrate=interrupted)
    try:
        identifier=str(uuid4());owned.manual.queue(identifier,PASSWORD,PASSWORD)
        owned.manual.server_stopped();assert owned.manual.perform_offline()
        saved=owned.manual.job(identifier)
        assert saved['state']=='ENCRYPTED_REVIEW_REQUIRED' and saved['active_database_encrypted']
        assert not owned.runtime.status()['unlocked'] and not owned.shared.frozen
        assert PASSWORD not in json.dumps(saved)
        owned.runtime.unlock(PASSWORD)
        with owned.runtime.engine.connect() as db:assert db.execute(text('SELECT count(*) FROM ledger')).scalar_one()==1
    finally:owned.runtime.close()


def test_alias_of_an_excluded_store_is_not_reserved_or_encrypted(tmp_path):
    owned=case(tmp_path)
    try:
        owned.manual.forbidden=(owned.runtime.path,)
        with pytest.raises(ProtectionError,match='excluido'):owned.manual.queue(str(uuid4()),PASSWORD,PASSWORD)
        assert not owned.shared.frozen and not owned.manual.status()['busy']
        assert legacy_database_format(owned.runtime.path)=='plain'
    finally:owned.runtime.close()
