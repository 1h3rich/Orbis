"""Versioned full-fill LIMIT touch hypothesis on the shared perpetual book."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.lab.engine import number
from app.lab.perp_account import cancel_order, fill_order, submit_order
from app.lab.perp_book_types import Transition
from app.lab.perp_dataset import PerpDataset
from app.lab.perp_execution_math import first_trade_trigger, limit_bound, limit_fill_price, opening_size
from app.lab.perp_path import PathPoint, interpolate
from app.lab.perp_profile import PerpLimitProfile, PerpPartialProfile
from app.lab.perp_rules import RuleObservation
from app.lab.perp_simulation import _Simulation, simulation_engine


@dataclass(frozen=True)
class _Resting:
    identifier: str
    signal: dict
    quantity: Decimal
    price: Decimal
    expires_index: int


class LimitSimulation(_Simulation):
    profile: PerpLimitProfile | PerpPartialProfile

    def __init__(self, dataset: PerpDataset, profile: PerpLimitProfile | PerpPartialProfile, definition: dict,
                 start: int, end: int, *, rule_observation: RuleObservation | None = None):
        super().__init__(dataset, profile, definition, start, end, rule_observation=rule_observation)
        self.resting: _Resting | None = None
        self.current_index = self.start
        self.observed_extreme: Decimal | None = None

    def record(self, transition: Transition) -> None:
        super().record(transition)
        if self.resting is not None:
            order = self.account.order(self.resting.identifier)
            if order.status in ('CANCELLED', 'REJECTED'):
                self.resting.signal.update(action='ENTRY_PRE_TRADE_CANCELLED', reason=order.reason)
                self.resting = None

    def mark(self, point: PathPoint) -> None:
        super().mark(point)
        if self.opened is not None:
            if self.observed_extreme is None:
                self.observed_extreme = point.trade
            else:
                self.observed_extreme = (max(self.observed_extreme, point.trade) if self.long
                    else min(self.observed_extreme, point.trade))

    def update_trailing(self, index: int) -> None:
        # A high/low before this intrabar entry cannot tighten its trailing stop.
        if self.observed_extreme is not None:
            self.trailing = (max(self.trailing, self.observed_extreme) if self.long
                else min(self.trailing, self.observed_extreme))
        self.observed_extreme = None

    def cancel_limit(self, reason: Literal['TIME_IN_FORCE_EXPIRED', 'END_OF_WINDOW'], action: str) -> None:
        resting = self.resting
        if resting is None:
            return
        self.record(cancel_order(self.account, self.cursor(self.time_ns), resting.identifier, reason=reason))
        resting.signal.update(action=action, cancelled_at_ns=str(self.time_ns))
        self.resting = None

    def enter(self, point: PathPoint, index: int) -> None:
        self.current_index = index
        if self.resting is not None and index >= self.resting.expires_index:
            self.cancel_limit('TIME_IN_FORCE_EXPIRED', 'LIMIT_EXPIRED')
        if self.pending is None or self.liquidated or self.halted:
            self.fill_limit(point)
            return
        signal = self.pending
        self.pending = None
        entry = self.profile.entry_order
        assert entry is not None
        reference = number(signal['limit_reference'])
        offset = number(entry.offset)
        tick = self.market.specification.tick_size
        bound = limit_bound(reference, offset, tick, buying=self.long)
        if bound <= 0:
            signal['action'] = 'SKIPPED_PRICE_BELOW_TICK'
            return
        quantity = opening_size(self.account.cash, number(self.definition['allocation']), self.market,
            bound, point.mark, long=self.long)
        if not quantity:
            signal['action'] = 'SKIPPED_SIZE_OR_MARGIN'
            return
        self.order_count += 1
        identifier = 'perp-' + str(self.order_count)
        self.record(submit_order(self.account, self.cursor(point.time_ns), identifier=identifier,
            instrument=self.instrument, side='BUY' if self.long else 'SELL', quantity=quantity,
            price_bound=bound, kind='LIMIT'))
        order = self.account.order(identifier)
        if not order.active:
            signal.update(action='ORDER_REJECTED', reason=order.reason)
            return
        self.resting = _Resting(identifier, signal, quantity, bound,
            index + entry.valid_bars)
        signal.update(action='LIMIT_PENDING', order_id=identifier, limit_price=format(bound, 'f'),
            placed_at_ns=str(point.time_ns), expires_index=self.resting.expires_index)
        self.fill_limit(point)

    def fill_limit(self, point: PathPoint) -> None:
        resting = self.resting
        if resting is None:
            return
        price = limit_fill_price(point.trade, resting.price, self.slip,
                                 self.market.specification.tick_size, buying=self.long)
        if price is None:
            return
        before = self.account
        signal = resting.signal
        self.opened = {'source_candle_t':signal['source_candle_t'], 'signal_known_at_ns':signal['known_at_ns'],
            'entry_time_ns':str(point.time_ns), 'entry_price':format(price, 'f'),
            'quantity':format(resting.quantity, 'f'), 'direction':self.definition['direction'],
            '_index':self.current_index, '_cash_before':before.cash, '_realized_before':before.realized_pnl,
            '_fees_before':before.fees_paid, '_funding_before':before.funding_paid,
            '_insurance_before':before.insurance_transferred, '_covered_before':before.insurance_covered}
        self.record(fill_order(self.account, self.cursor(point.time_ns), resting.identifier,
            quantity=resting.quantity, price=price))
        self.resting = None
        order = self.account.order(resting.identifier)
        if order.status != 'FILLED':
            self.opened = None
            signal.update(action='ENTRY_PRE_TRADE_CANCELLED', reason=order.reason)
            return
        signal.update(action='ENTERED_LIMIT', entry_time_ns=str(point.time_ns), entry_price=format(price, 'f'))
        if self.opened is not None:
            self.trailing = price
            self.observed_extreme = max(price, point.trade) if self.long else min(price, point.trade)

    def segment(self, start: PathPoint, end: PathPoint) -> None:
        if self.resting is not None:
            touch = first_trade_trigger(start, end, self.resting.price, below=self.long)
            if touch is not None and touch < end.time_ns:
                point = interpolate(start, end, touch)
                self.mark(point)
                self.fill_limit(point)
                if self.opened is not None:
                    self.trigger_now(point)
                if not self.halted:
                    super().segment(point, end)
                return
        super().segment(start, end)
        if self.resting is not None and not self.halted:
            self.fill_limit(end)
            if self.opened is not None:
                self.trigger_now(end)

    def signal(self, index: int, *, warmup: bool = False) -> None:
        super().signal(index, warmup=warmup)
        if self.pending is not None:
            if self.resting is not None:
                self.pending['action'] = 'SKIPPED_LIMIT_OPEN'
                self.pending = None
            else:
                self.pending.update(action='PLANNED_LIMIT_NEXT_OPEN',
                    limit_reference=format(self.candles[index].c, 'f'))
        if index == self.end - 1:
            self.cancel_limit('END_OF_WINDOW', 'LIMIT_CANCELLED_AT_WINDOW_END')

    def run(self) -> dict:
        result = super().run()
        result['engine'] = simulation_engine(self.profile)
        result['assumptions'] = [
            'Closed-candle signals place a LIMIT at the next open; price references the signal close'
            if 'next-open market fills' in item else item
            for item in result['assumptions']
        ]
        result['assumptions'].extend([
            'A TRADE touch models a full LIMIT fill; adverse slippage is capped at the order bound',
            'LIMIT quantity and margin are admitted before touch and checked again at the modeled fill',
            'Expiry precedes the open after valid_bars; outstanding LIMIT orders cancel at window end',
            'Trailing incorporates only TRADE points at or after entry, and updates after candle close',
        ])
        return result
