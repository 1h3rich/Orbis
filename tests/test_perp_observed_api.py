"""Local HTTP contracts and actual controlled PAPER execution through the API."""
from decimal import Decimal
import io
from threading import Event
from time import monotonic,sleep
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_observed_api import create_observed_paper_router
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_export import verify_observed_export
from app.derivatives.perp_observed_origin import verified_observed_origin
from app.derivatives.perp_observed_service import ObservedPaperService
from app.derivatives.perp_observed_worker import ObservedSessionWorker
from app.lab.access import lab_access
from app.lab.perp_validation import PerpValidationPlan, calculate_perp_validation
from app.lab.perp_validation_store import PerpValidations
from app.lab.store import content_hash, encoded
from app.security.loopback import LocalLoopbackMiddleware
from tests.perp_observed_archive_fixture import LAB_KEY, NS, owned_archive
from tests.perp_observed_service_fixture import owned_service
from tests.test_perp_partial_simulation import TARGETS


PREFIX='/paper/perp/observed'
def uid():return str(uuid4())


def client_for(service):
    app=FastAPI();app.add_middleware(LocalLoopbackMiddleware)
    app.include_router(create_observed_paper_router(service))
    return TestClient(app)


def prepare(client,origin):
    payload={'validation_id':origin.validation_id,'preparation_id':uid()}
    response=client.post(PREFIX+'/prepare',json=payload)
    assert response.status_code==200,response.text
    return {**payload,'request_id':uid()}


@pytest.mark.parametrize("profile", ["market-rolling", "limit", "partial"])
def test_reserve_comparison_replays_the_pinned_paper_snapshot_and_all_original_reserves(tmp_path,monkeypatch,profile):
    lab,_,archive,origin,seed,clock = owned_archive(tmp_path,monkeypatch,limited=profile!="market-rolling",
        partial_targets=TARGETS if profile=="partial" else None)
    if profile=="market-rolling":
        ref=origin.reference()
        report=PerpValidations(lab).create(ref.dataset_id,ref.profile_id,
            PerpValidationPlan(method="ROLLING_FIXED_VERSION",folds=2))
        origin=verified_observed_origin(lab,report["id"])
    saved=PerpValidations(lab)._verified(origin.validation_id)
    reserves=[row for row in saved["payload"]["result"]["windows"] if row["role"]=="RESERVE"]
    created=archive.create(origin,seed,request_id=uid())
    id=created["id"]
    clock["now"]+=NS
    archive.control(id,"STOP",request_id=uid())
    with client_for(ObservedPaperService(lab,archive)) as client:
        path=PREFIX+"/sessions/"+id+"/reserve-comparison"
        params={"snapshot_sequence":created["sequence"],"snapshot_hash":created["head_hash"]}
        assert client.get(path).status_code==422
        assert client.get(path,params=params,headers={"Origin":"https://outside.invalid"}).status_code==403
        assert client.get(path,params={**params,"snapshot_hash":"a"*64}).status_code==422
        response=client.get(path,params=params)
        assert response.status_code==200 and response.headers["cache-control"]=="no-store",response.text
        value=response.json()
        assert value["execution_authority"]=="NONE" and value["validation_hash"]==saved["hash"]
        assert value["strategy_version_id"]==saved["profile"].strategy_version_id
        paper=value["paper"]
        assert paper["head_hash"]==params["snapshot_hash"] and paper["sequence"]==0 and not paper["is_latest"]
        assert paper["from_ns"]==paper["to_ns"]==seed.received_at_ns
        assert Decimal(paper["metrics"]["initial_capital"])==1000 and Decimal(paper["metrics"]["return_pct"])==0
        assert paper["metrics"]["closed_trades"]==0
        assert len(value["reserves"])==(2 if profile=="market-rolling" else 1)
        for actual,original in zip(value["reserves"],reserves,strict=True):
            assert actual["result_hash"]==original["result_hash"] and actual["bars"]==original["bars"]
            assert int(actual["from_ns"])==original["from_t"]*NS
            assert int(actual["to_ns"])==(original["to_t"]+60)*NS
            metrics=actual["metrics"]
            assert metrics["equity"]==original["metrics"]["final_equity"]
            assert metrics["closed_trades"]==original["metrics"]["trades"]
            assert metrics["fees_paid"]==original["metrics"]["fees_paid"]
            assert metrics["funding_paid"]==original["metrics"]["funding_paid"]
            assert Decimal(metrics["return_pct"]).quantize(Decimal("0.000001"))==Decimal(
                original["metrics"]["return_pct"]).quantize(Decimal("0.000001"))
        assert client.get(PREFIX+"/sessions/"+id).json()["session"]["sequence"]==1


