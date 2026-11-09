"""Conditional, separately versioned closure after missing PAPER coverage.

This never resumes or rewrites an observed session. Known funding samples and
the current quote define an estimated scenario, not evidence of path survival.
Only a state computed by replay may enter this function, never a loaded book.
"""
from __future__ import annotations

from dataclasses import replace
import json
from typing import Literal

from pydantic import Field

from app.derivatives.funding import FundingRecord
from app.derivatives.perp_observed import _funding, _metadata, _quote
from app.derivatives.perp_observed_types import ObservedError, ObservedState, PartialOpenPosition
from app.derivatives.perp_observed_wire import (
    ExactInput, Nanoseconds, ObservedRiskSnapshot, ObservedSpecification, QuoteInput,
    _exact_json, _rules, _specification,
)
from app.lab.engine import number
from app.lab.perp_account import (
    equity, fill_order, mark_all, mark_with_rules, settle_funding, submit_order,
)
from app.lab.perp_book_types import Cursor, Transition, exact
from app.lab.perp_dataset import FundingPoint


GAP_CLOSE_ENGINE = 'perp-gap-close-1.0'
ASSUMPTIONS = (
    'NO_ORDERS_OR_STOPS_RECONSTRUCTED',
    'ORIGINAL_RULES_AT_HISTORICAL_FUNDING_POINTS',
    'CURRENT_RULES_AND_SPECIFICATION_AT_EXIT',
    'LIQUIDATION_AT_SAMPLED_MARKS_ONLY',
    'NO_PATH_SURVIVAL_PROOF',
)


class GapCloseInput(ExactInput):
    schema_name: Literal['orbis.paper.perp.gap-close.input.v1'] = Field(alias='schema')
    engine: Literal['perp-gap-close-1.0']
    acknowledgement: Literal['UNKNOWN_INTERVAL_ACCEPTED']
    received_at_ns: Nanoseconds
    quote: QuoteInput
    rules: ObservedRiskSnapshot
    specification: ObservedSpecification
    funding: list[FundingPoint] = Field(min_length=1, max_length=10_000)


