"""Explicit authority, lost responses, concurrency, cancellation and drain."""
from threading import Barrier,Event,Thread
from uuid import uuid4

import pytest

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_service import ObservedPaperService
from app.derivatives.perp_observed_types import ObservedError
from tests.perp_observed_archive_fixture import PAPER_KEY,NS
from tests.perp_observed_service_fixture import owned_service


def uid():return str(uuid4())


def ready(service,origin):
    prep=uid();service.prepare(origin.validation_id,preparation_id=prep);return prep


def background(call):
    result=[];errors=[]
    def run():
        try:result.append(call())
        except Exception as exc:errors.append(exc)
    thread=Thread(target=run,daemon=True);thread.start();return thread,result,errors


def test_prepare_has_no_account_and_activation_is_idempotent_even_after_expiry(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    with pytest.raises(ObservedError,match='no acepta'):ready(service,origin)
    service.start();prep=uid()
    prepared=service.prepare(origin.validation_id,preparation_id=prep)
    assert prepared['phase']=='READY' and prepared['initial_capital']=='1000'
    assert service.archive.page()['total']==0 and sources==[]
    request=uid();first=service.activate(origin.validation_id,preparation_id=prep,request_id=request)
    worker=service._workers[first['session_id']]
    lifetime['now']=1000
    duplicate=service.activate(origin.validation_id,preparation_id=prep,request_id=request)
    assert duplicate['session_id']==first['session_id'] and worker.starts==1 and len(sources)==1
    assert service.sessions()['total']==1 and service.sessions()['items'][0]['worker']['running']
    service.stop();assert worker.stops==1 and not service.archive.active_ids()


def test_cancel_before_activation_post_is_a_tombstone_not_a_late_start(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin);request=uid()
    assert service.cancel(request)['phase']=='CANCELLED'
    result=service.activate(origin.validation_id,preparation_id=prep,request_id=request)
    assert result['phase']=='CANCELLED' and sources==[] and service.archive.page()['total']==0
    service.stop()


def test_cancel_during_public_download_prevents_publication_and_duplicate_jobs(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin);request=uid();entered=Event();release=Event()
    original=service.source_factory
    def source():
        item=original();prepare=item.prepare
        def blocking(origin):entered.set();assert release.wait(8);return prepare(origin)
        item.prepare=blocking;return item
    service.source_factory=source
    thread,result,errors=background(lambda:service.activate(origin.validation_id,preparation_id=prep,request_id=request))
    try:
        assert entered.wait(5)
        duplicate=service.activate(origin.validation_id,preparation_id=prep,request_id=request)
        assert duplicate['phase']=='ACTIVATING' and len(sources)==1
        assert service.cancel(request)['cancel_requested']
    finally:release.set();thread.join(8);service.stop()
    assert not thread.is_alive() and not errors and result[0]['phase']=='CANCELLED'
    assert service.archive.page()['total']==0


def test_cancel_after_seed_commit_still_prevents_worker_start(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin);request=uid();entered=Event();release=Event()
    original=service.archive.create
    def committing(*args,**kwargs):
        result=original(*args,**kwargs);entered.set();assert release.wait(8);return result
    monkeypatch.setattr(service.archive,'create',committing)
    thread,result,errors=background(lambda:service.activate(origin.validation_id,preparation_id=prep,request_id=request))
    try:
        assert entered.wait(5);service.cancel(request)
    finally:release.set();thread.join(8);service.stop()
    assert not errors and result[0]['phase']=='CANCELLED' and not service._workers
    report=service.archive.find_start(request)
    assert report['session']['stored_status']=='GAP' and report['session']['checkpoint']['order_count']==0


def test_same_key_reauthentication_during_collection_cannot_publish_under_new_epoch(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin);original=service.source_factory
    def source():
        item=original();prepare=item.prepare
        def reauth(origin):
            seed=prepare(origin);paper_perp_access.lock(service.archive.paper.path)
            paper_perp_access.unlock(service.archive.paper.path,PAPER_KEY);return seed
        item.prepare=reauth;return item
    service.source_factory=source
    with pytest.raises(ObservedError,match='PAPER cambió'):
        service.activate(origin.validation_id,preparation_id=prep,request_id=uid())
    assert service.archive.page()['total']==0;service.stop()


def test_restart_returns_saved_request_without_reattaching_or_recollecting(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin);request=uid()
    activated=service.activate(origin.validation_id,preparation_id=prep,request_id=request);service.stop()
    saved=service.archive.read(activated['session_id'])
    def forbidden():raise AssertionError('A saved request must not start a source')
    reopened=ObservedPaperService(service.lab,ObservedArchive(PaperPerpStore(service.archive.paper.path),
        clock_ns=lambda:clock['now']),source_factory=forbidden)
    reopened.start()
    result=reopened.activate(origin.validation_id,preparation_id=prep,request_id=request)
    assert result['session_id']==activated['session_id'] and result['worker'] is None
    assert result['session']['checkpoint']==saved['session']['checkpoint']
    assert not reopened.archive.active_ids();reopened.stop()


def test_active_registration_survives_pruning_and_can_be_cancelled(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch,max_workers=1)
    service.start();prep=ready(service,origin);request=uid()
    activated=service.activate(origin.validation_id,preparation_id=prep,request_id=request)
    worker=service._workers[activated['session_id']]
    ready(service,origin)  # Invokes pruning while the first worker is alive.
    with pytest.raises(ObservedError,match='Límite de observadores'):
        service.activate(origin.validation_id,preparation_id=prep,request_id=uid())
    assert service.cancel(request)['phase']=='INTERRUPTING' and not worker.running
    service.stop()


def test_expired_preparation_and_reused_identity_are_rejected(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch,max_preparations=1)
    service.start();prep=ready(service,origin)
    with pytest.raises(ObservedError,match='otro origen'):service.prepare(uid(),preparation_id=prep)
    with pytest.raises(ObservedError,match='Demasiadas preparaciones'):ready(service,origin)
    lifetime['now']=301
    with pytest.raises(ObservedError,match='Prepara de nuevo'):
        service.activate(origin.validation_id,preparation_id=prep,request_id=uid())
    assert not sources and service.archive.page()['total']==0;service.stop()


def test_shutdown_waits_for_inflight_collection_and_prevents_late_start(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin);request=uid();entered=Event();release=Event();done=Event()
    original=service.source_factory
    def source():
        item=original();prepare=item.prepare
        def waiting(origin):entered.set();assert release.wait(8);return prepare(origin)
        item.prepare=waiting;return item
    service.source_factory=source
    activating,result,errors=background(lambda:service.activate(origin.validation_id,preparation_id=prep,request_id=request))
    assert entered.wait(5)
    def drain():service.stop();done.set()
    stopping,_,stop_errors=background(drain)
    try:
        assert not done.wait(.05)
        with pytest.raises(ObservedError,match='drenando'):service.start()
    finally:release.set();activating.join(8);stopping.join(8)
    assert not activating.is_alive() and not stopping.is_alive() and not errors and not stop_errors
    assert done.is_set() and result[0]['phase']=='CANCELLED' and service.archive.page()['total']==0


def test_server_timestamp_control_retries_remain_idempotent_after_clock_moves(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin)
    id=service.activate(origin.validation_id,preparation_id=prep,request_id=uid())['session_id']
    request=uid();first=service.archive.control(id,'STOP',request_id=request)
    clock['now']+=20*NS
    again=service.archive.control(id,'STOP',request_id=request)
    assert again['duplicate'] and again['sequence']==first['sequence']
    with pytest.raises(ObservedError,match='otro control'):service.archive.control(id,'CLOSE_REQUEST',request_id=request)
    assert service.archive.read(id)['session']['checkpoint']['order_count']==0
    service.stop()


def test_racing_same_request_with_different_preparations_does_not_acknowledge_both(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();preparations=[ready(service,origin),ready(service,origin)];request=uid()
    original=service.archive.find_start;barrier=Barrier(2)
    def reading(id):
        result=original(id);barrier.wait(timeout=8);return result
    monkeypatch.setattr(service.archive,'find_start',reading)
    tasks=[background(lambda prep=prep:service.activate(origin.validation_id,preparation_id=prep,request_id=request))
        for prep in preparations]
    try:
        for thread,_,_ in tasks:thread.join(8)
        assert all(not thread.is_alive() for thread,_,_ in tasks)
        assert sum(bool(result) for _,result,_ in tasks)==1
        errors=[error for _,_,errors in tasks for error in errors]
        assert len(errors)==1 and isinstance(errors[0],ObservedError) and 'otro origen' in str(errors[0])
        assert service.archive.page()['total']==1 and len(sources)==1
    finally:service.stop()


def test_lab_original_edit_after_review_cannot_publish_a_session(tmp_path,monkeypatch):
    from app.lab.store import encoded
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin)
    with service.lab.connect() as db:
        db.execute('UPDATE strategy_versions SET definition_json=? WHERE id=?',
            (encoded({**seed.definition,'allocation':'0.4'}),seed.profile.strategy_version_id))
    with pytest.raises(ValueError,match='origen LAB cambió'):
        service.activate(origin.validation_id,preparation_id=prep,request_id=uid())
    assert service.archive.page()['total']==0;service.stop()


def test_access_change_revokes_review_and_active_cursor_without_rearming(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();prep=ready(service,origin)
    activated=service.activate(origin.validation_id,preparation_id=prep,request_id=uid())
    service.access_changed()
    assert not service.archive.active_ids() and not service._workers[activated['session_id']].running
    assert service.archive.read(activated['session_id'])['session']['status']=='PAUSED'
    with pytest.raises(ObservedError,match='Prepara de nuevo'):
        service.activate(origin.validation_id,preparation_id=prep,request_id=uid())
    service.stop()
