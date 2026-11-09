"""Actual threads with owned encrypted books and controlled public observations."""
from threading import Event
from time import monotonic,sleep
from uuid import uuid4
from decimal import Decimal

import pytest

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_worker import ObservedSessionWorker
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_wire import CloseEvent
from app.marketdata.grvt_candle_time import GrvtCandleNotClosed
from tests.perp_observed_archive_fixture import PAPER_KEY,NS,owned_archive
from tests.perp_observed_source_fixture import OwnedPublicAdapter


def wait_for(predicate):
    until=monotonic()+8
    while not predicate():
        assert monotonic()<until,'Owned worker did not reach the expected checkpoint'
        sleep(.01)


def setup_worker(tmp_path,monkeypatch,*,duration=None):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    if duration is not None:
        paper_perp_access.unlock(paper.path,PAPER_KEY,session_seconds=duration)
    id=archive.create(origin,seed,request_id=str(uuid4()))['id']
    adapter=OwnedPublicAdapter(seed,clock);public=GrvtObservedSource(adapter=adapter,clock_ns=lambda:clock['now'])
    entered=Event();release=Event()
    class Source:
        count=0
        def poll(self,state):
            self.count+=1
            if self.count==1:entered.set();assert release.wait(8)
            clock['now']=state.last_received_ns+10*NS
            return public.poll(state)
    source=Source();worker=ObservedSessionWorker(archive,id,source,poll_seconds=.01,clock_ns=lambda:clock['now'])
    return paper,archive,id,seed,clock,adapter,source,worker,entered,release


def test_shutdown_during_http_discards_late_quote_and_records_gap(tmp_path,monkeypatch):
    paper,archive,id,seed,clock,adapter,source,worker,entered,release=setup_worker(tmp_path,monkeypatch)
    worker.start();assert entered.wait(5)
    assert worker.snapshot()['running'] and not worker.snapshot()['coverage_current']
    worker.start();assert source.count==1
    worker.request_stop();release.set();worker.stop(8)
    assert not worker.snapshot()['running'] and worker.snapshot()['interruption_saved']
    report=archive.read(id)['session']
    assert report['stored_status']=='GAP' and report['sequence']==1
    assert not report['checkpoint']['account']['positions'] and not archive.active_ids()
    assert [row['input']['kind'] for row in archive.events_page(id)['items']]==['START','GAP']
    worker.start();assert not worker.snapshot()['running']  # One lifecycle only.


def test_public_failure_records_gap_without_fabricating_orders(tmp_path,monkeypatch):
    paper,archive,id,seed,clock,adapter,source,worker,entered,release=setup_worker(tmp_path,monkeypatch)
    adapter.fail='quote';release.set();worker.start()
    wait_for(lambda:not worker.snapshot()['running'])
    assert worker.snapshot()['phase']=='FAILED' and worker.snapshot()['interruption_saved']
    report=archive.read(id)['session']['checkpoint']
    assert report['status']=='GAP' and report['order_count']==0 and report['account']['cash']=='1000'


@pytest.mark.parametrize('expired',[False,True])
def test_pending_grvt_candle_only_retries_while_observation_has_coverage(tmp_path,monkeypatch,expired):
    _,archive,id,seed,clock,adapter,source,worker,_,first_release=setup_worker(tmp_path,monkeypatch)
    start=int(seed.received_at_ns)
    boundary=start+60*NS
    original=source.poll
    waiting=Event();release=Event();observed=Event();stop_observing=Event()

    def delayed(state):
        if state.last_received_ns<start+50*NS:
            return original(state)
        if state.last_received_ns<boundary and not waiting.is_set():
            clock['now']=boundary+(16*NS if expired else 0)
            waiting.set()
            raise GrvtCandleNotClosed(boundary)
        if state.last_received_ns<boundary:
            assert release.wait(8)
            clock['now']=boundary+NS
            stamp=start//NS
            adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
            public=GrvtObservedSource(adapter=adapter,clock_ns=lambda:clock['now'])
            return public.poll(state)
        observed.set();assert stop_observing.wait(8)
        return original(state)

    monkeypatch.setattr(source,'poll',delayed)
    first_release.set();worker.start()
    try:
        assert waiting.wait(8)
        if expired:
            wait_for(lambda:not worker.snapshot()['running'])
            events=archive.events_page(id)['items']
            assert events[-1]['input']['kind']=='GAP'
            assert archive.read(id)['session']['checkpoint']['order_count']==0
        else:
            assert archive.read(id)['session']['checkpoint']['order_count']==0
            assert len(archive.events_page(id)['items'])==6  # START and five earlier observations.
            release.set();assert observed.wait(8)
            assert archive.read(id)['session']['checkpoint']['order_count']==1
            events=archive.events_page(id)['items']
            assert len(events)==7 and len(events[-1]['input']['closed_candles'])==1
    finally:
        worker.request_stop();release.set();stop_observing.set();worker.stop(8)


