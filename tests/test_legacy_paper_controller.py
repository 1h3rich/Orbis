"""Actual file modes, SQLAlchemy transactions, migration and revocable access."""
from decimal import Decimal
from pathlib import Path
import sqlite3
from threading import Event,Thread

import pytest
from sqlalchemy import Column,ForeignKey,MetaData,String,Table,text

from app.database.legacy_controller import LegacyPaperRuntime
from app.database.legacy_migration import encrypt_active_legacy_database
from app.database.legacy_lease import exclusive_legacy_lease
from app.database.legacy_runtime import LegacyDatabaseError,legacy_database_format

PASSWORD='owned manual PAPER controller password'


def schema(engine):
    metadata=MetaData()
    Table('assets',metadata,Column('symbol',String,primary_key=True))
    Table('fills',metadata,Column('id',String,primary_key=True),
        Column('asset',String,ForeignKey('assets.symbol')),Column('quantity',String))
    metadata.create_all(engine)


def runtime(path,**kwargs):
    return LegacyPaperRuntime('sqlite:///'+str(path),path,**kwargs)


def test_first_use_has_no_plain_file_and_creates_cipher_schema_before_publish(tmp_path):
    path=tmp_path/'manual.db';owned=runtime(path,allow_locked=True)
    try:
        engine=owned.engine
        assert not path.exists() and owned.status()['format']=='missing'
        with pytest.raises(LegacyDatabaseError):engine.connect()
        assert owned.create(PASSWORD,initialize=schema)['unlocked']
        assert owned.engine is engine and path.read_bytes()[:16]!=b'SQLite format 3\x00'
        with engine.begin() as db:
            assert db.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==1
            db.execute(text("INSERT INTO assets VALUES ('BTC')"))
            db.execute(text("INSERT INTO fills VALUES ('buy','BTC','0.123456789012345678')"))
        assert owned.lock()['unlocked'] is False
        with pytest.raises(LegacyDatabaseError):engine.connect()
        with pytest.raises(LegacyDatabaseError):owned.unlock('incorrect owned password',initialize=schema)
        assert owned.unlock(PASSWORD,initialize=schema)['unlocked']
        with engine.connect() as db:
            assert Decimal(db.execute(text('SELECT quantity FROM fills')).scalar_one())==Decimal('0.123456789012345678')
        assert not list(tmp_path.glob('.legacy-init-*'))
    finally:owned.close()
    cold=runtime(path,allow_locked=True)
    try:
        assert not cold.status()['unlocked']
        cold.unlock(PASSWORD,initialize=schema)
        with cold.engine.connect() as db:assert db.execute(text('SELECT count(*) FROM fills')).scalar_one()==1
    finally:cold.close()


def test_offline_migration_keeps_engine_identity_records_and_returns_locked(tmp_path):
    path=tmp_path/'manual.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY, quantity TEXT)')
        db.execute("INSERT INTO ledger VALUES (1,'0.000000000000000001')")
    owned=runtime(path)
    try:
        engine=owned.engine
        with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM ledger')).scalar_one()==1
        with pytest.raises(LegacyDatabaseError):
            with exclusive_legacy_lease(path):pass
        owned.pause_offline()
        with pytest.raises(LegacyDatabaseError):engine.connect()
        result=encrypt_active_legacy_database(path,'CIFRAR PAPER',PASSWORD)
        owned.resume_offline()
        assert result['encrypted'] and owned.engine is engine and not owned.status()['unlocked']
        assert legacy_database_format(path)=='encrypted_or_invalid'
        with pytest.raises(LegacyDatabaseError):engine.connect()
        owned.unlock(PASSWORD)
        with engine.begin() as db:
            assert db.execute(text('SELECT quantity FROM ledger')).scalar_one()=='0.000000000000000001'
            db.execute(text("INSERT INTO ledger VALUES (2,'1000000000.123456789012345678')"))
        assert Path(result['backup']).read_bytes()[:16]!=b'SQLite format 3\x00'
    finally:owned.close()


def test_pause_cannot_release_lease_while_a_connection_is_still_checked_out(tmp_path):
    path=tmp_path/'manual.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE preserved (id INTEGER)')
    owned=runtime(path)
    try:
        with owned.engine.connect() as db:
            with pytest.raises(LegacyDatabaseError,match='conexiones'):owned.pause_offline()
            assert db.execute(text('SELECT count(*) FROM preserved')).scalar_one()==0
            with pytest.raises(LegacyDatabaseError):
                with exclusive_legacy_lease(path):pass
        owned.pause_offline();owned.resume_offline()
        assert owned.status()['unlocked'] and owned.status()['format']=='plain'
    finally:owned.close()


def test_a_replaced_cipher_file_is_never_opened_as_plaintext(tmp_path):
    path=tmp_path/'manual.db';owned=runtime(path,allow_locked=True)
    try:
        owned.create(PASSWORD,initialize=schema);owned.engine.dispose()
        other=tmp_path/'other.db'
        with sqlite3.connect(other) as db:db.execute('CREATE TABLE injected (id INTEGER)')
        other.replace(path)
        state=owned.status()
        assert state['format']=='plain' and not state['unlocked'] and state['requires_review']
        with pytest.raises(LegacyDatabaseError):owned.engine.connect()
    finally:owned.close()


