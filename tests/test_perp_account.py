"""Independent accounting examples; no market client, user store or wallet."""
from dataclasses import replace
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext

import pytest

from app.derivatives.paper_math import InstrumentSpec, MarginRules
from app.lab.perp_book_types import Cursor, Market, PerpAccountError
from app.lab.perp_account import (
    assert_accounting, cancel_order, equity, fill_order, mark_all, new_account,
    replace_rules, reservations, settle_funding, submit_order,
)


D = Decimal
BTC = 'BTC_USDT_Perp'
ETH = 'ETH_USDT_Perp'
SOL = 'SOL_USDT_Perp'
NOW = 1735689600000000000


def market(mode='CROSS', name=BTC, *, initial='0.2', maintenance='0.05', fee='0.001', max_size='1000'):
    rules = MarginRules.from_payload({'instrument': name, 'fetched_at_ns': NOW,
        'max_position_size': max_size, 'risk_brackets': [{'tier': 1,
        'notional_floor': '0', 'notional_cap': None, 'initial_margin_rate': initial,
        'maintenance_margin_rate': maintenance, 'cumulative_maintenance_amount': '0', 'max_leverage': 5}]}, name, NOW)
    spec = InstrumentSpec.from_payload({'instrument': name, 'fetched_at_ns': NOW,
        'kind': 'PERPETUAL', 'venues': ['ORDERBOOK'], 'base_decimals': 3,
        'min_size': '0.001', 'min_notional': '1', 'tick_size': '0.01', 'max_position_size': max_size}, name, NOW)
    return Market(name, mode, D('5'), D(fee), spec, rules)


class Driver:
    def __init__(self, *markets, capital='1000'):
        self.book = new_account(D(capital), markets or (market(),))
        self.time, self.ordinal, self.order_number = NOW, -1, 0
        self.entries = []
        self.mark(**{m.instrument: '100' for m in self.book.markets})

    def cursor(self):
        self.ordinal += 1
        return Cursor(self.time, self.ordinal)

    def accept(self, result):
        previous = self.book
        self.book = result.account
        assert_accounting(self.book)
        # Every cash movement is journaled, including liquidations. This check
        # doesn't invoke the production reservation/P&L formulas.
        assert self.book.cash-previous.cash == sum((e.cash_delta for e in result.entries), D('0'))
        before_collateral = sum((p.collateral for p in previous.positions), D('0'))
        after_collateral = sum((p.collateral for p in self.book.positions), D('0'))
        assert after_collateral-before_collateral == sum((e.collateral_delta for e in result.entries), D('0'))
        for attribute, journal in [('realized_pnl','realized_pnl'), ('fees_paid','fee'),
                ('funding_paid','funding_payment'), ('insurance_transferred','insurance_transfer'),
                ('insurance_covered','insurance_cover')]:
            assert getattr(self.book,attribute)-getattr(previous,attribute) == sum(
                (getattr(e,journal) for e in result.entries),D('0'))
        self.entries.extend(result.entries)
        return result

    def mark(self, **prices):
        self.time += 1
        self.ordinal = -1
        return self.accept(mark_all(self.book, self.cursor(), {k: D(v) for k, v in prices.items()}))

    def submit(self, side, quantity, price='100', *, name=BTC, reduce=False, kind='MARKET'):
        self.order_number += 1
        identifier = str(self.order_number)
        self.accept(submit_order(self.book, self.cursor(), identifier=identifier, instrument=name,
            side=side, quantity=D(quantity), price_bound=D(price), reduce_only=reduce, kind=kind))
        return identifier

    def fill(self, identifier, quantity, price):
        return self.accept(fill_order(self.book, self.cursor(), identifier, quantity=D(quantity), price=D(price)))

    def trade(self, side, quantity, price='100', *, name=BTC, reduce=False):
        identifier = self.submit(side, quantity, price, name=name, reduce=reduce)
        return self.fill(identifier, quantity, price)

    def funding(self, rate, *, name=BTC, price='100'):
        return self.accept(settle_funding(self.book, self.cursor(), instrument=name,
            rate_percent=D(rate), settlement_mark=D(price)))


