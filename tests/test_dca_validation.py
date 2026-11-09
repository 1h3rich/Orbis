"""Chronological DCA reserve keeps causal context and independent funds."""

from dataclasses import replace
from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, localcontext

import pytest

from app.automation.dca import simulate_dca, validate_dca
from app.automation.validation import chronological_dca_holdout
from app.lab.engine import LabError, validate_candles
from app.lab.store import content_hash

START, DAY = 1735689600, 86400


def bars(count=100):
    return validate_candles([{'t': START + i * DAY, 'o': str(100 + i % 7),
        'h': str(102 + i % 7), 'l': str(98 + i % 7), 'c': str(101 + i % 7), 'v': '10'}
        for i in range(count)], '1d', now=START + (count + 1) * DAY)[0]


def rule(reference='initial', **changes):
    return validate_dca({'frequency': 'daily', 'amount': '100', 'budget_total': '10000',
        'monthly_limit': '500', 'max_exposure': '0.8', 'fee_rate': '0.001',
        'slippage_rate': '0.002', 'reference_mode': reference,
        'dips': [{'drop_pct': '0.03', 'amount': '50'}], **changes})


@pytest.mark.parametrize('reference,digest', [
    ('initial', 'ea263a0c147c8a446f021ddc43b00629b6cd3a562f0c421bdcf3dc043017a378'),
    ('high_watermark', 'ab2dc332143d88ec3eaa9c7ab499dbec8825fef666217ca20d08d9c6706d4e14'),
])
def test_published_normal_dca_16_results_remain_exact(reference, digest):
    assert content_hash(simulate_dca(bars(), rule(reference), timeframe='1d')) == digest


@pytest.mark.parametrize('reference', ['initial', 'high_watermark'])
def test_reserve_resets_capital_and_limits_but_not_known_price_reference(reference):
    history = bars()
    definition = rule(reference, budget_total='500', monthly_limit='150',
                      max_exposure='1', fee_rate='0', slippage_rate='0')
    saved = chronological_dca_holdout(history, definition, '1d')
    split = saved['split_index']
    assert saved['train_bars'] == 70 and saved['test_bars'] == 30
    assert saved['train_end_t'] < saved['test_start_t']
    assert saved['train'] == simulate_dca(history[:split], definition, timeframe='1d')
    reserve = saved['out_of_sample']
    anchor = max(bar.c for bar in history[:split]) if reference == 'high_watermark' else history[0].c
    assert Decimal(reserve['reference_close']) == anchor
    for mode in ('conditional', 'fixed', 'lump'):
        result = reserve['results'][mode]
        assert result['equity'][0] == {'t': history[split - 1].t, 'value': '500'}
        assert result['events'][0]['execution_time'] == history[split].t
        assert result['events'][0]['signal_time'] == history[split - 1].t
        assert all(event['execution_time'] >= history[split].t for event in result['events'])
        assert Decimal(result['metrics']['remaining_cash']) + Decimal(result['metrics']['invested']) == 500
    assert reserve['results']['lump']['events'][0]['kind'] == 'LUMP'
    assert Decimal(reserve['results']['lump']['events'][0]['spent']) == 500


def test_future_candles_do_not_change_training_or_initial_reserve_decisions():
    history = bars()
    original = chronological_dca_holdout(history, rule('high_watermark'), '1d')
    changed = history[:72] + [replace(bar, o=Decimal('1'), h=Decimal('2'),
                                      l=Decimal('1'), c=Decimal('1')) for bar in history[72:]]
    replay = chronological_dca_holdout(changed, rule('high_watermark'), '1d')
    assert replay['train'] == original['train']
    for mode in ('conditional', 'fixed', 'lump'):
        def initial(result):
            return [row for row in result['out_of_sample']['results'][mode]['events']
                    if row['execution_time'] < history[72].t]
        assert initial(replay) == initial(original)


def test_dca_volume_reserve_uses_closed_prefix_and_resets_cash():
    history = bars()
    history[69] = replace(history[69], v=Decimal("20"))
    definition = rule(volume_gate={"period": 2, "min_ratio": "2"})
    saved = chronological_dca_holdout(history, definition, "1d")
    assert saved["dca_engine_version"] == "dca-1.9"
    conditional = saved["out_of_sample"]["results"]["conditional"]
    assert conditional["equity"][0]["value"] == "10000"
    first = conditional["events"][0]
    assert first["volume_ratio"] == "2" and first["signal_time"] == history[69].t
    assert first["execution_time"] == history[70].t
    history[70] = replace(history[70], v=Decimal("10000"))
    replay = chronological_dca_holdout(history, definition, "1d")
    assert replay["train"] == saved["train"]
    assert replay["out_of_sample"]["results"]["conditional"]["events"][0] == first


@pytest.mark.parametrize('rounding', [ROUND_HALF_EVEN, ROUND_DOWN, ROUND_UP])
def test_reserve_reference_ignores_future_high_and_context_is_independent(rounding):
    history = bars()
    history[-1] = replace(history[-1], o=Decimal('1000'), h=Decimal('1001'),
                          l=Decimal('999'), c=Decimal('1000'))
    expected = chronological_dca_holdout(history, rule('high_watermark'), '1d', '0.65')
    assert expected['split_index'] == 65
    assert expected['out_of_sample']['reference_close'] == '107'
    with localcontext() as context:
        context.prec = 1
        context.rounding = rounding
        context.Emax = 2
        context.Emin = -2
        context.traps[Inexact] = True
        assert chronological_dca_holdout(history, rule('high_watermark'), '1d', '0.65') == expected


@pytest.mark.parametrize('fraction', ['0.49', '0.81', 'NaN'])
def test_reserve_rejects_invalid_splits(fraction):
    with pytest.raises(LabError):
        chronological_dca_holdout(bars(), rule(), '1d', fraction)


def test_reserve_rejects_short_or_gapped_history_and_unsupported_engine():
    with pytest.raises(LabError):
        chronological_dca_holdout(bars(59), rule(), '1d')
    with pytest.raises(LabError):
        chronological_dca_holdout(bars()[:50] + bars()[51:], rule(), '1d')
    with pytest.raises(LabError):
        chronological_dca_holdout(bars(), rule(), '1d', dca_engine_version='dca-1.5')


@pytest.mark.parametrize('index', [True, 0, -1, 100, '70'])
def test_simulation_rejects_invalid_window(index):
    with pytest.raises(LabError):
        simulate_dca(bars(), rule(), timeframe='1d', entry_start_index=index)
