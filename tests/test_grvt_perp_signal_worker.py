"""Independent collection, durable-before-publication and immediate RAM STOP."""
from threading import Event

import pytest

from app.live_execution.grvt_perp_signal_worker import PerpSignalWorker
from app.live_execution.grvt_perp_signals import replay_signal_records
from app.marketdata.grvt_candle_time import GrvtCandleNotClosed
from tests.grvt_perp_signal_fixture import NS, bar, signal_input, signal_seed


class OwnedSource:
    def __init__(self, seed):
        self.seed=seed
        self.clock=int(seed.prepared_at_ns)
        self.calls=0
        self.before=None
        self.after=None

    def poll(self,state):
        self.calls+=1
        if self.before:self.before()
        seconds=self.calls*10
        self.clock=state.started_at_ns+seconds*NS
        event=signal_input(state,seconds,
            candles=(bar(state.started_at_ns//NS+seconds-60),) if seconds%60==0 else ())
        if self.after:self.after()
        return event


def fixture_worker(*,append=None,permission=lambda:None):
    seed=signal_seed()
    source=OwnedSource(seed)
    records=[]
    waiting=Event()
    def waiter(stop,seconds):
        if source.calls>=6:
            waiting.set()
            stop.wait(5)
    worker=PerpSignalWorker(seed,source=source,append_record=append or records.append,
        require_permission=permission,clock_ns=lambda:source.clock,waiter=waiter)
    return worker,seed,source,records,waiting


def test_collector_advances_while_the_executor_is_elsewhere_and_publishes_only_after_durable_append():
    append_observations=[]
    def append(record):
        # The first matched decision must not be visible until its event has
        # been acknowledged by the encrypted journal writer.
        append_observations.append(worker.candidate())
        records.append(record)
    worker,seed,source,records,waiting=fixture_worker(append=append)
    try:
        worker.start()
        assert waiting.wait(5)
        assert source.calls==len(records)==6
        assert all(candidate is None for candidate in append_observations)
        candidate=worker.candidate()
        assert candidate is not None and candidate.seed_hash==seed.start().seed_hash
        assert candidate.instrument==seed.instrument and candidate.definition_json==seed.start().definition_json
        assert worker.snapshot()['execution_authority']=='NONE'
        assert replay_signal_records(seed,records).state.matched_entries==1
        source.clock+=6*NS
        assert worker.candidate() is None  # Stalled market, even if the signal is younger than 15s.
    finally:
        worker.stop()
        assert worker.join(5)
    assert worker.candidate() is None
    with pytest.raises((ValueError,RuntimeError)):worker.start()


@pytest.mark.parametrize('lost_response',[False,True])
def test_archive_failure_never_publishes_retries_or_restarts_even_if_commit_may_have_succeeded(lost_response):
    attempts=[]
    def append(record):
        attempts.append(record)
        if len(attempts)==6:
            if lost_response:records.append(record)
            raise RuntimeError('Owned archive reply lost or write failed')
        records.append(record)
    worker,seed,source,records,waiting=fixture_worker(append=append)
    worker.start()
    assert worker.join(5)
    assert worker.snapshot()['status']=='ARCHIVE_ERROR'
    assert worker.candidate() is None and source.calls==len(attempts)==6
    assert len(records)==(6 if lost_response else 5)
    replay=replay_signal_records(seed,records)
    assert replay.state.matched_entries==(1 if lost_response else 0)
    assert replay.outcome=='REPLAYED_WITHOUT_AUTHORITY'
    assert worker.snapshot()['checkpoint']['sequence']==5


def test_stop_during_http_revokes_before_request_finishes_and_prevents_archive_and_publication():
    entered,release=Event(),Event()
    worker,seed,source,records,waiting=fixture_worker()
    def blocking():
        entered.set()
        assert release.wait(5)
    source.before=blocking
    try:
        worker.start()
        assert entered.wait(5)
        worker.stop()
        assert worker.snapshot()['status']=='STOPPED'
        assert worker.candidate() is None
        assert not worker.join(0)
    finally:
        release.set()
        assert worker.join(5)
    assert not records and source.calls==1


def test_revoked_permission_after_collection_blocks_the_first_archive_write():
    permitted={'yes':True}
    def permission():
        if not permitted['yes']:raise RuntimeError('Owned session revoked')
    worker,seed,source,records,waiting=fixture_worker(permission=permission)
    source.after=lambda:permitted.update(yes=False)
    worker.start()
    assert worker.join(5)
    assert worker.snapshot()['status']=='PERMISSION_REVOKED'
    assert not records and worker.candidate() is None and source.calls==1


def test_slow_archive_keeps_receipt_times_and_stops_before_publication():
    def append(record):
        records.append(record)
        if len(records)==6:source.clock+=6*NS
    worker,seed,source,records,waiting=fixture_worker(append=append)
    worker.start()
    assert worker.join(5)
    assert worker.snapshot()['status']=='ARCHIVE_DELAY' and worker.candidate() is None
    assert len(records)==6
    assert records[-1]['input']['accepted_at_ns']==str(seed.start().started_at_ns+60*NS)
    assert replay_signal_records(seed,records).state.matched_entries==1


def test_source_error_does_not_retry_and_a_saved_checkpoint_does_not_start_anything():
    worker,seed,source,records,waiting=fixture_worker()
    def failed():raise RuntimeError('Owned HTTP failure')
    source.before=failed
    assert worker.snapshot()['status']=='PREPARED'
    assert worker.join(0) and source.calls==0
    worker.start()
    assert worker.join(5)
    assert worker.snapshot()['status']=='SOURCE_ERROR' and source.calls==1 and not records
    assert worker.candidate() is None


@pytest.mark.parametrize('expired',[False,True])
def test_pending_grvt_candle_wait_does_not_archive_or_publish_early(expired):
    seed=signal_seed()
    source=OwnedSource(seed)
    records=[];waiting=Event();resume=Event();observed=Event()
    original=source.poll
    boundary=seed.start().started_at_ns+60*NS

    def pending_once(state):
        if source.calls<5:
            return original(state)
        source.calls+=1
        if source.calls==6:
            source.clock=boundary+(16*NS if expired else 0)
            raise GrvtCandleNotClosed(boundary)
        source.clock=boundary+NS
        return signal_input(state,61,candles=(bar(boundary//NS-60),))

    def waiter(stop,_seconds):
        if source.calls==6:
            waiting.set();assert resume.wait(5)
        if source.calls==7:
            observed.set();stop.wait(5)

    source.poll=pending_once
    worker=PerpSignalWorker(seed,source=source,append_record=records.append,require_permission=lambda:None,
        clock_ns=lambda:source.clock,waiter=waiter)
    worker.start()
    try:
        if expired:
            assert worker.join(5) and worker.snapshot()['status']=='SOURCE_ERROR'
            assert not waiting.is_set() and worker.candidate() is None and len(records)==5
        else:
            assert waiting.wait(5) and len(records)==5 and worker.candidate() is None
            resume.set();assert observed.wait(5)
            assert len(records)==6 and worker.candidate() is not None
            assert replay_signal_records(seed,records).state.matched_entries==1
    finally:
        worker.stop();resume.set();assert worker.join(5)


def test_gap_is_archived_and_terminal_without_publishing_an_entry():
    worker,seed,source,records,waiting=fixture_worker()
    original=source.poll
    def gap(state):
        source.calls=2
        return original(state)  # First received point arrives 30s after seed.
    source.poll=gap
    worker.start()
    assert worker.join(5)
    assert worker.snapshot()['status']=='GAP' and worker.candidate() is None
    assert len(records)==1 and records[0]['outcome']=='QUOTE_COVERAGE_GAP'
    assert replay_signal_records(seed,records).state.status=='GAP'


def test_stop_while_archive_acknowledges_a_match_never_exposes_the_candidate():
    def append(record):
        records.append(record)
        if len(records)==6:worker.stop()
    worker,seed,source,records,waiting=fixture_worker(append=append)
    worker.start()
    assert worker.join(5)
    assert len(records)==6 and worker.snapshot()['status']=='STOPPED' and worker.candidate() is None
    assert replay_signal_records(seed,records).state.matched_entries==1


@pytest.mark.parametrize('poll',[0,-1,True,6,float('nan'),float('inf')])
def test_invalid_poll_interval_is_rejected(poll):
    seed=signal_seed()
    with pytest.raises(ValueError):
        PerpSignalWorker(seed,source=OwnedSource(seed),append_record=lambda value:None,
            require_permission=lambda:None,poll_seconds=poll)


def test_thread_start_failure_is_terminal_and_has_no_source_io(monkeypatch):
    worker,seed,source,records,waiting=fixture_worker()
    def failed(*args,**kwargs):raise RuntimeError('Owned thread allocation failure')
    monkeypatch.setattr('app.live_execution.grvt_perp_signal_worker.Thread.start',failed)
    with pytest.raises(RuntimeError,match='allocation failure'):worker.start()
    assert worker.snapshot()['status']=='START_ERROR' and worker.join(0)
    assert not records and source.calls==0 and worker.candidate() is None
