"""LIMIT reservations use independent accounts and the versioned child matcher."""
from decimal import Decimal as D, localcontext, ROUND_DOWN

import pytest

from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_profile import read_profile
from app.lab.perp_simulation import LIMIT_ENGINE, simulate_perpetual
from app.lab.perp_validation import PerpValidationPlan, calculate_perp_validation
from app.lab.store import content_hash
from tests.perp_validation_fixture import validation_inputs
from tests.perp_simulation_fixture import definition
from tests.test_perp_limit_simulation import limit_profile


PLANS = [PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT', train_fraction='0.5'),
    PerpValidationPlan(method='ROLLING_FIXED_VERSION', folds=3),
    PerpValidationPlan(method='PATH_SENSITIVITY')]


def limit_inputs(monkeypatch, direction='LONG', count=120, offset='0.01', valid_bars=3):
    data, strategy, _ = validation_inputs(monkeypatch, count)
    if direction == 'SHORT':
        raw = data.payload()
        for bar in raw['trade']['bars']:
            bar.update(o='101', h='102', l='99', c='100')
        data = read_perp_dataset(raw)
        strategy = definition('SHORT', fee_rate='0', stop_loss='0.5', take_profit='0.5')
    return data, strategy, limit_profile(strategy, offset=offset, valid_bars=valid_bars)


@pytest.mark.parametrize('plan', PLANS)
@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_limit_windows_reproduce_the_actual_matcher_and_independent_cash(monkeypatch, direction, plan):
    data, strategy, profile = limit_inputs(monkeypatch, direction)
    result = calculate_perp_validation(data, profile, strategy, plan)
    assert result['schema'] == 'orbis.lab.perp.validation.result.v2'
    assert result['engine'] == 'perp-validation-2.0' and result['simulation_engine'] == LIMIT_ENGINE
    assert result['execution_authority'] == 'NONE'
    for window in result['windows']:
        selected = read_profile(window['profile'])
        assert selected.entry_order == profile.entry_order
        child = simulate_perpetual(data, selected, strategy,
            entry_start_index=window['entry_start_index'], end_index=window['end_index'])
        assert content_hash(child) == window['result_hash'] and child['metrics'] == window['metrics']
        trade = child['trades'][0]
        assert D(trade['quantity']) == D('24.999')
        assert D(trade['entry_price']) == D('99.99' if direction == 'LONG' else '101')
        assert D(window['metrics']['final_equity']) == D('1025.24899' if direction == 'LONG' else '1024.999')
        assert int(trade['entry_time_ns']) >= window['from_t'] * 10**9
    reserves = [window for window in result['windows'] if window['role'] == 'RESERVE']
    assert all(left['end_index'] == right['entry_start_index'] for left, right in zip(reserves, reserves[1:]))
    if plan.method == 'PATH_SENSITIVITY':
        assert not reserves and 'PATH_SCENARIOS_ARE_NOT_OUT_OF_SAMPLE' in result['warnings']
        assert len({(w['profile']['trade_path'], w['profile']['mark_path']) for w in result['windows']}) == 4


def test_limit_development_and_its_orders_do_not_depend_on_future_prices(monkeypatch):
    data, strategy, profile = limit_inputs(monkeypatch, count=60)
    plan = PLANS[0]
    original = calculate_perp_validation(data, profile, strategy, plan)
    raw = data.payload()
    for bar in raw['trade']['bars'][30:]:
        bar.update(o='150', h='160', l='145', c='155')
    changed = read_perp_dataset(raw)
    report = calculate_perp_validation(changed, profile, strategy, plan)
    assert original['windows'][0]['metrics'] == report['windows'][0]['metrics']
    before = simulate_perpetual(data, profile, strategy, end_index=30)
    after = simulate_perpetual(changed, profile, strategy, end_index=30)
    for key in ('journal', 'trades', 'signals', 'account', 'metrics'):
        assert before[key] == after[key]
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        assert calculate_perp_validation(data, profile, strategy, plan) == original


def test_an_unfilled_development_order_is_cancelled_and_a_new_reserve_order_is_created(monkeypatch):
    data, strategy, profile = limit_inputs(monkeypatch, count=60, offset='0.5', valid_bars=10000)
    result = calculate_perp_validation(data, profile, strategy, PLANS[0])
    for window in result['windows']:
        child = simulate_perpetual(data, profile, strategy,
            entry_start_index=window['entry_start_index'], end_index=window['end_index'])
        assert child['trades'] == [] and D(child['metrics']['cash']) == 1000
        assert len(child['account']['orders']) == 1
        assert child['account']['orders'][0]['reason'] == 'END_OF_WINDOW'
        signal = next(row for row in child['signals'] if 'placed_at_ns' in row)
        assert int(signal['placed_at_ns']) >= window['from_t'] * 10**9
        assert signal['warmup_context'] == (window['role'] == 'RESERVE')