@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_manual_increase_partial_close_reversal_and_funding(mode):
    driver = Driver(market(mode))
    rows = [
        ('BUY', '2', '100', '2', '200', '0', '0.20', '999.80', '959.80', '40'),
        ('BUY', '1', '130', '3', '330', '0', '0.33', '999.67', '921.67', '78'),
        ('SELL', '1', '140', '2', '220', '30', '0.47', '1029.53', '977.53', '52'),
        ('SELL', '3', '90', '-1', '90', '-10', '0.74', '989.26', '971.26', '18'),
    ]
    for side, qty, price, size, basis, realized, fees, cross_cash, isolated_cash, collateral in rows:
        driver.mark(**{BTC: price})
        result = driver.trade(side, qty, price)
        book = driver.book
        position = book.position(BTC)
        assert position.size == D(size) and position.basis == D(basis)
        assert book.realized_pnl == D(realized) and book.fees_paid == D(fees)
        assert book.cash == D(cross_cash if mode == 'CROSS' else isolated_cash)
        assert position.collateral == D('0' if mode == 'CROSS' else collateral)
        assert result.entries[0].kind == 'FILL'
    assert equity(driver.book) == D('989.26')
    result = driver.funding('0.1', price='90')
    assert result.entries[0].funding_payment == D('-0.09')
    assert driver.book.funding_paid == D('-0.09')
    assert equity(driver.book) == D('989.35')


def test_pending_extremes_cancellation_and_reserves_do_not_spend_cash():
    driver = Driver()
    driver.trade('BUY', '1')
    cash = driver.book.cash
    driver.submit('BUY', '1.5', kind='LIMIT')
    sell = driver.submit('SELL', '2.6', kind='LIMIT')
    reserved = reservations(driver.book)
    assert reserved.cross_initial == D('52') and reserved.fees == D('0.41')
    assert driver.book.position(BTC).size == D('1') and driver.book.cash == cash
    driver.accept(cancel_order(driver.book, driver.cursor(), sell))
    reserved = reservations(driver.book)
    assert reserved.cross_initial == D('50') and reserved.fees == D('0.15')
    assert driver.book.cash == cash and driver.book.realized_pnl == 0


def test_partial_fill_releases_only_executed_quantity_and_fee():
    driver = Driver()
    order = driver.submit('BUY', '3')
    driver.fill(order, '1', '90')
    assert driver.book.order(order).remaining == D('2')
    assert driver.book.order(order).status == 'PARTIAL'
    assert driver.book.cash == D('999.91')
    assert driver.book.position(BTC).basis == D('90')
    reserved = reservations(driver.book)
    assert reserved.cross_initial == D('60') and reserved.fees == D('0.2')
    before = reserved.available
    driver.accept(cancel_order(driver.book, driver.cursor(), order))
    assert reservations(driver.book).available - before == D('40.2')
    assert driver.book.cash == D('999.91') and driver.book.position(BTC).size == D('1')


@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_reduce_only_is_revalidated_after_another_fill(mode):
    driver = Driver(market(mode))
    driver.trade('BUY', '3')
    first = driver.submit('SELL', '3', reduce=True)
    second = driver.submit('SELL', '3', reduce=True)
    result = driver.fill(first, '2', '100')
    assert driver.book.order(first).status == 'PARTIAL'
    assert driver.book.order(second).status == 'CANCELLED'
    assert any(e.order_id == second and e.reason == 'REDUCE_ONLY_NO_LONGER_REDUCING' for e in result.entries)
    driver.fill(first, '1', '100')
    assert driver.book.position(BTC) is None
    assert driver.book.cash == D('999.4') and driver.book.fees_paid == D('0.6')
    before = driver.book
    with pytest.raises(PerpAccountError, match='terminal'):
        fill_order(before, driver.cursor(), second, quantity=D('3'), price=D('100'))
    assert driver.book == before


@pytest.mark.parametrize('side,rate,payment', [('BUY','0.1','0.2'), ('BUY','-0.1','-0.2'),
                                            ('SELL','0.1','-0.2'), ('SELL','-0.1','0.2')])
@pytest.mark.parametrize('mode', ['CROSS','ISOLATED'])
def test_funding_posts_signed_percent_once(mode, side, rate, payment):
    driver = Driver(market(mode))
    driver.trade(side, '2')
    driver.funding(rate)
    assert driver.book.funding_paid == D(payment)
    if mode == 'CROSS':
        assert driver.book.cash == D('999.8') - D(payment)
    else:
        assert driver.book.cash == D('959.8')
        assert driver.book.position(BTC).collateral == D('40') - D(payment)
    before = driver.book
    with pytest.raises(PerpAccountError, match='repetida'):
        settle_funding(before, driver.cursor(), instrument=BTC, rate_percent=D(rate), settlement_mark=D('100'))
    assert driver.book == before


def test_no_position_funding_is_consumed_without_later_retroactive_charge():
    driver = Driver()
    driver.funding('0.1')
    assert driver.entries[-1].kind == 'FUNDING_NO_POSITION'
    driver.trade('BUY', '1')
    with pytest.raises(PerpAccountError, match='repetida'):
        driver.funding('0.1')
    assert driver.book.funding_paid == 0


