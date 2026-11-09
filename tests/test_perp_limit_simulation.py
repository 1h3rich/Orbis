"""LIMIT touch hypotheses with independent prices, fees and causal boundaries."""
from decimal import Decimal as D, ROUND_CEILING, localcontext

import pytest

from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_profile import read_profile
from app.lab.perp_simulation import LIMIT_ENGINE, simulate_perpetual
from tests.perp_simulation_fixture import dataset, definition, profile


def limit_profile(strategy, *, offset='0.02', valid_bars=3, **changes):
    payload = profile(strategy, **changes).payload()
    payload.update(schema='orbis.lab.perp.profile.v2',
        execution='LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE',
        end_action='CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE',
        entry_order={'reference':'CLOSED_SIGNAL_TRADE_CLOSE', 'offset':offset, 'valid_bars':valid_bars})
    return read_profile(payload)


@pytest.mark.parametrize('direction,entry', [('LONG', '98'), ('SHORT', '102')])
@pytest.mark.parametrize('mode', ['ISOLATED', 'CROSS'])
def test_limit_entry_waits_for_its_price_and_keeps_exact_costs(monkeypatch, direction, entry, mode):
    data = dataset(monkeypatch, direction)
    strategy = definition(direction, stop_loss='0.5', take_profit='0.5')
    configured = limit_profile(strategy, margin_mode=mode)
    result = simulate_perpetual(data, configured, strategy)
    assert result['engine'] == LIMIT_ENGINE and result['metrics']['trades'] == 1
    trade = result['trades'][0]
    assert D(trade['entry_price']) == D(entry)
    assert int(trade['entry_time_ns']) > data.trade.bars[2].t * 10**9
    assert trade['exit_reason'] == 'END_OF_WINDOW_ARTIFICIAL_CLOSE'
    assert [order['kind'] for order in result['account']['orders']] == ['LIMIT', 'MARKET']
    with localcontext() as ctx:
        ctx.prec = 80
        quantity, price, close = D(trade['quantity']), D(entry), D(trade['exit_price'])
        quantum = D('1e-18')
        fees = sum((quantity * value * D('.001')).quantize(quantum, rounding=ROUND_CEILING)
            for value in (price, close))
        profit = quantity * (close - price) * (1 if direction == 'LONG' else -1) - fees
    assert D(trade['fees']) == fees and D(trade['net_profit']) == profit
    assert D(result['metrics']['cash']) == D(1000) + profit
    assert result['signals'][0]['action'] == 'ENTERED_LIMIT'
    assert result['real_authority'] is result['paper_handoff_available'] is False


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_untouched_limit_releases_reserve_without_trade_or_fee(monkeypatch, direction):
    strategy = definition(direction)
    result = simulate_perpetual(dataset(monkeypatch, direction), limit_profile(strategy, offset='0.5'), strategy)
    assert result['trades'] == [] and D(result['metrics']['cash']) == 1000
    assert D(result['metrics']['fees_paid']) == 0
    assert len(result['account']['orders']) == 1
    assert result['account']['orders'][0]['status'] == 'CANCELLED'
    assert result['account']['orders'][0]['reason'] == 'END_OF_WINDOW'
    assert result['signals'][0]['action'] == 'LIMIT_CANCELLED_AT_WINDOW_END'


def test_expiry_precedes_a_favorable_open_and_never_reuses_the_signal_bar(monkeypatch):
    raw = dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(o='100', h='105', l='99', c='100')
    raw['trade']['bars'][3].update(o='95', h='96', l='94', c='95')
    strategy = definition()
    result = simulate_perpetual(read_perp_dataset(raw), limit_profile(strategy, valid_bars=1), strategy)
    assert result['trades'] == []
    assert result['account']['orders'][0]['reason'] == 'TIME_IN_FORCE_EXPIRED'
    assert result['signals'][0]['action'] == 'LIMIT_EXPIRED'