def test_key_lock_during_http_never_commits_or_silently_resumes(tmp_path,monkeypatch):
    paper,archive,id,seed,clock,adapter,source,worker,entered,release=setup_worker(tmp_path,monkeypatch)
    worker.start();assert entered.wait(5)
    paper_perp_access.lock(paper.path);release.set()
    wait_for(lambda:not worker.snapshot()['running'])
    assert not worker.snapshot()['interruption_saved']
    assert worker.snapshot()['last_error'].startswith('INTERRUPTION_NOT_SAVED:')
    paper_perp_access.unlock(paper.path,PAPER_KEY)
    assert archive.read(id)['session']['sequence']==0 and not archive.active_ids()


@pytest.mark.parametrize('action',['CLOSE','SHUTDOWN'])
def test_observed_open_and_requested_close_use_public_prices_and_replay(tmp_path,monkeypatch,action):
    paper,archive,id,seed,clock,adapter,source,worker,entered,release=setup_worker(tmp_path,monkeypatch)
    stamp=int(seed.received_at_ns)//NS
    adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
    closed=Event();continue_close=Event();original=source.poll
    def controlled(state):
        if state.opened is not None:
            closed.set();assert continue_close.wait(8)
        return original(state)
    monkeypatch.setattr(source,'poll',controlled)
    release.set();worker.start()
    try:
        assert closed.wait(8)
        assert worker.snapshot()['coverage_current']
        before_control=archive.read(id)['session']['checkpoint']['account']
        if action=='CLOSE':
            event=CloseEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='CLOSE_REQUEST',received_at_ns=str(clock['now']))
            archive.append(id,event,request_id=str(uuid4()))
        else:worker.request_stop()
        adapter.quote_override={'bid':'103','ask':'104'};continue_close.set()
        wait_for(lambda:not worker.snapshot()['running'])
        result=archive.read(id)['session']['checkpoint']
        if action=='CLOSE':
            assert result['status']=='STOPPED' and result['closed_trades']==1
            assert Decimal(result['account']['cash'])==Decimal('1074.997') and not result['account']['positions']
            assert worker.snapshot()['phase']=='STOPPED' and not worker.snapshot()['interruption_saved']
        else:
            assert result['status']=='GAP' and result['closed_trades']==0 and result['order_count']==1
            assert result['account']==before_control
            position=result['account']['positions'][0]
            assert Decimal(position['size'])==Decimal('24.999')
            assert Decimal(position['collateral'])==Decimal('499.98')
            assert Decimal(result['account']['cash'])==Decimal('500.02')
            assert Decimal(result['account']['cash'])+Decimal(position['collateral'])==1000
            assert worker.snapshot()['phase']=='INTERRUPTED' and worker.snapshot()['interruption_saved']
    finally:
        continue_close.set();worker.stop(8)


def test_two_workers_cannot_claim_the_same_session(tmp_path,monkeypatch):
    paper,archive,id,seed,clock,adapter,source,worker,entered,release=setup_worker(tmp_path,monkeypatch)
    worker.start();assert entered.wait(5)
    other=ObservedSessionWorker(archive,id,source,clock_ns=lambda:clock['now'])
    try:
        with pytest.raises(ObservedError,match='Ya existe'):other.start()
        other.request_stop()
        with pytest.raises(ObservedError,match='Ya existe'):other.start()
        assert worker.snapshot()['running'] and not other.snapshot()['running']
    finally:
        worker.request_stop();release.set();worker.stop(8)


def test_cancel_before_start_cannot_leave_a_consuming_cursor(tmp_path,monkeypatch):
    paper,archive,id,seed,clock,adapter,source,worker,entered,release=setup_worker(tmp_path,monkeypatch)
    worker.request_stop()
    with pytest.raises(ObservedError,match='cancelada'):worker.start()
    assert not entered.is_set() and not worker.snapshot()['running'] and not archive.active_ids()
    assert archive.read(id)['session']['stored_status']=='GAP'


