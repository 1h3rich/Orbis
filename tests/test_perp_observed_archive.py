"""Encrypted source/event persistence, replay, epochs and explicit cold inactivity."""
from dataclasses import replace
from decimal import Decimal as D
import json
import zlib
import io
from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_migration import create_secure_paper_perp_backup,verify_paper_perp_database
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_archive_format import event_hash,pack,unpack
from app.derivatives.perp_observed_wire import CloseEvent,GapEvent,ObservedSeed,observation_input
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_export import verify_observed_export
from app.lab.access import lab_access
from app.lab.store import content_hash,encoded
from tests.perp_observed_archive_fixture import LAB_KEY,PAPER_KEY,NS,owned_archive
from tests.perp_observed_fixture import bar,batch


def uid():return str(uuid4())


def test_cold_reopen_never_reattaches_and_duplicate_create_does_not_reset(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    request=uid();created=archive.create(origin,seed,request_id=request);id=created['id']
    assert created['active_in_this_process'] and archive.active_ids()==(id,)
    duplicate=archive.create(origin,seed,request_id=request)
    assert duplicate['session']['id']==id and duplicate['session']['checkpoint']['account']['cash']=='1000'
    opened=archive.read(id);assert opened['session']['verification']=='OBSERVED_REPLAY_VERIFIED'
    reopened=ObservedArchive(PaperPerpStore(paper.path),clock_ns=lambda:clock['now'])
    cold=reopened.read(id);assert cold['session']['status']=='RESTARTED' and not reopened.active_ids()
    assert cold['session']['checkpoint']==opened['session']['checkpoint']
    assert len(archive.page()['items'])==1 and archive.page()['items'][0]['active_in_this_process']
    clock['now']+=NS
    with pytest.raises(ObservedError,match='no activa'):
        reopened.append(id,GapEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='GAP',received_at_ns=str(clock['now']),reason='SERVICE_SHUTDOWN'),request_id=uid())
    assert paper.path.read_bytes()[:16]!=b'SQLite format 3\x00'