@exact
def estimate_gap_close(state: ObservedState, source: GapCloseInput) -> dict:
    source = GapCloseInput.model_validate(source.payload())
    opened = state.opened
    if (opened is None or state.status not in ('RUNNING', 'CLOSE_ONLY', 'GAP', 'HALTED')
            or len(state.account.positions) != 1 or len(state.account.markets) != 1
            or any(order.active for order in state.account.orders)):
        raise ObservedError('No hay una posición interrumpida elegible para estimar su cierre')
    now = int(source.received_at_ns)
    if now <= max(state.last_received_ns, state.last_control_ns):
        raise ObservedError('El cierre debe ser posterior al estado y al hueco conocidos')
    market = state.account.markets[0]
    instrument = market.instrument
    quote = source.quote.quote()
    _quote(quote, instrument, now)
    if quote.event_time_ns <= state.last_quote.event_time_ns:
        raise ObservedError('El cierre necesita una cotización nueva')
    rules = _rules(source.rules, now)
    spec = _specification(source.specification, now)
    _metadata(rules, spec, instrument, now)
    for previous, current in ((market.rules, rules), (market.specification, spec)):
        if (current.fetched_at_ns < previous.fetched_at_ns
                or current.fetched_at_ns == previous.fetched_at_ns and current != previous):
            raise ObservedError('Metadatos anteriores o contradictorios respecto al original')
    records = _funding(tuple(FundingRecord.from_payload(row.model_dump(), instrument)
                             for row in source.funding), instrument, now)
    if records[0] != state.funding_anchor:
        raise ObservedError('Falta o cambió el ancla de financiación consumida')
    if any(row.time_ns <= state.last_received_ns for row in records[1:]):
        raise ObservedError('Financiación retroactiva al periodo observado')
    if quote.event_time_ns < records[-1].time_ns:
        raise ObservedError('La cotización precede al último punto de financiación')
    definition = json.loads(state.definition_json)
    account = state.account
    journal = []
    terminal_ns = None
    terminal_reference = None
    liquidated = False

    def cursor(stamp: int) -> Cursor:
        old = account.cursor
        return Cursor(stamp, old.ordinal + 1 if old is not None and old.time_ns == stamp else 0)

    def record(transition: Transition) -> None:
        nonlocal account, terminal_ns, terminal_reference, liquidated
        account = transition.account
        journal.extend(transition.entries)
        for entry in transition.entries:
            if entry.kind in ('ISOLATED_LIQUIDATION', 'CROSS_ACCOUNT_LIQUIDATION'):
                liquidated = True
                if terminal_ns is None:
                    terminal_ns = entry.cursor.time_ns
                    terminal_reference = account.mark(instrument)

    for row in records[1:]:
        # These are reconstructed samples known *now*. No current rules are
        # moved back to an earlier time and no intragap stop is fabricated.
        record(mark_all(account, cursor(row.time_ns), {instrument: row.mark}))
        record(settle_funding(account, cursor(row.time_ns), instrument=instrument,
                              rate_percent=row.rate_percent, settlement_mark=row.mark))
    # Validate/apply both current metadata components together. An otherwise
    # compatible new lot must not be checked against obsolete risk bounds.
    account = replace(account, markets=(replace(market, specification=spec, rules=rules),))
    record(mark_with_rules(account, cursor(now), {instrument: quote.mark}, {instrument: rules}))
    price = None
    position = account.position(instrument)
    if position is not None:
        from app.lab.perp_execution_math import adverse_price
        buying = position.size < 0
        price = adverse_price(quote.ask if buying else quote.bid, buying,
                              number(definition['slippage_rate']), spec.tick_size)
        if price <= 0:
            raise ObservedError('El precio de cierre estimado queda bajo el tick')
        size = abs(position.size)
        order_id = 'gap-close-1'
        record(submit_order(account, cursor(now), identifier=order_id,
                            instrument=instrument, side='BUY' if buying else 'SELL',
                            quantity=size, price_bound=price, reduce_only=True))
        order = account.order(order_id)
        if order.active:
            record(fill_order(account, cursor(now), order_id, quantity=size, price=price))
        if account.position(instrument) is not None or account.order(order_id).status != 'FILLED':
            raise ObservedError('La posición completa no se puede cerrar con las reglas actuales')
        terminal_ns = now
        terminal_reference = price
    if account.positions or terminal_ns is None:
        raise ObservedError('El escenario no termina con la exposición cerrada')
    profit = account.cash - opened.cash_before
    return {
        'schema': 'orbis.paper.perp.gap-close.result.v1', 'engine': GAP_CLOSE_ENGINE,
        'environment': 'PAPER', 'quality': 'ESTIMATED_GAP_CLOSE', 'execution_authority': 'NONE',
        'status': 'ESTIMATED_LIQUIDATED' if liquidated else 'ESTIMATED_CLOSED',
        'assumptions': list(ASSUMPTIONS), 'reconstructed': True,
        'unknown_interval': {'from_ns': str(state.last_received_ns), 'through_ns': str(now)},
        'known_at_ns': str(now), 'exit_time_ns': str(terminal_ns),
        'exit_price': format(price, 'f') if price is not None else None,
        'exit_reference_price': format(terminal_reference, 'f') if terminal_reference is not None else None,
        'entry_price': format(opened.entry_price, 'f'), 'quantity': format(opened.quantity, 'f'),
        **({'remaining_quantity': format(abs(state.account.positions[0].size), 'f')}
           if isinstance(opened, PartialOpenPosition) else {}),
        'direction': definition['direction'],
        'position_net_profit': format(profit, 'f'),
        'position_fees': format(account.fees_paid - opened.fees_before, 'f'),
        'position_funding_paid': format(account.funding_paid - opened.funding_before, 'f'),
        'metrics': {'initial_capital': format(account.initial_cash, 'f'), 'cash': format(account.cash, 'f'),
                    'equity': format(equity(account), 'f'),
                    'net_profit': format(equity(account) - account.initial_cash, 'f'),
                    'realized_pnl': format(account.realized_pnl, 'f'),
                    'fees_paid': format(account.fees_paid, 'f'),
                    'funding_paid': format(account.funding_paid, 'f')},
        'events': [{'kind': 'CURRENT_METADATA_APPLIED_AT_ESTIMATED_CLOSE', 'known_at_ns': str(now),
                    'rules_changed': rules != market.rules,
                    'specification_changed': spec != market.specification}],
        'account': _exact_json(replace(account, orders=tuple(order for order in account.orders if order.active))),
        'journal': _exact_json(tuple(journal)),
    }