def test_reserve_comparison_rejects_changed_lab_identity_and_locked_storage(tmp_path,monkeypatch):
    lab,paper,archive,origin,seed,_=owned_archive(tmp_path,monkeypatch)
    created=archive.create(origin,seed,request_id=uid())
    id=created["id"]
    saved=PerpValidations(lab)._verified(origin.validation_id)
    alternate=PerpValidationPlan(method="CHRONOLOGICAL_HOLDOUT",train_fraction="0.6")
    payload={**saved["payload"],"plan":alternate.payload(),"result":calculate_perp_validation(
        saved["dataset"],saved["profile"],saved["definition"],alternate)}
    with lab.connect() as db:
        db.execute("UPDATE perp_validations SET payload_json=?,hash=? WHERE id=?",
                   (encoded(payload),content_hash(payload),origin.validation_id))
    # The replacement is valid on its own, but does not match this PAPER origin.
    assert PerpValidations(lab)._verified(origin.validation_id)["hash"]==content_hash(payload)
    with client_for(ObservedPaperService(lab,archive)) as client:
        path=PREFIX+"/sessions/"+id+"/reserve-comparison"
        params={"snapshot_sequence":0,"snapshot_hash":created["head_hash"]}
        assert client.get(PREFIX+"/sessions").json()["lab_unlocked"]
        changed=client.get(path,params=params)
        assert changed.status_code==422 and "referencia original" in changed.text and "net_profit" not in changed.text
        lab_access.lock(lab.path)
        assert client.get(path,params=params).status_code==423
        assert not client.get(PREFIX+"/sessions").json()["lab_unlocked"]
        assert client.get(PREFIX+"/sessions/"+id+"/view").status_code==200
        paper_perp_access.lock(paper.path)
        assert client.get(path,params=params).status_code==423
    archive.detach(id)


def test_reserve_comparison_does_not_reuse_an_old_lab_unlock_epoch(tmp_path,monkeypatch):
    lab,_,archive,origin,seed,_=owned_archive(tmp_path,monkeypatch)
    created=archive.create(origin,seed,request_id=uid())
    read=archive.read
    def change_lab_session(*args,**kwargs):
        result=read(*args,**kwargs)
        lab_access.lock(lab.path)
        lab_access.unlock(lab.path,LAB_KEY)
        return result
    monkeypatch.setattr(archive,"read",change_lab_session)
    with client_for(ObservedPaperService(lab,archive)) as client:
        response=client.get(PREFIX+"/sessions/"+created["id"]+"/reserve-comparison",
                            params={"snapshot_sequence":0,"snapshot_hash":created["head_hash"]})
        assert response.status_code==422 and "sesión LAB cambió" in response.text
        assert "net_profit" not in response.text
    archive.detach(created["id"])


