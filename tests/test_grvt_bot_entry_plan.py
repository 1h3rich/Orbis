"""Dimensionado automático, independiente del saldo ficticio PAPER."""

from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument


NOW = 1_790_000_000_000_000_000
TERMS = GrvtTestnetInstrument('BTC_USDT_Perp', 123456789, 3,
    Decimal('0.01'), Decimal('0.001'), Decimal('10'))
QUOTE = GrvtTestnetQuote(TERMS.name, NOW, NOW, Decimal('99.99'), Decimal('100.01'),
    Decimal('10'), Decimal('10'), Decimal('100'))


@pytest.mark.parametrize('direction,expected', [
    ('LONG', ('0.499', '100.01', '97.01', '108.02')),
    ('SHORT', ('0.500', '99.99', '102.98', '91.99')),
])
def test_trade_uses_executable_book_side_floor_size_and_strategy_percentages(direction, expected):
    from app.live_execution.grvt_bot_entry_plan import automatic_entry_terms
    from tests.test_signal_monitor import definition
    strategy = definition(direction)
    with localcontext() as context:
        context.prec = 6  # El cálculo no depende de la precisión del llamador.
        result = automatic_entry_terms(strategy, TERMS, QUOTE,
            entry_cap=Decimal('50'), max_spread_bps=25, max_slippage_bps=25, now_ns=NOW)
    assert tuple(format(getattr(result, key), 'f') for key in
        ('size', 'limit_price', 'stop_loss', 'take_profit')) == expected
    assert result.size * result.limit_price <= Decimal('50')


@pytest.mark.parametrize('fault', ['tiny', 'cap', 'nan', 'stale', 'spread', 'instrument', 'small_stop'])
def test_unexecutable_terms_fail_before_any_signature_or_order(fault):
    from app.live_execution.grvt_bot_entry_plan import automatic_entry_terms
    from tests.test_signal_monitor import definition
    strategy = definition('LONG'); cap = Decimal('50'); quote = QUOTE
    if fault == 'tiny': cap = Decimal('1')
    if fault == 'cap': cap = Decimal('1000.01')
    if fault == 'nan': cap = Decimal('NaN')
    if fault == 'stale': quote = replace(quote, event_time_ns=NOW-30_000_000_000)
    if fault == 'spread': quote = replace(quote, ask=Decimal('101'))
    if fault == 'instrument': quote = replace(quote, instrument='ETH_USDT_Perp')
    if fault == 'small_stop':
        quote = replace(quote, last_price=Decimal('100.02'))
        strategy['stop_loss'] = '0.00001'
    with pytest.raises(GrvtTestnetBracketError):
        automatic_entry_terms(strategy, TERMS, quote, entry_cap=cap,
            max_spread_bps=25, max_slippage_bps=25, now_ns=NOW)


def test_short_target_and_exit_minimum_must_remain_executable():
    from app.live_execution.grvt_bot_entry_plan import automatic_entry_terms
    from tests.test_signal_monitor import definition
    strategy = definition('SHORT'); strategy['take_profit'] = '1'
    with pytest.raises(GrvtTestnetBracketError):
        automatic_entry_terms(strategy, TERMS, QUOTE, entry_cap=Decimal('50'),
            max_spread_bps=25, max_slippage_bps=25, now_ns=NOW)


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_actual_hundred_usdt_minimum_requires_an_explicit_larger_entry_budget(direction):
    from app.live_execution.grvt_bot_entry_plan import automatic_entry_terms
    from tests.test_signal_monitor import definition
    instrument = replace(TERMS, min_notional=Decimal('100'))
    with pytest.raises(GrvtTestnetBracketError):
        automatic_entry_terms(definition(direction), instrument, QUOTE, entry_cap=Decimal('100'),
            max_spread_bps=25, max_slippage_bps=25, now_ns=NOW)
    result = automatic_entry_terms(definition(direction), instrument, QUOTE, entry_cap=Decimal('150'),
        max_spread_bps=25, max_slippage_bps=25, now_ns=NOW)
    assert min(result.limit_price, result.stop_loss, result.take_profit)*result.size >= 100
    assert result.size*result.limit_price <= 150