@pytest.mark.parametrize('direction,path,trade_bar,last_bar', [
    ('LONG', 'O_H_L_C', ('100','150','98','100'), ('95','100','94','95')),
    ('SHORT', 'O_L_H_C', ('100','102','50','100'), ('105','106','100','105')),
])
def test_trailing_excludes_the_extreme_before_the_intrabar_entry(monkeypatch, direction, path, trade_bar, last_bar):
    raw = dataset(monkeypatch, direction).payload()
    for bar, values in zip(raw['trade']['bars'][2:], (trade_bar, last_bar)):
        bar.update(dict(zip(('o','h','l','c'), values)))
    strategy = definition(direction, stop_loss='0.8', take_profit='0.9', trailing_stop='0.1')
    result = simulate_perpetual(read_perp_dataset(raw), limit_profile(strategy, offset='0.01', trade_path=path), strategy)
    assert len(result['trades']) == 1
    assert result['trades'][0]['exit_reason'] == 'END_OF_WINDOW_ARTIFICIAL_CLOSE'


def test_mark_risk_is_rechecked_at_touch_without_a_fake_fill(monkeypatch):
    raw = dataset(monkeypatch).payload()
    raw['mark']['bars'][2].update(o='100', h='1000', l='100', c='100')
    strategy = definition(stop_loss='0.5', take_profit='0.5')
    result = simulate_perpetual(read_perp_dataset(raw), limit_profile(strategy), strategy)
    assert result['trades'] == [] and D(result['metrics']['fees_paid']) == 0
    assert result['signals'][0]['action'] == 'ENTRY_PRE_TRADE_CANCELLED'
    assert not any(entry['kind'] == 'FILL' for entry in result['journal'])


def test_warmup_and_marketability_use_known_signal_close_and_price_improvement(monkeypatch):
    raw = dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(o='90', h='100', l='90', c='100')
    strategy = definition(stop_loss='0.5', take_profit='0.5', slippage_rate='0.01')
    result = simulate_perpetual(read_perp_dataset(raw), limit_profile(strategy), strategy, entry_start_index=2)
    signal = result['signals'][0]
    assert signal['warmup_context'] and signal['limit_reference'] == '100'
    assert signal['limit_price'] == '98.00'
    assert D(result['trades'][0]['entry_price']) == D('90.9')
    assert result['trades'][0]['entry_time_ns'] == str(raw['trade']['bars'][2]['t'] * 10**9)


@pytest.mark.parametrize('offset_seconds,expected_rate', [(1200, '0'), (2400, '0.001')])
def test_funding_at_a_limit_touch_boundary_uses_actual_position_time(monkeypatch, offset_seconds, expected_rate):
    raw = dataset(monkeypatch).payload()
    target = (raw['trade']['bars'][2]['t'] + offset_seconds) * 10**9
    for funding in raw['funding']:
        funding['funding_time'] = str(int(funding['funding_time']) + offset_seconds * 10**9)
        funding['funding_rate'] = '0.1' if int(funding['funding_time']) == target else '0'
    strategy = definition(stop_loss='0.5', take_profit='0.5')
    result = simulate_perpetual(read_perp_dataset(raw), limit_profile(strategy), strategy)
    trade = result['trades'][0]
    assert D(trade['funding_paid']) == D(trade['quantity']) * D(100) * D(expected_rate)


def test_later_prices_do_not_change_the_signal_order_or_entry(monkeypatch):
    raw = dataset(monkeypatch).payload()
    strategy = definition(stop_loss='0.5', take_profit='0.5')
    configured = limit_profile(strategy)
    before = simulate_perpetual(read_perp_dataset(raw), configured, strategy)
    raw['trade']['bars'][3].update(o='140', h='145', l='135', c='140')
    after = simulate_perpetual(read_perp_dataset(raw), configured, strategy)
    assert before['signals'][0] == after['signals'][0]
    for key in ('entry_time_ns', 'entry_price', 'quantity', 'signal_known_at_ns'):
        assert before['trades'][0][key] == after['trades'][0][key]


def test_zero_distance_has_one_stable_profile_representation():
    strategy = definition()
    assert limit_profile(strategy, offset='-0').payload() == limit_profile(strategy, offset='0.000').payload()


@pytest.mark.parametrize('entry', [
    {'offset':'-0.1', 'valid_bars':1}, {'offset':'1', 'valid_bars':1},
    {'offset':'NaN', 'valid_bars':1}, {'offset':0.1, 'valid_bars':1},
    {'offset':'0.1', 'valid_bars':True}, {'offset':'0.1', 'valid_bars':0},
])
def test_invalid_limit_configuration_is_rejected(entry):
    payload = limit_profile(definition()).payload()
    payload['entry_order'] = entry
    with pytest.raises(ValueError):
        read_profile(payload)