def test_api_preparation_activation_stop_export_and_lost_response(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();client=client_for(service)
    try:
        request=prepare(client,origin)
        assert client.get(PREFIX+'/sessions').json()['items']==[]
        activated=client.post(PREFIX+'/activate',json=request)
        assert activated.status_code==202 and activated.headers['cache-control']=='no-store'
        id=activated.json()['session_id']
        repeat=client.post(PREFIX+'/activate',json=request)
        assert repeat.json()['session_id']==id and len(sources)==1
        status=client.get(PREFIX+'/requests/'+request['request_id']).json()
        assert not status['in_progress'] and status['worker']['running']
        stop={'request_id':uid()}
        first=client.post(PREFIX+'/sessions/'+id+'/stop',json=stop)
        clock['now']+=20*NS
        second=client.post(PREFIX+'/sessions/'+id+'/stop',json=stop)
        assert first.status_code==second.status_code==200 and second.json()['duplicate']
        detail=client.get(PREFIX+'/sessions/'+id).json()
        assert detail['session']['checkpoint']['status']=='STOPPED'
        exported=client.get(PREFIX+'/sessions/'+id+'/export')
        assert exported.status_code==200 and exported.headers['content-type'].startswith('application/x-ndjson')
        assert verify_observed_export(io.BytesIO(exported.content))['session_id']==id
        assert client.get(PREFIX+'/sessions/'+id+'/events?limit=1').json()['verification']=='INDIVIDUAL_HASH_ONLY_NOT_FULL_REPLAY'
    finally:service.stop();client.close()


def test_api_lock_hides_data_but_emergency_cancel_still_works(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();client=client_for(service)
    try:
        request=prepare(client,origin);id=client.post(PREFIX+'/activate',json=request).json()['session_id']
        paper_perp_access.lock(service.archive.paper.path)
        for path in ('/sessions','/sessions/'+id,'/sessions/'+id+'/events','/sessions/'+id+'/export','/sessions/'+id+'/view','/requests/'+request['request_id']):
            assert client.get(PREFIX+path).status_code==423
        cancel=client.post(PREFIX+'/requests/'+request['request_id']+'/cancel')
        assert cancel.status_code==200 and cancel.json()['cancel_requested']
        assert set(cancel.json())=={'request_id','phase','cancel_requested','in_progress'}
    finally:service.stop();client.close()


def test_api_rejects_external_origin_real_options_and_unbounded_pages(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    service.start();client=client_for(service)
    try:
        assert client.get(PREFIX+'/status',headers={'Origin':'https://external.invalid'}).status_code==403
        assert client.get(PREFIX+'/status',headers={'Host':'external.invalid'}).status_code==403
        assert client.get(PREFIX+'/sessions?limit=101').status_code==422
        payload={'validation_id':origin.validation_id,'preparation_id':uid(),'execution_mode':'REAL'}
        assert client.post(PREFIX+'/prepare',json=payload).status_code==422
        assert client.post(PREFIX+'/prepare',json={**payload,'validation_id':1}).status_code==422
        assert not sources and service.archive.page()['total']==0
    finally:service.stop();client.close()


def test_api_plain_storage_does_not_silently_create_or_encrypt(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    plain=PaperPerpStore(tmp_path/'owned-plain.db')
    with plain.connect():pass
    service.archive=ObservedArchive(plain);client=client_for(service);service.start()
    try:
        response=client.post(PREFIX+'/prepare',json={'validation_id':origin.validation_id,'preparation_id':uid()})
        assert response.status_code==423 and plain.path.read_bytes()[:16]==b'SQLite format 3\x00'
        assert not sources
    finally:service.stop();client.close()


def test_actual_worker_buys_and_closes_from_http_then_replays_and_exports(tmp_path,monkeypatch):
    service,origin,seed,clock,lifetime,sources=owned_service(tmp_path,monkeypatch)
    original_factory=service.source_factory;opened=Event();release=Event()
    def source_factory():
        source=original_factory();original=source.poll
        # A new closed candle arrives after seed activation, never in its past.
        stamp=int(seed.received_at_ns)//NS
        source.adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
        def poll(state):
            if state.opened is not None:opened.set();assert release.wait(8)
            clock['now']=state.last_received_ns+10*NS
            return original(state)
        source.poll=poll;return source
    service.source_factory=source_factory
    service.worker_factory=lambda archive,id,source,**options:ObservedSessionWorker(archive,id,source,poll_seconds=.01,**options)
    service.start();client=client_for(service)
    try:
        request=prepare(client,origin);response=client.post(PREFIX+'/activate',json=request)
        assert response.status_code==202,response.text
        id=response.json()['session_id'];assert opened.wait(8)
        page=client.get(PREFIX+'/sessions').json()
        assert page['items'][0]['worker']['coverage_current']
        assert page['items'][0]['checkpoint']['account']['positions']
        response=client.post(PREFIX+'/sessions/'+id+'/close',json={'request_id':uid()})
        assert response.status_code==200,response.text
        sources[0].adapter.quote_override={'bid':'103','ask':'104'};release.set()
        deadline=monotonic()+8
        while service._workers[id].snapshot()['running']:
            assert monotonic()<deadline;sleep(.01)
        detail=client.get(PREFIX+'/sessions/'+id)
        assert detail.status_code==200,detail.text
        checkpoint=detail.json()['session']['checkpoint']
        assert checkpoint['status']=='STOPPED' and checkpoint['closed_trades']==1
        assert Decimal(checkpoint['account']['cash'])==Decimal('1074.997')
        assert not checkpoint['account']['positions']
        inspected=client.get(PREFIX+'/sessions/'+id+'/view?limit=3').json()
        view=inspected['view']
        assert view['verification']=='OBSERVED_REPLAY_VERIFIED' and len(view['candles'])==3
        assert Decimal(view['metrics']['equity'])==Decimal('1074.997')
        assert Decimal(view['metrics']['net_profit'])==Decimal('74.997')
        assert len(view['markers'])==2 and [row['entry'] for row in view['markers']]==[True,False]
        assert [Decimal(row['price']) for row in view['markers']]==[Decimal('100'),Decimal('103')]
        assert view['trades'][0]['exit_reason']=='MANUAL_CLOSE_OBSERVED' and view['trade_total']==1
        before=view['candles'][0]['t']
        older=client.get(PREFIX+'/sessions/'+id+'/view?limit=3&before_t='+str(before)).json()['view']
        assert len(older['candles'])==3 and older['candles'][-1]['t']<before
        assert not older['markers'] and older['trade_total']==1
        offset=client.get(PREFIX+'/sessions/'+id+'/view?trade_offset=1&marker_offset=1').json()['view']
        assert not offset['trades'] and len(offset['markers'])==1
        verified=verify_observed_export(io.BytesIO(client.get(PREFIX+'/sessions/'+id+'/export').content))
        assert verified['checkpoint']==checkpoint and verified['execution_authority']=='NONE'
    finally:release.set();service.stop();client.close()
