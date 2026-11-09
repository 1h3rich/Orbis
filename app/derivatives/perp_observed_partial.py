"""PAPER reductions on observed executable bid/ask, without path interpolation."""
from dataclasses import replace
import json
from typing import TypedDict

from app.derivatives.perp_observed import _Observed
from app.derivatives.perp_observed_limit import LimitObserved
from app.derivatives.perp_observed_types import PartialOpenPosition, ObservedTransition
from app.lab.engine import number
from app.lab.perp_book_types import D, Transition
from app.lab.perp_execution_math import partial_reduction
from app.lab.perp_profile import PerpPartialProfile
from app.lab.store import encoded


class PartialIntent(TypedDict):
    target_index: int
    trigger_price: str
    fraction: str
    quantity: str
    remaining_before: str
    received_at_ns: str
    quote_time_ns: str


class PartialObserved(_Observed):
    position_type = PartialOpenPosition
    _partial_intent: PartialIntent | None = None

    def run(self) -> ObservedTransition:
        # Terminal orders remain in the original journal. Only resting orders
        # can affect the next observation and its risk or reservations.
        account = self.state.account
        self.state = replace(self.state, account=replace(account,
            orders=tuple(order for order in account.orders if order.active)))
        return super().run()

    def _remember(self, event: dict) -> None:
        opened = self.state.opened
        assert isinstance(opened, PartialOpenPosition)
        evidence = encoded(event)
        self.state = replace(self.state, opened=replace(opened,
            partial_exits_json=(*opened.partial_exits_json, evidence)))
        self.events.append(evidence)

    def record(self, transition: Transition) -> None:
        if self._partial_intent is not None:
            for entry in transition.entries:
                if entry.kind == 'FILL':
                    intent = self._partial_intent
                    remaining = D(intent['remaining_before']) - abs(entry.size_delta)
                    self._remember({**intent, 'kind': 'PARTIAL_TARGET_FILLED', 'order_id': entry.order_id,
                        'remaining_after': format(remaining, 'f'), 'price': format(entry.price, 'f'),
                        'fee': format(entry.fee, 'f'), 'realized_pnl': format(entry.realized_pnl, 'f'),
                        'collateral_delta': format(entry.collateral_delta, 'f')})
        # If the same fill liquidates the residue, finish sees its partial.
        super().record(transition)

    def finish(self, reason: str, price: D | None) -> None:
        opened = self.state.opened
        assert isinstance(opened, PartialOpenPosition)
        super().finish(reason, price)
        self.trades[-1] = encoded({**json.loads(self.trades[-1]),
            'partial_exits': [json.loads(row) for row in opened.partial_exits_json]})

    def _reduce(self, opened: PartialOpenPosition, index: int, trigger: D, fraction: D) -> None:
        position = self.state.account.position(self.instrument)
        assert position is not None
        remaining = abs(position.size)
        price = self.execution_price(buying=not self.long)
        size, eligible = partial_reduction(opened.quantity, fraction, remaining, price,
            self.state.account.market(self.instrument).specification)
        intent: PartialIntent = {'target_index': index + 1, 'trigger_price': format(trigger, 'f'),
            'fraction': format(fraction, 'f'), 'quantity': format(size, 'f'),
            'remaining_before': format(remaining, 'f'), 'received_at_ns': str(self.batch.received_at_ns),
            'quote_time_ns': str(self.batch.quote.event_time_ns)}
        if not eligible:
            self._remember({**intent, 'kind': 'PARTIAL_TARGET_SKIPPED_MINIMUM',
                'remaining_after': intent['remaining_before']})
            return
        self._partial_intent = intent
        try:
            filled = self.execute(size, opening=False, reason='PARTIAL_TARGET')
        finally:
            self._partial_intent = None
        if filled is None:
            self._remember({**intent, 'kind': 'PARTIAL_TARGET_REJECTED',
                'remaining_after': intent['remaining_before'], 'reason': self.state.reason})

    def trigger(self) -> None:
        opened, profile = self.state.opened, self.state.profile
        if opened is None or self.state.status not in ('RUNNING', 'CLOSE_ONLY'):
            return
        assert isinstance(opened, PartialOpenPosition) and isinstance(profile, PerpPartialProfile)
        if self.state.close_requested:
            self.exit('MANUAL_CLOSE_OBSERVED')
            return
        stop, target = self.protection_levels(opened)
        price = self.batch.quote.bid if self.long else self.batch.quote.ask
        if (price <= stop if self.long else price >= stop):
            self.exit('STOP_OR_TRAILING')
            return
        sign = 1 if self.long else -1
        for index in range(len(opened.partial_exits_json), len(profile.partial_targets)):
            rule = profile.partial_targets[index]
            trigger = opened.entry_price * (1 + sign * number(rule.target))
            if (price < trigger if self.long else price > trigger):
                break
            self._reduce(opened, index, trigger, number(rule.fraction))
            if self.state.opened is None or self.state.status not in ('RUNNING', 'CLOSE_ONLY'):
                return
        if (price >= target if self.long else price <= target):
            self.exit('TAKE_PROFIT')


class PartialLimitObserved(LimitObserved, PartialObserved):
    """LIMIT entry with the same PAPER partial exit rules."""