@pytest.mark.parametrize('mark,realized,transfer,covered', [('84','-32','8','0'), ('70','-60','0','20')])
def test_full_isolated_liquidation_loses_pocket_and_exposes_insurance(mark, realized, transfer, covered):
    driver = Driver(market('ISOLATED'))
    driver.trade('BUY', '2')
    pending = driver.submit('BUY', '1')
    result = driver.mark(**{BTC: mark})
    assert driver.book.position(BTC) is None and driver.book.cash == D('959.8')
    assert driver.book.realized_pnl == D(realized)
    assert driver.book.insurance_transferred == D(transfer)
    assert driver.book.insurance_covered == D(covered)
    assert driver.book.order(pending).status == 'CANCELLED'
    assert [e.kind for e in result.entries].count('FILL') == 0
    assert any(e.kind == 'ISOLATED_LIQUIDATION' and e.reason == 'MARK' for e in result.entries)


def test_funding_can_liquidate_without_price_change():
    driver = Driver(market('ISOLATED'))
    driver.trade('BUY', '2')
    driver.mark(**{BTC: '85'})
    assert driver.book.position(BTC) is not None
    result = driver.funding('1', price='85')
    assert driver.book.cash == D('959.8') and driver.book.position(BTC) is None
    assert driver.book.funding_paid == D('1.7') and driver.book.realized_pnl == D('-30')
    assert driver.book.insurance_transferred == D('8.3')
    assert any(e.kind == 'ISOLATED_LIQUIDATION' and e.reason == 'FUNDING' for e in result.entries)


@pytest.mark.parametrize('btc,eth,realized,transfer,cover', [('60','47','-930','47.9','0'),
                                                         ('40','40','-1200','0','222.1')])
def test_cross_pool_liquidation_spares_isolated_pocket(btc, eth, realized, transfer, cover):
    driver = Driver(market(name=BTC), market(name=ETH), market('ISOLATED', SOL))
    driver.trade('BUY', '1', name=SOL)
    driver.trade('BUY', '10', name=BTC)
    driver.trade('BUY', '10', name=ETH)
    opening = driver.submit('BUY', '1', name=SOL)
    cross_order = driver.submit('BUY', '1', name=BTC)
    isolated_close = driver.submit('SELL', '1', '80', name=SOL, reduce=True)
    result = driver.mark(**{BTC: btc, ETH: eth, SOL:'100'})
    assert driver.book.cash == 0 and len(driver.book.positions) == 1
    assert driver.book.position(SOL).collateral == D('20')
    assert driver.book.realized_pnl == D(realized)
    assert driver.book.insurance_transferred == D(transfer) and driver.book.insurance_covered == D(cover)
    assert driver.book.order(opening).status == driver.book.order(cross_order).status == 'CANCELLED'
    assert driver.book.order(isolated_close).active
    assert sum(e.kind == 'CROSS_POSITION_LIQUIDATED' for e in result.entries) == 2
    driver.fill(isolated_close, '1', '100')
    assert driver.book.cash == D('19.9') and not driver.book.positions


@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_admission_and_pre_trade_reject_without_fees_or_position(mode):
    driver = Driver(market(mode), capital='25')
    rejected = driver.submit('BUY', '2')
    assert driver.book.order(rejected).status == 'REJECTED'
    assert driver.book.cash == D('25') and not driver.book.positions
    accepted = driver.submit('BUY', '1')
    assert driver.book.order(accepted).active
    driver.mark(**{BTC: '200'})
    result = driver.fill(accepted, '1', '100')
    assert driver.book.order(accepted).status == 'CANCELLED'
    assert result.entries[0].reason.startswith('PRE_TRADE_')
    assert driver.book.cash == D('25') and driver.book.fees_paid == 0 and not driver.book.positions


@pytest.mark.parametrize('mode', ['CROSS', 'ISOLATED'])
def test_actual_adverse_fill_is_checked_after_fee_and_mark_loss(mode):
    driver = Driver(market(mode), capital='25')
    accepted = driver.submit('BUY', '1', '120')
    assert driver.book.order(accepted).active
    driver.fill(accepted, '1', '120')
    assert driver.book.order(accepted).status == 'CANCELLED'
    assert driver.book.cash == D('25') and not driver.book.positions