@pytest.mark.parametrize('mark',['100','20'])
def test_control_after_collection_preserves_current_price_and_mark_priority(tmp_path,monkeypatch,mark):
    # Collection precedes the command by 1ns; consumer acceptance follows it.
    # A real observed breach must not be erased by discarding the earlier point.
    paper,archive,id,seed,clock,adapter,source,worker,entered,first_release=setup_worker(tmp_path,monkeypatch)
    stamp=int(seed.received_at_ns)//NS;adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
    original=source.poll;captured=Event();release=Event();once=[]
    def queued(state):
        if state.opened is not None:adapter.quote_override={'mark_price':mark}
        event=original(state)
        if state.opened is not None and not once:
            once.append(event.received_at_ns);captured.set();assert release.wait(8)
        return event
    monkeypatch.setattr(source,'poll',queued);first_release.set();worker.start()
    try:
        assert captured.wait(8);clock['now']+=1
        archive.control(id,'CLOSE_REQUEST',request_id=str(uuid4()));release.set()
        wait_for(lambda:not worker.snapshot()['running'])
        report=archive.read(id)['session']['checkpoint']
        assert not report['account']['positions'] and report['closed_trades']==1
        if mark=='100':
            assert report['status']=='STOPPED' and Decimal(report['account']['cash'])==Decimal('975.001')
        else:
            assert report['status']=='LIQUIDATED' and Decimal(report['account']['cash'])==Decimal('500.02')
            assert report['order_count']==1  # Liquidation precedes a voluntary close.
        events=archive.events_page(id)['items']
        accepted=[row for row in events if row['input'].get('collected_at_ns')==once[0]][-1]
        assert int(accepted['input']['received_at_ns'])==int(once[0])+1
        assert any(event['kind']=='QUOTE_ACCEPTED' for event in accepted['evidence']['events'])
    finally:release.set();worker.stop(8)


def test_live_thread_does_not_claim_coverage_when_provider_quote_is_stale(tmp_path,monkeypatch):
    paper,archive,id,seed,clock,adapter,source,worker,entered,first_release=setup_worker(tmp_path,monkeypatch)
    original=source.poll;second=Event();release=Event()
    def delayed(state):
        event=original(state)
        if source.count==2:second.set();assert release.wait(8)
        return event
    monkeypatch.setattr(source,'poll',delayed);first_release.set();worker.start()
    try:
        assert second.wait(8);current=worker.snapshot()
        assert current['running'] and current['coverage_current']
        clock['now']=int(current['last_received_at_ns'])+16*NS
        stale=worker.snapshot()
        assert stale['running'] and not stale['coverage_current']
        assert int(stale['last_quote_time_ns'])==int(current['last_received_at_ns'])
    finally:worker.request_stop();release.set();worker.stop(8)


@pytest.mark.parametrize('duration',[900,604800])
def test_worker_respects_explicit_key_deadline_without_extending_or_resuming(tmp_path,monkeypatch,duration):
    key_clock=[0.0]
    monkeypatch.setattr(paper_perp_access,'_clock',lambda:key_clock[0])
    paper,archive,id,seed,clock,adapter,source,worker,entered,first_release=setup_worker(
        tmp_path,monkeypatch,duration=duration)
    stamp=int(seed.received_at_ns)//NS;adapter.rows[stamp]={**seed.history[-1].model_dump(),'t':stamp}
    original=source.poll;opened=Event();release=Event();once=[]
    def held(state):
        if state.opened is not None and not once:
            once.append(True);opened.set();assert release.wait(8)
        return original(state)
    monkeypatch.setattr(source,'poll',held);first_release.set();worker.start()
    try:
        assert opened.wait(8)
        before=archive.read(id)['session'];last=worker.snapshot()['last_received_at_ns']
        assert before['checkpoint']['account']['positions']
        key_clock[0]=901;release.set()
        if duration==900:
            wait_for(lambda:not worker.snapshot()['running'])
            assert worker.snapshot()['last_error'].startswith('INTERRUPTION_NOT_SAVED:')
            assert not paper_perp_access.status(paper.path)['unlocked']
            paper_perp_access.unlock(paper.path,PAPER_KEY)
            after=archive.read(id)['session']
            assert after['checkpoint']==before['checkpoint']
            assert not after['active_in_this_process'] and not archive.active_ids()
            assert not worker.snapshot()['running']  # Unlocking does not restart it.
        else:
            wait_for(lambda:worker.snapshot()['last_received_at_ns']!=last)
            assert worker.snapshot()['running'] and worker.snapshot()['coverage_current']
            assert paper_perp_access.status(paper.path)['remaining_seconds']==duration-901
            archive.control(id,'CLOSE_REQUEST',request_id=str(uuid4()))
            wait_for(lambda:not worker.snapshot()['running'])
            after=archive.read(id)['session']['checkpoint']
            assert after['status']=='STOPPED' and after['closed_trades']==1
            assert not after['account']['positions']
            assert paper_perp_access.status(paper.path)['remaining_seconds']==duration-901
    finally:
        release.set();worker.stop(8);paper_perp_access.lock(paper.path)
