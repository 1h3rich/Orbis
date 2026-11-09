from decimal import Decimal

import pytest

from app.lab import perp_optimization as grid
from app.lab.engine import LabError
from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_profile import read_profile
from tests.perp_validation_fixture import validation_inputs


PLAN = grid.PerpOptimizationPlan(stop_losses=['0.02', '0.01'],
                                 take_profits=['0.03', '0.01'], train_fraction='0.50')


def test_grid_actual_engine_and_reserve_cannot_change_selection(monkeypatch):
    dataset, definition, profile = validation_inputs(monkeypatch)
    calculate = grid.simulate_perpetual
    calls = []

    def simulate(data, settings, strategy, *, entry_start_index, end_index):
        calls.append((entry_start_index, end_index, strategy['stop_loss'], strategy['take_profit']))
        return calculate(data, settings, strategy,
                         entry_start_index=entry_start_index, end_index=end_index)

    monkeypatch.setattr(grid, 'simulate_perpetual', simulate)
    original = grid.calculate_perp_optimization(dataset, profile, definition, PLAN)
    winner = original['candidates'][original['selected_index']]
    assert winner['take_profit'] == '0.01'
    assert winner['development']['metrics']['trades'] >= 10
    assert Decimal(winner['development']['metrics']['net_equity_change']) > 0
    assert calls == [(0, 60, '0.01', '0.01'), (0, 60, '0.01', '0.03'),
                     (0, 60, '0.02', '0.01'), (0, 60, '0.02', '0.03'),
                     (60, 120, winner['stop_loss'], winner['take_profit'])]
    assert original['out_of_sample']['from_t'] - winner['development']['to_t'] == 3600
    assert original['execution_authority'] == 'NONE'
    raw = dataset.payload()
    for bar in raw['trade']['bars'][60:]:
        bar.update(h='100', l='97', c='98')
    changed = grid.calculate_perp_optimization(read_perp_dataset(raw), profile, definition, PLAN)
    assert changed['selected_index'] == original['selected_index']
    for first, second in zip(original['candidates'], changed['candidates'], strict=True):
        assert first['development']['metrics'] == second['development']['metrics']
        assert first['exclusions'] == second['exclusions']
    assert changed['out_of_sample']['metrics'] != original['out_of_sample']['metrics']


def test_empty_selection_does_not_simulate_reserve(monkeypatch):
    dataset, definition, profile = validation_inputs(monkeypatch)
    calculate = grid.simulate_perpetual
    calls = []

    def simulate(data, settings, strategy, *, entry_start_index, end_index):
        calls.append(entry_start_index)
        return calculate(data, settings, strategy,
                         entry_start_index=entry_start_index, end_index=end_index)

    monkeypatch.setattr(grid, 'simulate_perpetual', simulate)
    plan = grid.PerpOptimizationPlan(stop_losses=['0.5'], take_profits=['0.5'])
    result = grid.calculate_perp_optimization(dataset, profile, definition, plan)
    assert calls == [0]
    assert result['selected_index'] is result['out_of_sample'] is None
    assert result['candidates'][0]['exclusions'] == ['INSUFFICIENT_TRADES']


def test_rank_breaks_ties_by_drawdown_and_never_reselects_after_failed_reserve(monkeypatch):
    dataset, definition, profile = validation_inputs(monkeypatch)
    candidates = {
        ('0.01', '0.01'): ('100', '3', False),
        ('0.01', '0.03'): ('100', '2', False),
        ('0.02', '0.01'): ('110', '1', True),
        ('0.02', '0.03'): ('100', '2', False),
    }

    def simulate(data, settings, strategy, *, entry_start_index, end_index):
        key = (strategy['stop_loss'], strategy['take_profit'])
        net, drawdown, liquidated = candidates[key]
        if entry_start_index:
            assert key == ('0.01', '0.03')
        return {'status': 'HALTED_EXECUTION' if entry_start_index else 'COMPLETE', 'halt_reason': None,
                'metrics': {'trades': 12, 'open_position': bool(entry_start_index), 'liquidated': liquidated,
                            'net_equity_change': net, 'max_drawdown_pct': drawdown}, 'warnings': []}

    monkeypatch.setattr(grid, 'simulate_perpetual', simulate)
    result = grid.calculate_perp_optimization(dataset, profile, definition, PLAN)
    assert result['selected_index'] == 1
    assert result['out_of_sample']['status'] == 'HALTED_EXECUTION'
    assert result['candidates'][2]['exclusions'] == ['LIQUIDATED']


def test_decimal_grid_and_bounds_validate_before_running(monkeypatch):
    with pytest.raises(ValueError, match='duplicados'):
        grid.PerpOptimizationPlan(stop_losses=['0.01', '1e-2'], take_profits=['0.1'])
    with pytest.raises(ValueError):
        grid.PerpOptimizationPlan(stop_losses=[0.01], take_profits=['0.1'])
    dataset, definition, profile = validation_inputs(monkeypatch, direction='SHORT')
    monkeypatch.setattr(grid, 'simulate_perpetual', lambda *_, **__: pytest.fail('premature simulation'))
    with pytest.raises(LabError, match='SHORT'):
        grid.calculate_perp_optimization(dataset, profile, definition,
            grid.PerpOptimizationPlan(stop_losses=['0.02'], take_profits=['0.1', '1']))
    partial = read_profile({**profile.payload(), 'schema':'orbis.lab.perp.profile.v3',
                            'partial_targets':[{'target':'0.02','fraction':'0.2'}]})
    with pytest.raises(LabError, match='parcial'):
        grid.calculate_perp_optimization(dataset, partial, definition, PLAN)
    monkeypatch.setattr(grid, 'MAX_WORK', 300)
    with pytest.raises(LabError, match='Reduce'):
        grid.calculate_perp_optimization(dataset, profile, definition, PLAN)
    assert PLAN.payload() == {'stop_losses':['0.01','0.02'], 'take_profits':['0.01','0.03'],
                              'train_fraction':'0.5'}


@pytest.mark.parametrize('partial', [False, True])
def test_short_limit_and_partial_variants_run_in_their_own_engine(monkeypatch, partial):
    dataset, definition, profile = validation_inputs(monkeypatch, direction='SHORT')
    configuration = {
        **profile.payload(),
        'schema': 'orbis.lab.perp.profile.v3' if partial else 'orbis.lab.perp.profile.v2',
        'execution': 'LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE',
        'end_action': 'CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE',
        'entry_order': {'offset':'0', 'valid_bars':2},
    }
    if partial:
        configuration['partial_targets'] = [{'target':'0.005','fraction':'0.25'}]
    profile = read_profile(configuration)
    plan = grid.PerpOptimizationPlan(stop_losses=['0.03'], take_profits=['0.01', '0.05'],
                                     train_fraction='0.5')
    result = grid.calculate_perp_optimization(dataset, profile, definition, plan)
    assert result['simulation_engine'] == ('perp-ohlc-3.0' if partial else 'perp-ohlc-2.0')
    assert result['selected_index'] == 0
    assert result['out_of_sample']['metrics']['trades'] >= 10
    assert all(row['development']['status'] == 'COMPLETE' for row in result['candidates'])