def test_rule_change_checks_maintenance_and_pending_pre_trade_independently():
    driver = Driver(market('ISOLATED'))
    driver.trade('BUY', '2')
    driver.mark(**{BTC: '90'})
    result = driver.accept(replace_rules(driver.book, driver.cursor(), instrument=BTC,
        rules=market('ISOLATED', initial='0.3', maintenance='0.15').rules))
    assert driver.book.position(BTC) is None
    assert any(e.kind == 'ISOLATED_LIQUIDATION' and e.reason == 'RULES' for e in result.entries)
    other = Driver(capital='50')
    order = other.submit('BUY', '1')
    other.accept(replace_rules(other.book, other.cursor(), instrument=BTC,
        rules=market(initial='0.8', maintenance='0.1').rules))
    assert other.book.order(order).active
    other.fill(order, '1', '100')
    assert other.book.order(order).reason == 'PRE_TRADE_INITIAL_MARGIN'
    assert other.book.cash == D('50')


def test_complete_mark_batch_freshness_and_explicit_same_time_order():
    driver = Driver(market(), market(name=ETH))
    before = driver.book
    with pytest.raises(PerpAccountError, match='todos'):
        mark_all(before, driver.cursor(), {BTC:D('100')})
    with pytest.raises(PerpAccountError, match='mismo instante'):
        submit_order(before, Cursor(driver.time+1,0), identifier='future', instrument=BTC,
            side='BUY', quantity=D('1'), price_bound=D('100'))
    with pytest.raises(PerpAccountError, match='secuencia'):
        mark_all(before, before.cursor, {BTC:D('100'),ETH:D('100')})
    assert driver.book == before


@pytest.mark.parametrize('unsafe', [True, 1, 0.1, D('NaN'), D('Infinity'), D('1e100000'), D('1e-100000')])
def test_unsafe_numbers_do_not_mutate_the_original_account(unsafe):
    driver = Driver()
    before = driver.book
    with pytest.raises(PerpAccountError):
        submit_order(before, driver.cursor(), identifier='bad', instrument=BTC,
            side='BUY', quantity=unsafe, price_bound=D('100'))
    assert driver.book == before


def test_order_id_cannot_be_reused_even_after_cancel_and_fill_is_bounded():
    driver = Driver()
    order = driver.submit('BUY','1')
    with pytest.raises(PerpAccountError, match='protección'):
        fill_order(driver.book, driver.cursor(), order, quantity=D('1'),price=D('100.01'))
    driver.accept(cancel_order(driver.book,driver.cursor(),order))
    with pytest.raises(PerpAccountError, match='repetido'):
        submit_order(driver.book,driver.cursor(),identifier=order,instrument=BTC,
            side='BUY',quantity=D('1'),price_bound=D('100'))


def test_cost_residual_and_context_are_reproducible_under_repeated_reductions():
    def run():
        driver=Driver(market('ISOLATED',fee='0'))
        driver.trade('BUY','3','100')
        driver.trade('BUY','1','100')
        for _ in range(39):
            driver.trade('SELL','0.1','100',reduce=True)
        driver.trade('SELL','0.1','100',reduce=True)
        assert not driver.book.positions and driver.book.cash==D('1000')
        return driver.book,driver.entries
    expected=run()
    with localcontext() as ctx:
        ctx.prec=5;ctx.rounding=ROUND_DOWN;ctx.traps[Inexact]=True
        actual=run()
    assert actual==expected


def test_profile_rejects_cross_instrument_or_mutable_market_configuration():
    with pytest.raises(PerpAccountError,match='otro instrumento'):
        replace(market(), rules=market(name=ETH).rules)
    with pytest.raises(PerpAccountError):
        new_account(D('1000'),[market()])
    with pytest.raises(PerpAccountError):
        new_account(D('1000'),(market(),market()))


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_rounding_residual_is_fully_assigned_when_reducing_unequal_entries(mode):
    driver=Driver(market(mode,fee='0'))
    driver.trade('BUY','1','100.01')
    driver.trade('BUY','2','100.50')
    assert driver.book.position(BTC).basis==D('301.01')
    for _ in range(30):
        driver.trade('SELL','0.1','100',reduce=True)
    assert not driver.book.positions
    assert driver.book.realized_pnl==D('-1.01') and driver.book.cash==D('998.99')


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_reduce_only_never_opens_increases_or_reverses(mode):
    driver=Driver(market(mode))
    missing=driver.submit('SELL','1',reduce=True)
    assert driver.book.order(missing).status=='REJECTED'
    driver.trade('BUY','1')
    increasing=driver.submit('BUY','1',reduce=True)
    reversing=driver.submit('SELL','1.001',reduce=True)
    assert driver.book.order(increasing).status=='REJECTED'
    assert driver.book.order(reversing).status=='REJECTED'
    assert driver.book.position(BTC).size==D('1')


