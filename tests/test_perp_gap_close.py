"""Financial oracles for a conditional closure, without inventing gap fills."""
from dataclasses import replace
from decimal import Decimal as D, localcontext, ROUND_DOWN

import pytest

from app.derivatives.funding import FundingRecord
from app.derivatives.perp_gap_close import GapCloseInput, estimate_gap_close
from app.derivatives.perp_observed import observed_gap
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_wire import (
    funding_input, quote_input, rules_input, specification_input,
)
from tests.perp_observed_fixture import BTC, NOW, NS, batch, enter, initial


def close_input(point):
    return GapCloseInput.model_validate({
        'schema': 'orbis.paper.perp.gap-close.input.v1', 'engine': 'perp-gap-close-1.0',
        'acknowledgement': 'UNKNOWN_INTERVAL_ACCEPTED',
        'received_at_ns': str(point.received_at_ns), 'quote': quote_input(point.quote).payload(),
        'rules': rules_input(point.rules).model_dump(),
        'specification': specification_input(point.specification).payload(),
        'funding': [funding_input(row).model_dump() for row in point.funding],
    })


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_gap_exit_current_bid_ask_cost_oracle_and_original_unchanged(side, mode):
    state, _ = enter(side, mode)
    state = observed_gap(state, NOW + 90 * NS, 'OWNED_GAP').state
    before = state
    point = batch(state, 100, bid='103' if side == 'LONG' else '96',
                  ask='104' if side == 'LONG' else '97')
    receipt = close_input(point)
    result = estimate_gap_close(state, receipt)
    # Both directions earn three per unit; both pay entry100 and exit103/97 fees.
    exit_price = D('103' if side == 'LONG' else '97')
    expected = D('1000') + D('24.875') * (3 - D('.1') - exit_price * D('.001'))
    assert D(result['metrics']['cash']) == expected
    assert result['status'] == 'ESTIMATED_CLOSED' and result['quality'] == 'ESTIMATED_GAP_CLOSE'
    assert result['exit_price'] == str(exit_price) and result['exit_time_ns'] == str(NOW + 100 * NS)
    assert result['execution_authority'] == 'NONE' and result['reconstructed']
    assert result['unknown_interval'] == {'from_ns': str(NOW + 60 * NS), 'through_ns': str(NOW + 100 * NS)}
    assert state == before and state.account.positions and not result['account']['positions']
    assert all(int(row['cursor']['time_ns']) == NOW + 100 * NS for row in result['journal'])
    assert sum(row['kind'] == 'FILL' for row in result['journal']) == 1
    assert 'NO_ORDERS_OR_STOPS_RECONSTRUCTED' in result['assumptions']


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_real_funding_points_count_once_before_exit(side, mode):
    state, _ = enter(side, mode, start=NOW + 3480 * NS, fee='0')
    record = FundingRecord(BTC, NOW + 3600 * NS, D('1'), D('100'), 1)
    point = batch(state, 130, bid='103' if side == 'LONG' else '96',
                  ask='104' if side == 'LONG' else '97', funding=(state.funding_anchor, record))
    result = estimate_gap_close(state, close_input(point))
    funding = D('24.999') if side == 'LONG' else D('-24.999')
    assert D(result['metrics']['funding_paid']) == funding
    assert D(result['metrics']['cash']) == D('1000') + D('24.999') * 3 - funding
    kinds = [row['kind'] for row in result['journal']]
    assert kinds.count('FUNDING') == 1 and kinds.index('FUNDING') < kinds.index('FILL')
    assert result['known_at_ns'] == str(point.received_at_ns)


@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_sampled_mark_liquidation_is_estimated_and_does_not_fabricate_fill(mode):
    state, _ = enter(mode=mode)
    result = estimate_gap_close(state, close_input(batch(state, 100, bid='109', ask='110', mark='60')))
    assert result['status'] == 'ESTIMATED_LIQUIDATED' and result['exit_price'] is None
    assert not any(row['kind'] == 'FILL' for row in result['journal'])
    assert D(result['metrics']['cash']) == (D('500.0125') if mode == 'ISOLATED' else D(0))
    assert result['quality'] == 'ESTIMATED_GAP_CLOSE' and result['execution_authority'] == 'NONE'


def test_known_funding_mark_can_estimate_earlier_liquidation_without_claiming_observation():
    state, _ = enter(start=NOW + 3480 * NS, fee='0')
    record = FundingRecord(BTC, NOW + 3600 * NS, D('1'), D('20'), 1)
    point = batch(state, 130, bid='109', ask='110', funding=(state.funding_anchor, record))
    result = estimate_gap_close(state, close_input(point))
    assert result['status'] == 'ESTIMATED_LIQUIDATED'
    assert result['exit_time_ns'] == str(record.time_ns) and result['known_at_ns'] == str(point.received_at_ns)
    assert not any(row['kind'] == 'FILL' for row in result['journal'])
    assert result['reconstructed'] and result['quality'] == 'ESTIMATED_GAP_CLOSE'


@pytest.mark.parametrize('fault', ['STALE', 'FUTURE', 'WRONG_INSTRUMENT', 'OLD_QUOTE', 'BEFORE_CONTROL', 'MISSING_FUNDING', 'REVISED_ANCHOR', 'RETROACTIVE_FUNDING'])
def test_bad_data_never_silently_close_or_change_the_original(fault):
    state, _ = enter()
    point = batch(state, 100, bid='103', ask='104')
    if fault == 'STALE': point = replace(point, quote=replace(point.quote, event_time_ns=point.received_at_ns - 16 * NS))
    if fault == 'FUTURE': point = replace(point, quote=replace(point.quote, event_time_ns=point.received_at_ns + 1))
    if fault == 'WRONG_INSTRUMENT': point = replace(point, quote=replace(point.quote, instrument='ETH_USDT_Perp'))
    if fault == 'OLD_QUOTE': point = replace(point, quote=state.last_quote)
    if fault == 'BEFORE_CONTROL': state = observed_gap(state, point.received_at_ns + 1, 'GAP').state
    if fault == 'MISSING_FUNDING': point = batch(state, 3700)
    if fault == 'REVISED_ANCHOR': point = replace(point, funding=(replace(state.funding_anchor, rate_percent=D('1')),))
    if fault == 'RETROACTIVE_FUNDING':
        anchor = replace(state.funding_anchor, time_ns=state.last_received_ns - 3600 * NS)
        state = replace(state, funding_anchor=anchor)
        point = replace(point, funding=(anchor, replace(anchor, time_ns=state.last_received_ns)))
    before = state
    with pytest.raises(ValueError): estimate_gap_close(state, close_input(point))
    assert state == before and state.account.positions


def test_no_position_or_false_acknowledgement_is_rejected():
    state = initial()
    with pytest.raises(ObservedError, match='posición'): estimate_gap_close(state, close_input(batch(state, 100)))
    state, _ = enter()
    raw = close_input(batch(state, 100)).payload()
    raw['acknowledgement'] = 'ASSUME_CONTINUOUS_COVERAGE'
    with pytest.raises(ValueError): GapCloseInput.model_validate(raw)


def test_decimal_context_cannot_change_the_result():
    state, _ = enter()
    source = close_input(batch(state, 100, bid='103.02', ask='104.01'))
    expected = estimate_gap_close(state, source)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        assert estimate_gap_close(state, source) == expected