def test_open_close_idempotency_index_and_replay(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id'];state=seed.start().state
    for second in range(10,61,10):
        incoming=observation_input(batch(state,second,bars=(bar(state.started_at_ns//NS),) if second==60 else ()))
        clock['now']=int(incoming.received_at_ns);archive.append(id,incoming,request_id=uid())
    request=uid();clock['now']=state.started_at_ns+61*NS
    close=CloseEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='CLOSE_REQUEST',received_at_ns=str(clock['now']))
    archive.append(id,close,request_id=request)
    assert archive.append(id,close,request_id=request)['duplicate']
    changed=close.model_copy(update={'received_at_ns':str(clock['now']+1)})
    with pytest.raises(ObservedError,match='reutilizada'):archive.append(id,changed,request_id=request)
    # Use the owned active consumer's immutable state to supply the next input.
    current=archive._cache[id].replayed.state
    incoming=observation_input(batch(current,70,bid='103',ask='104'))
    clock['now']=int(incoming.received_at_ns);archive.append(id,incoming,request_id=uid())
    report=archive.read(id);account=report['session']['checkpoint']['account']
    assert D(account['cash'])==D('1074.997') and not account['positions']
    assert report['session']['stored_status']=='STOPPED'
    with paper.connect() as db:
        assert db.execute('SELECT count(*) FROM paper_observed_trades').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM paper_observed_candles').fetchone()[0]==len(seed.history)+1
    first=archive.events_page(id,limit=2);last=archive.events_page(id,offset=first['total']-1,limit=2)
    assert len(first['items'])==2 and len(last['items'])==1
    assert first['verification']=='INDIVIDUAL_HASH_ONLY_NOT_FULL_REPLAY'
    assert verify_paper_perp_database(paper.path,PAPER_KEY)['counts']['paper_observed_sessions']==1


def test_result_and_checkpoint_rehash_cannot_fake_replay(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id']
    with paper.connect() as db:
        row=db.execute('SELECT * FROM paper_observed_events WHERE session_id=?',(id,)).fetchone()
        source=unpack(row['input_blob']);evidence=unpack(row['evidence_blob'])
        evidence['checkpoint']['account']['cash']='999999'
        fake=event_hash(id,0,None,source,evidence)
        db.execute('UPDATE paper_observed_events SET evidence_blob=?,hash=? WHERE session_id=?',(pack(evidence),fake,id))
        db.execute('UPDATE paper_observed_sessions SET checkpoint_json=?,head_hash=? WHERE id=?',(encoded(evidence['checkpoint']),fake,id))
    with pytest.raises(ObservedError,match='reproduce'):archive.read(id)
    with pytest.raises(ValueError):verify_paper_perp_database(paper.path,PAPER_KEY)
    clock['now']+=NS
    event=GapEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='GAP',received_at_ns=str(clock['now']),reason='SERVICE_SHUTDOWN')
    with pytest.raises(ObservedError,match='head'):archive.append(id,event,request_id=uid())


def test_old_source_epoch_cannot_publish_a_new_session(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    lab_access.lock(lab.path);lab_access.unlock(lab.path,LAB_KEY)
    with pytest.raises(ValueError,match='sesión LAB cambió'):archive.create(origin,seed,request_id=uid())
    assert archive.page()['total']==0


def test_same_key_paper_reauthentication_during_compute_cancels_append(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id']
    from app.derivatives.perp_observed_archive import apply_input as original
    def compute(*args,**kwargs):
        result=original(*args,**kwargs);paper_perp_access.lock(paper.path);paper_perp_access.unlock(paper.path,PAPER_KEY)
        return result
    monkeypatch.setattr('app.derivatives.perp_observed_archive.apply_input',compute)
    incoming=observation_input(batch(seed.start().state,10));clock['now']=int(incoming.received_at_ns)
    with pytest.raises(ValueError,match='PAPER cambió'):archive.append(id,incoming,request_id=uid())
    assert not archive.active_ids()
    assert archive.read(id)['session']['sequence']==0


def test_source_raw_edit_and_seed_prefix_change_are_rejected(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    raw=seed.payload();raw['history'][0]['c']='102';changed=ObservedSeed.model_validate(raw)
    with pytest.raises(ObservedError,match='origen'):archive.create(origin,changed,request_id=uid())
    with lab.connect() as db:
        db.execute('UPDATE strategy_versions SET definition_json=? WHERE id=?',(encoded({**seed.definition,'allocation':'0.4'}),seed.profile.strategy_version_id))
    with pytest.raises(ValueError,match='origen LAB cambió'):archive.create(origin,seed,request_id=uid())
    assert archive.page()['total']==0


def test_compressed_input_truncation_concat_and_bomb_are_rejected():
    normal=pack({'hello':'world'});assert unpack(normal)=={'hello':'world'}
    for blob in [normal[:-1],normal+normal,zlib.compress(b'x'*(4*1024*1024+1))]:
        with pytest.raises(ObservedError):unpack(blob)


def test_plain_storage_never_accepts_a_new_observed_session(tmp_path):
    paper=PaperPerpStore(tmp_path/'plain.db')
    with paper.connect():pass
    archive=ObservedArchive(paper)
    with pytest.raises(ObservedError,match='cifrado'):archive.create(None,None,request_id=uid())


def test_online_encrypted_backup_replays_with_independent_key_and_never_activates(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id']
    snapshot=archive.read(id)
    copy=create_secure_paper_perp_backup(paper,'owned independent PAPER backup key')
    file=paper.path.parent/'paper-perp-backups'/copy['name']
    assert file.read_bytes()[:16]!=b'SQLite format 3\x00'
    assert copy['verified']==verify_paper_perp_database(file,'owned independent PAPER backup key')
    restored=ObservedArchive(PaperPerpStore(file,passphrase='owned independent PAPER backup key'),clock_ns=lambda:clock['now'])
    assert restored.read(id)['session']['checkpoint']==snapshot['session']['checkpoint']
    assert not restored.active_ids() and archive.active_ids()==(id,)
    assert not list(file.parent.glob('.pending-*'))


def test_replay_verifies_a_pinned_prefix_while_a_new_observation_commits(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id'];state=seed.start().state
    first=observation_input(batch(state,10));clock['now']=int(first.received_at_ns)
    archive.append(id,first,request_id=uid())
    from app.derivatives.perp_observed_archive_format import apply_input as original
    done=[]
    def advancing(*args,**kwargs):
        result=original(*args,**kwargs)
        if not done:
            done.append(True);current=archive._cache[id].replayed.state
            incoming=observation_input(batch(current,20));clock['now']=int(incoming.received_at_ns)
            archive.append(id,incoming,request_id=uid())
        return result
    monkeypatch.setattr('app.derivatives.perp_observed_archive_format.apply_input',advancing)
    prefix=archive.read(id)
    assert prefix['session']['sequence']==1 and prefix['snapshot_is_latest'] is False
    assert prefix['session']['active_in_this_process']
    assert archive.read(id)['session']['sequence']==2


def test_reauthentication_during_backup_does_not_publish_or_leave_plaintext(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    archive.create(origin,seed,request_id=uid())
    from app.derivatives.paper_migration import verify_paper_perp_database as original
    def verify(*args,**kwargs):
        result=original(*args,**kwargs);paper_perp_access.lock(paper.path);paper_perp_access.unlock(paper.path,PAPER_KEY)
        return result
    monkeypatch.setattr('app.derivatives.paper_migration.verify_paper_perp_database',verify)
    with pytest.raises(ValueError,match='PAPER cambió'):
        create_secure_paper_perp_backup(paper,'owned unused independent copy password')
    assert not list((paper.path.parent/'paper-perp-backups').iterdir())


def test_backup_sync_error_reports_the_published_verified_copy(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    archive.create(origin,seed,request_id=uid())
    def fail_sync(_):raise OSError('owned sync fault')
    monkeypatch.setattr('app.derivatives.paper_migration._sync_dir',fail_sync)
    with pytest.raises(ValueError,match='copia está guardada'):
        create_secure_paper_perp_backup(paper,"owned backup's independent secret")
    files=list((paper.path.parent/'paper-perp-backups').glob('*.db'));assert len(files)==1
    assert verify_paper_perp_database(files[0],"owned backup's independent secret")['counts']['paper_observed_sessions']==1


def test_migration_12_to13_preserves_previous_account_rows(tmp_path):
    from app.derivatives.paper_schema import CURRENT_PAPER_SCHEMA
    from tests.paper_schema_fixture import drop_observed_tables
    paper=PaperPerpStore(tmp_path/'owned-old-paper.db')
    account=paper.create_account('Owned old account','1000','500','1000','5',2,'0.001','0')
    with paper.connect() as db:
        previous=[tuple(row) for row in db.execute('SELECT * FROM paper_perp_accounts')]
        drop_observed_tables(db);db.execute('PRAGMA user_version=12')
    assert verify_paper_perp_database(paper.path)['schema_version']==12
    assert paper.account(account['id'])['initial_balance']=='1000'
    with paper.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==CURRENT_PAPER_SCHEMA
        assert [tuple(row) for row in db.execute('SELECT * FROM paper_perp_accounts')]==previous
        assert db.execute('SELECT count(*) FROM paper_observed_sessions').fetchone()[0]==0


def test_streamed_export_roundtrip_replays_and_requires_a_final_footer(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id'];state=seed.start().state
    event=observation_input(batch(state,10));clock['now']=int(event.received_at_ns)
    archive.append(id,event,request_id=uid())
    data=b''.join(archive.export_lines(id));verified=verify_observed_export(io.BytesIO(data))
    assert verified['verified'] and verified['events']==2 and verified['execution_authority']=='NONE'
    assert verified['session_activated'] is False
    assert verified['checkpoint']==archive.read(id)['session']['checkpoint']
    for changed in [data[:-1],b'\n'.join(data.splitlines()[:-1])+b'\n',data+b'\n']:
        with pytest.raises(ObservedError):verify_observed_export(io.BytesIO(changed))
    rows=[json.loads(line) for line in data.splitlines()]
    rows[1]['evidence']['checkpoint']['account']['cash']='999999'
    with pytest.raises(ObservedError,match='reproduce'):
        verify_observed_export(io.BytesIO(b''.join((encoded(row)+'\n').encode() for row in rows)))


def test_expiry_during_cipher_connection_cannot_commit_an_observation(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    access_clock={'now':0};monkeypatch.setattr(paper_perp_access,'_clock',lambda:access_clock['now'])
    paper_perp_access.unlock(paper.path,PAPER_KEY)
    id=archive.create(origin,seed,request_id=uid())['id']
    original=paper.connect;calls=[]
    @contextmanager
    def delaying():
        with original() as db:
            calls.append(True)
            if len(calls)==2:access_clock['now']=paper_perp_access.timeout_seconds+1
            yield db
    monkeypatch.setattr(paper,'connect',delaying)
    event=observation_input(batch(seed.start().state,10));clock['now']=int(event.received_at_ns)
    with pytest.raises(ValueError,match='bloqueado|cambió'):archive.append(id,event,request_id=uid())
    paper_perp_access.unlock(paper.path,PAPER_KEY)
    assert archive.read(id)['session']['sequence']==0


def test_cached_identities_do_not_retain_passwords_and_lock_discards_cursor(tmp_path,monkeypatch):
    from dataclasses import fields
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id']
    for identity,password in ((origin.stamp,LAB_KEY),(archive._cache[id].stamp,PAPER_KEY)):
        assert {item.name for item in fields(identity)}=={'device','inode','key_digest','epoch'}
        assert len(identity.key_digest)==32
        assert password not in repr(identity) and password.encode() not in identity.key_digest
    paper_perp_access.lock(paper.path)
    assert archive.active_ids()==() and id not in archive._cache
    paper_perp_access.unlock(paper.path,PAPER_KEY)
    assert archive.read(id)['session']['status']=='PAUSED'
    clock['now']+=NS
    event=GapEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='GAP',received_at_ns=str(clock['now']),reason='SERVICE_SHUTDOWN')
    with pytest.raises(ObservedError,match='no activa'):archive.append(id,event,request_id=uid())


def test_append_after_same_key_reauthentication_discards_old_cursor(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id']
    paper_perp_access.lock(paper.path);paper_perp_access.unlock(paper.path,PAPER_KEY)
    incoming=observation_input(batch(seed.start().state,10));clock['now']=int(incoming.received_at_ns)
    with pytest.raises(ObservedError,match='PAPER cambió'):archive.append(id,incoming,request_id=uid())
    assert id not in archive._cache and not archive.active_ids()
    assert archive.read(id)['session']['sequence']==0
