"""PAPER LIMIT entries from closed signals and eligible public GRVT quotes."""
from dataclasses import replace

from app.derivatives.perp_observed import _Observed, cancel_resting_limit
from app.derivatives.perp_observed_types import RestingLimit
from app.lab.engine import INTERVALS, number
from app.lab.perp_account import fill_order, submit_order
from app.lab.perp_book_types import Transition
from app.lab.perp_execution_math import limit_bound, limit_fill_price, opening_size
from app.lab.perp_profile import PerpLimitProfile, PerpPartialProfile
from app.lab.store import encoded


class LimitObserved(_Observed):
    def record(self, transition: Transition) -> None:
        super().record(transition)
        resting = self.state.resting
        if resting is None:
            return
        order = self.state.account.order(resting.order_id)
        if order.active:
            return
        self.state = replace(self.state, resting=None)
        assert self.state.account.cursor is not None
        self.events.append(encoded({'kind': 'LIMIT_CANCELLED', 'order_id': order.id,
            'book_time_ns': str(self.state.account.cursor.time_ns),
            'received_at_ns': str(self.batch.received_at_ns), 'reason': order.reason}))

    def before_mark(self, stamp: int) -> None:
        if self.state.resting is not None and stamp >= self.state.resting.expires_at_ns:
            cancelled = cancel_resting_limit(self.state, stamp, 'TIME_IN_FORCE_EXPIRED',
                                             received_at_ns=self.batch.received_at_ns)
            self.state = cancelled.state
            self.journal.extend(cancelled.journal)
            self.events.extend(cancelled.events_json)

    def enter(self, signal: dict) -> None:
        if self.state.resting is not None:
            signal['action'] = 'SKIPPED_LIMIT_OPEN'
            return
        profile = self.state.profile
        assert isinstance(profile, (PerpLimitProfile, PerpPartialProfile)) and profile.entry_order is not None
        candle = self.state.history[-1]
        assert candle.t == signal['source_candle_t']
        received = self.batch.received_at_ns
        expires = (candle.t + (1 + profile.entry_order.valid_bars) * INTERVALS[self.state.timeframe]) * 10**9
        if received >= expires:
            signal['action'] = 'SKIPPED_EXPIRED_LIMIT'
            return
        account = self.state.account
        market = account.market(self.instrument)
        bound = limit_bound(candle.c, number(profile.entry_order.offset), market.specification.tick_size,
                            buying=self.long)
        if bound <= 0:
            signal['action'] = 'SKIPPED_PRICE_BELOW_TICK'
            return
        quantity = opening_size(account.cash, number(self.definition['allocation']), market,
                                bound, self.batch.quote.mark, long=self.long)
        if not quantity:
            signal['action'] = 'SKIPPED_SIZE_OR_MARGIN'
            return
        count = self.state.order_count + 1
        order_id = f'observed-{count}'
        self.state = replace(self.state, order_count=count)
        self.record(submit_order(account, self.cursor(received), identifier=order_id,
            instrument=self.instrument, side='BUY' if self.long else 'SELL', quantity=quantity,
            price_bound=bound, kind='LIMIT'))
        order = self.state.account.order(order_id)
        signal.update(order_id=order_id, limit_price=format(bound, 'f'),
                      limit_reference=format(candle.c, 'f'))
        if not order.active:
            signal.update(action='ORDER_REJECTED', reason=order.reason)
            return
        self.state = replace(self.state, resting=RestingLimit(order_id, candle.t, received, expires))
        signal.update(action='LIMIT_PENDING', placed_at_ns=str(received), expires_at_ns=str(expires))

    def match_resting(self) -> None:
        resting = self.state.resting
        quote = self.batch.quote
        received = self.batch.received_at_ns
        if (resting is None or self.state.status != 'RUNNING' or received <= resting.placed_at_ns
                or quote.event_time_ns <= resting.placed_at_ns):
            return
        account = self.state.account
        order = account.order(resting.order_id)
        price = limit_fill_price(quote.ask if self.long else quote.bid, order.price_bound,
                                 number(self.definition['slippage_rate']),
                                 account.market(self.instrument).specification.tick_size, buying=self.long)
        if price is None:
            return
        fill = fill_order(account, self.cursor(received), order.id, quantity=order.remaining, price=price)
        if fill.account.order(order.id).status != 'FILLED':
            self.record(fill)
            return
        opened = self.opening_position(resting.source_candle_t, resting.placed_at_ns, price, order.remaining)
        self.state = replace(self.state, opened=opened, resting=None)
        self.record(fill)
        self.events.append(encoded({'kind': 'LIMIT_FILLED_OBSERVED_QUOTE', 'order_id': order.id,
            'received_at_ns': str(received), 'quote_time_ns': str(quote.event_time_ns),
            'price': format(price, 'f')}))
