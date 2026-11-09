"""Check exact simulated fills and causal order handling using owned observations."""
from decimal import Decimal as D
import io
import json
from uuid import uuid4

import pytest

from app.derivatives.perp_observed import observe, start_observed, stop_observed
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_export import verify_observed_export
from app.derivatives.perp_observed_wire import observation_input, read_observed_seed
from app.derivatives.paper_migration import verify_paper_perp_database
from app.research.perp_live_review import review_perp_live
from tests.perp_observed_archive_fixture import PAPER_KEY, owned_archive
from tests.perp_observed_fixture import NS, bar, batch, initial
from tests.test_perp_limit_simulation import limit_profile


def uid():
    return str(uuid4())


def start(side, *, slippage='0.01'):
    base = initial(side, slippage_rate=slippage)
    definition = json.loads(base.definition_json)
    profile = limit_profile(definition, offset='0.01', valid_bars=1)
    return start_observed(profile, definition, base.timeframe, base.account.markets[0],
        base.history, base.last_quote, base.started_at_ns, (base.funding_anchor,)).state


def rest(state, side, *, lag=0, **quote):
    for second in range(10, 61, 10):
        bars = (bar(state.started_at_ns // NS, side),) if second == 60 else ()
        transition = observe(state, batch(state, second, bars=bars,
            quote_time=state.started_at_ns + (second-lag)*NS, **quote))
        state = transition.state
    assert state.resting and state.opened is None
    assert state.account.fees_paid == 0
    assert json.loads(transition.signals_json[0])['action'] == 'LIMIT_PENDING'
    return state


@pytest.mark.parametrize(('side', 'bid', 'ask', 'bound'), [
    ('LONG', '98', '99.50', '99.99'), ('SHORT', '100', '101', '99.99')])
def test_admission_requires_a_later_quote_and_fill_respects_limit(side, bid, ask, bound):
    # The quote that accompanies the signal is already eligible on price.
    # Even so, a resting order cannot consume it.
    state = rest(start(side), side, lag=2, bid=bid, ask=ask)
    order = state.account.order(state.resting.order_id)
    assert order.kind == 'LIMIT' and order.price_bound == D(bound)
    # This point is new, but its venue timestamp precedes the order.
    stale = observe(state, batch(state, 61, quote_time=state.started_at_ns + 59*NS))
    assert stale.state.last_received_ns > state.last_received_ns
    assert stale.state.resting == state.resting and stale.state.opened is None
    result = observe(stale.state, batch(state, 70, bid=bid, ask=ask))
    assert result.state.resting is None
    position = result.state.opened
    assert position.entry_price == D(bound)
    assert position.entry_time_ns == state.started_at_ns + 70*NS
    assert position.signal_known_at_ns == state.started_at_ns + 60*NS
    fills = [event for event in result.journal if event.kind == 'FILL']
    assert len(fills) == 1 and fills[0].price == D(bound)
    assert fills[0].fee == fills[0].price * abs(fills[0].size_delta) * D('0.001')


def test_expiry_precedes_touch_at_boundary_and_releases_reservation():
    state = rest(start('LONG'), 'LONG', bid='99', ask='100')
    order_id = state.resting.order_id
    for second in range(70, 120, 10):
        state = observe(state, batch(state, second)).state
        assert state.resting.order_id == order_id
    neutral = bar(state.started_at_ns//NS + 60, c='100')
    result = observe(state, batch(state, 120, bid='98', ask='99', bars=(neutral,)))
    assert not result.state.resting and not result.state.opened
    assert result.state.account.order(order_id).reason == 'TIME_IN_FORCE_EXPIRED'
    assert result.state.account.cash == D('1000') and result.state.account.fees_paid == 0
    assert not any(event.kind == 'FILL' for event in result.journal)


@pytest.mark.parametrize(('stop', 'status', 'reason'), [
    (True, 'STOPPED', 'USER_CANCELLED'), (False, 'GAP', 'OBSERVATION_INTERRUPTED')])
def test_control_and_missing_coverage_cancel_pending_without_fill(stop, status, reason):
    state = rest(start('LONG'), 'LONG', bid='99', ask='100')
    result = (stop_observed(state, state.started_at_ns + 70*NS) if stop
              else observe(state, batch(state, 90, bid='98', ask='99')))
    assert result.state.status == status
    assert result.state.resting is None and result.state.opened is None
    assert result.state.account.order(state.resting.order_id).reason == reason
    assert result.state.account.fees_paid == 0
    assert not any(event.kind == 'FILL' for event in result.journal)


def test_limit_encrypted_replay_and_real_gate(tmp_path, monkeypatch):
    lab, paper, archive, origin, seed, clock = owned_archive(tmp_path, monkeypatch, limited=True)
    with pytest.raises(ValueError):
        read_observed_seed({**seed.payload(), 'engine': 'perp-observed-1.0'})
    identifier = archive.create(origin, seed, request_id=uid())['id']
    state = seed.start().state
    for second in range(10, 61, 10):
        bars = (bar(state.started_at_ns//NS),) if second == 60 else ()
        event = observation_input(batch(state, second, bars=bars))
        clock['now'] = int(event.received_at_ns)
        archive.append(identifier, event, request_id=uid())
    report = archive.read(identifier)
    snapshot = report['session']['checkpoint']
    assert snapshot['schema'] == 'orbis.paper.perp.observed.checkpoint.v2'
    assert snapshot['resting']['order_id'] == snapshot['account']['orders'][0]['id']
    assert D(snapshot['order_reservation']['available']) < D(snapshot['account']['cash'])
    cold = ObservedArchive(paper, clock_ns=lambda: clock['now'])
    assert cold.read(identifier)['session']['status'] == 'RESTARTED'
    assert cold.read(identifier)['session']['checkpoint'] == snapshot
    assert not cold.active_ids()
    exported = verify_observed_export(io.BytesIO(b''.join(archive.export_lines(identifier))))
    assert exported['checkpoint'] == snapshot and exported['execution_authority'] == 'NONE'
    assert verify_paper_perp_database(paper.path, PAPER_KEY)['counts']['paper_observed_sessions'] == 1
    with pytest.raises(ValueError, match='LIMIT'):
        review_perp_live(lab, archive, identifier, '1', object())