def test_reduce_only_can_close_cross_below_initial_margin_after_rule_change():
    driver=Driver(capital='25')
    driver.trade('BUY','1')
    driver.accept(replace_rules(driver.book,driver.cursor(),instrument=BTC,
        rules=market(initial='0.8',maintenance='0.1').rules))
    assert reservations(driver.book).available<0
    driver.trade('SELL','1',reduce=True)
    assert driver.book.cash==D('24.8') and not driver.book.positions


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_adverse_voluntary_fill_cannot_manufacture_insurance_cover(mode):
    driver=Driver(market(mode),capital='100')
    driver.trade('BUY','2')
    driver.mark(**{BTC:'85'})
    order=driver.submit('SELL','2','1',reduce=True)
    before=driver.book
    driver.fill(order,'2','1')
    assert driver.book.order(order).status=='CANCELLED'
    assert driver.book.cash==before.cash and driver.book.positions==before.positions
    assert driver.book.insurance_covered==0 and driver.book.fees_paid==D('0.2')


def test_extreme_isolated_partial_close_cannot_export_loss_to_cross_cash():
    driver=Driver(market('ISOLATED'))
    driver.trade('BUY','2')
    driver.mark(**{BTC:'85'})
    order=driver.submit('SELL','0.5','30',reduce=True)
    driver.fill(order,'0.5','30')
    assert driver.book.order(order).reason=='PRE_TRADE_NEGATIVE_ISOLATED_EQUITY'
    assert driver.book.cash==D('959.8') and driver.book.position(BTC).collateral==D('40')
    assert driver.book.insurance_covered==0


def test_size_limits_use_both_pending_extremes_and_cannot_be_netted_away():
    driver=Driver(market(max_size='2'))
    driver.trade('BUY','1')
    driver.submit('SELL','1')
    oversized=driver.submit('BUY','1.001')
    assert driver.book.order(oversized).reason=='MAX_POSITION_SIZE'
    assert driver.book.position(BTC).size==D('1')


def test_execution_loss_buffer_for_isolated_open_is_paid_from_cash():
    driver=Driver(market('ISOLATED'))
    driver.trade('BUY','1','110')
    # MARK=100; entry at110 loses10 immediately. 20 initial +10 loss buffer.
    assert driver.book.cash==D('969.89')
    assert driver.book.position(BTC).collateral==D('30')
    assert equity(driver.book)==D('989.89')


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_journal_invariants_across_seeded_mixed_transitions(mode):
    import random
    rng=random.Random(724103)
    driver=Driver(market(mode),capital='100000')
    for index in range(200):
        driver.mark(**{BTC:str(rng.randint(60,140))})
        if index%7==0:
            driver.funding(str(rng.choice(['-0.1','0.1'])),price=str(driver.book.mark(BTC)))
        if index%5==0 and driver.book.position(BTC) is not None:
            position=driver.book.position(BTC)
            side='SELL' if position.size>0 else 'BUY'
            quantity=min(abs(position.size),D('1'))
            driver.trade(side,str(quantity),str(driver.book.mark(BTC)),reduce=True)
        else:
            driver.trade(rng.choice(['BUY','SELL']),str(rng.choice([1,2,3])),str(driver.book.mark(BTC)))
    position=driver.book.position(BTC)
    if position is not None:
        driver.trade('SELL' if position.size>0 else 'BUY',str(abs(position.size)),
            str(driver.book.mark(BTC)),reduce=True)
    assert not driver.book.positions
    assert driver.book.cash==(driver.book.initial_cash+driver.book.realized_pnl-driver.book.fees_paid
        -driver.book.funding_paid-driver.book.insurance_transferred+driver.book.insurance_covered)


def test_available_margin_is_independent_of_readers_decimal_context():
    driver=Driver()
    driver.trade('BUY','1')
    driver.submit('BUY','1.5')
    driver.submit('SELL','2.6')
    reserved=reservations(driver.book)
    with localcontext() as context:
        context.prec=3;context.traps[Inexact]=True
        assert reserved.available==D('947.49')


def test_partial_close_fee_can_trigger_full_isolated_liquidation():
    driver=Driver(market('ISOLATED',fee='0.1'))
    driver.trade('BUY','2')
    driver.mark(**{BTC:'85'})
    result=driver.trade('SELL','1','85',reduce=True)
    assert driver.book.cash==D('940') and not driver.book.positions
    assert driver.book.realized_pnl==D('-30') and driver.book.fees_paid==D('28.5')
    assert driver.book.insurance_transferred==D('1.5')
    assert [e.kind for e in result.entries]==['FILL','ISOLATED_LIQUIDATION']
    assert result.entries[-1].reason=='FILL'