def test_a_pool_connection_cannot_keep_reading_an_old_replaced_plain_file(tmp_path):
    path=tmp_path/'manual.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE original (id INTEGER)')
    owned=runtime(path)
    try:
        with owned.engine.connect() as db:assert db.execute(text('SELECT count(*) FROM original')).scalar_one()==0
        replacement=tmp_path/'replacement.db'
        with sqlite3.connect(replacement) as db:db.execute('CREATE TABLE changed (id INTEGER)')
        replacement.replace(path)
        with pytest.raises(LegacyDatabaseError,match='cambió'):owned.engine.connect()
    finally:owned.close()


def test_failed_schema_initialization_never_publishes_a_partial_new_database(tmp_path):
    path=tmp_path/'manual.db';owned=runtime(path,allow_locked=True)
    def failed(engine):
        schema(engine)
        raise RuntimeError('Owned schema initialization failure')
    try:
        with pytest.raises(RuntimeError):owned.create(PASSWORD,initialize=failed)
        assert not path.exists() and not owned.status()['unlocked']
        assert not list(tmp_path.glob('.legacy-init-*'))
        assert owned.create(PASSWORD,initialize=schema)['unlocked']
    finally:owned.close()


@pytest.mark.parametrize('change',['lock','newer_unlock','failed_newer_unlock'])
def test_late_unlock_cannot_undo_lock_or_a_newer_authentication(tmp_path,monkeypatch,change):
    path=tmp_path/'manual.db';owned=runtime(path,allow_locked=True)
    owned.create(PASSWORD,initialize=schema);owned.lock()
    entered=Event();release=Event();errors=[]
    original=owned.access.unlock
    def held(passphrase,**kwargs):
        result=original(passphrase,**kwargs)
        from threading import current_thread
        if current_thread().name=='old-unlock':
            entered.set();assert release.wait(10)
        return result
    monkeypatch.setattr(owned.access,'unlock',held)
    def unlock():
        try:owned.unlock(PASSWORD)
        except LegacyDatabaseError as error:errors.append(str(error))
    worker=Thread(target=unlock,name='old-unlock');worker.start()
    try:
        assert entered.wait(10)
        if change=='lock':owned.lock()
        elif change=='newer_unlock':owned.unlock(PASSWORD)
        else:
            with pytest.raises(LegacyDatabaseError):owned.unlock('new incorrect owned password')
        release.set();worker.join(10)
        assert not worker.is_alive() and len(errors)==1 and 'cancelado' in errors[0]
        assert owned.status()['unlocked'] is (change=='newer_unlock')
    finally:
        release.set();worker.join(10);owned.close()


@pytest.mark.parametrize('change',['lock','newer_unlock','failed_newer_unlock'])
def test_revocation_during_actual_cipher_verification_wins(tmp_path,monkeypatch,change):
    from threading import current_thread
    import app.database.legacy_runtime as cipher
    path=tmp_path/'manual.db';owned=runtime(path,allow_locked=True)
    owned.create(PASSWORD,initialize=schema);owned.lock()
    entered=Event();release=Event();errors=[];original=cipher._key
    def held(db,key):
        original(db,key)
        if current_thread().name=='old-cipher-verification':
            entered.set();assert release.wait(10)
    monkeypatch.setattr(cipher,'_key',held)
    def old():
        try:owned.unlock(PASSWORD)
        except LegacyDatabaseError as error:errors.append(str(error))
    worker=Thread(target=old,name='old-cipher-verification');worker.start()
    try:
        assert entered.wait(10)
        if change=='lock':owned.lock()
        elif change=='newer_unlock':owned.unlock(PASSWORD)
        else:
            with pytest.raises(LegacyDatabaseError):owned.unlock('incorrect later owned password')
        release.set();worker.join(10)
        assert not worker.is_alive() and len(errors)==1 and 'cancelado' in errors[0]
        assert owned.status()['unlocked'] is (change=='newer_unlock')
    finally:release.set();worker.join(10);owned.close()


def test_lock_during_creation_publishes_complete_cipher_but_does_not_unlock(tmp_path):
    path=tmp_path/'manual.db';owned=runtime(path,allow_locked=True)
    entered=Event();release=Event();errors=[]
    def held(engine):
        schema(engine);entered.set();assert release.wait(10)
    def create():
        try:owned.create(PASSWORD,initialize=held)
        except LegacyDatabaseError as error:errors.append(str(error))
    worker=Thread(target=create);worker.start()
    try:
        assert entered.wait(10)
        assert owned.status()['creating'] and not path.exists()
        with pytest.raises(LegacyDatabaseError):owned.create(PASSWORD,initialize=schema)
        assert not owned.lock()['unlocked']
        release.set();worker.join(10)
        assert not worker.is_alive() and len(errors)==1 and 'cancelado' in errors[0]
        assert legacy_database_format(path)=='encrypted_or_invalid' and not owned.status()['unlocked']
        owned.unlock(PASSWORD,initialize=schema)
        with owned.engine.connect() as db:assert db.execute(text('SELECT count(*) FROM fills')).scalar_one()==0
    finally:release.set();worker.join(10);owned.close()
