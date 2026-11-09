"""Owned encrypted archive and fresh public points, never private GRVT."""
from uuid import uuid4

from app.derivatives.perp_gap_close_store import GapCloseStore
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_wire import observation_input
from tests.perp_observed_archive_fixture import NS, owned_archive
from tests.perp_observed_fixture import bar, batch
from tests.perp_observed_source_fixture import OwnedPublicAdapter


def owned_gap(tmp_path, monkeypatch, *, interrupt=True):
    lab, paper, archive, origin, seed, clock = owned_archive(tmp_path, monkeypatch)
    session_id = archive.create(origin, seed, request_id=str(uuid4()))['id']
    for second in range(10, 61, 10):
        state = archive.current_state(session_id)
        point = batch(state, second, bars=(bar(state.started_at_ns // NS),) if second == 60 else ())
        clock['now'] = point.received_at_ns
        archive.append(session_id, observation_input(point), request_id=str(uuid4()))
    assert archive.current_state(session_id).opened is not None
    if interrupt:
        clock['now'] += 30 * NS
        archive.interrupt(session_id, 'OWNED_TEST_INTERRUPTION')
    adapter = OwnedPublicAdapter(seed, clock)
    adapter.quote_override = {'bid': '103', 'ask': '104'}
    def factory():
        return GrvtObservedSource(adapter=adapter, clock_ns=lambda: clock['now'])
    store = GapCloseStore(archive, source_factory=factory)
    clock['now'] += 10 * NS
    return paper, archive, store, session_id, clock, adapter
