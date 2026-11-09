"""Pure experimental account transitions for historical perpetual research.

MARK is used for risk, never as an implicit fill. Funding really changes cash
or isolated collateral. Full liquidation transfers the remaining pocket and
records covered deficit; it is not a voluntary close at a guessed threshold.
No functions in this module talk to an exchange, wallet, database or clock.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import re
from typing import Literal, TypedDict, Unpack

from app.derivatives.paper_math import MarginRules, PaperPerpError
from app.lab.perp_book_types import (
    Account, Cursor, D, Entry, Market, Order, PerpAccountError, Position,
    Reservations, RiskPocket, Transition, ZERO, exact, money, value,
)


def _advance(account: Account, cursor: Cursor, *, fresh: bool = False) -> Account:
    if not isinstance(cursor, Cursor) or (account.cursor is not None and cursor <= account.cursor):
        raise PerpAccountError('El evento debe tener una secuencia posterior')
    if fresh and (account.mark_time_ns != cursor.time_ns or len(account.marks) != len(account.markets)):
        raise PerpAccountError('Se requiere un lote MARK del mismo instante')
    return replace(account, cursor=cursor)


def _positions(account: Account, position: Position | None, instrument: str) -> Account:
    rows = [p for p in account.positions if p.instrument != instrument]
    if position is not None:
        rows.append(position)
    return replace(account, positions=tuple(sorted(rows, key=lambda p: p.instrument)))


def _order(account: Account, order: Order) -> Account:
    existing = any(row.id == order.id for row in account.orders)
    return replace(account, orders=tuple(order if row.id == order.id else row for row in account.orders)
                   if existing else (*account.orders, order))


class _EntryFields(TypedDict, total=False):
    instrument: str | None
    order_id: str | None
    reason: str | None
    size_delta: Decimal
    price: Decimal | None
    cash_delta: Decimal
    collateral_delta: Decimal
    realized_pnl: Decimal
    fee: Decimal
    funding_payment: Decimal
    insurance_transfer: Decimal
    insurance_cover: Decimal


def _entry(account: Account, kind: str, **fields: Unpack[_EntryFields]) -> Entry:
    assert account.cursor is not None
    return Entry(account.cursor, kind, **fields)


def _pnl(position: Position, mark: Decimal) -> Decimal:
    return (money(abs(position.size) * mark) - position.basis) * (1 if position.size > 0 else -1)


def _initial(market: Market, size: Decimal, mark: Decimal) -> Decimal:
    if not size:
        return ZERO
    notional = abs(size) * mark
    tier = market.rules.tier_for(notional)
    return money(max(notional * tier.initial_rate, notional / market.leverage), up=True)


def _fee(market: Market, size: Decimal, price: Decimal) -> Decimal:
    return money(abs(size) * price * market.fee_rate, up=True)


def _maximum(market: Market, size: Decimal) -> None:
    limits = [market.rules.max_size]
    if market.specification.max_size is not None:
        limits.append(market.specification.max_size)
    if abs(size) > min(limits):
        raise PerpAccountError('MAX_POSITION_SIZE')


def _valid_order(account: Account, order: Order) -> None:
    market = account.market(order.instrument)
    spec = market.specification
    if order.remaining % spec.size_step or order.price_bound % spec.tick_size:
        raise PerpAccountError('INSTRUMENT_GRANULARITY')
    if order.reduce_only:
        position = account.position(order.instrument)
        if (position is None or (position.size > 0) == (order.side == 'BUY')
                or order.remaining > abs(position.size)):
            raise PerpAccountError('REDUCE_ONLY_NO_LONGER_REDUCING')
    elif order.quantity < spec.min_size or order.quantity * order.price_bound < spec.min_notional:
        raise PerpAccountError('MIN_ORDER_SIZE')


@exact
def new_account(initial_cash: Decimal, markets: tuple[Market, ...]) -> Account:
    value(initial_cash, positive=True)
    if money(initial_cash) != initial_cash:
        raise PerpAccountError('El capital excede la precisión de liquidación')
    if (type(markets) is not tuple or not 1 <= len(markets) <= 32
            or any(not isinstance(market, Market) for market in markets)
            or len({market.instrument for market in markets}) != len(markets)):
        raise PerpAccountError('Perfil de mercados no válido')
    return Account(initial_cash, initial_cash, tuple(sorted(markets, key=lambda m: m.instrument)))


@exact
def equity(account: Account) -> Decimal:
    return account.cash + sum((p.collateral + _pnl(p, account.mark(p.instrument))
                               for p in account.positions), ZERO)


@exact
def risk_pockets(account: Account) -> tuple[RiskPocket,...]:
    result = []
    cross_pnl = cross_maintenance = ZERO
    has_cross = False
    for position in account.positions:
        market = account.market(position.instrument)
        mark = account.mark(position.instrument)
        pnl = _pnl(position,mark)
        maintenance = money(market.rules.maintenance_requirement(abs(position.size)*mark),up=True)
        if market.mode == 'ISOLATED':
            result.append(RiskPocket(position.instrument,position.collateral+pnl,maintenance))
        else:
            has_cross = True
            cross_pnl += pnl
            cross_maintenance += maintenance
    if has_cross:
        result.append(RiskPocket(None,account.cash+cross_pnl,cross_maintenance))
    return tuple(result)


@exact
def reservations(account: Account, *, enforce_limits: bool = False) -> Reservations:
    cross_initial = isolated_pending = fees = cross_pnl = ZERO
    for market in account.markets:
        position = account.position(market.instrument)
        size = position.size if position is not None else ZERO
        mark = account.mark(market.instrument)
        active = [o for o in account.orders if o.instrument == market.instrument and o.active]
        opening = [o for o in active if not o.reduce_only]
        buy = sum((o.remaining for o in opening if o.side == 'BUY'), ZERO)
        sell = sum((o.remaining for o in opening if o.side == 'SELL'), ZERO)
        long = max(size, ZERO) + buy
        short = min(size, ZERO) - sell
        if enforce_limits and opening:
            _maximum(market, max(abs(long), abs(short)))
            tier = market.rules.tier_for(max(abs(long), abs(short)) * mark)
            if market.leverage > tier.max_leverage:
                raise PerpAccountError('LEVERAGE_TIER')
        # reduce-only cannot create either extreme. Fees for isolated reducing
        # orders are paid by their own pocket, not reserved from cross cash.
        fees += sum((_fee(market, o.remaining, o.price_bound) for o in active
                     if market.mode == 'CROSS' or not o.reduce_only), ZERO)
        if market.mode == 'CROSS':
            cross_initial += max(_initial(market, long, mark), _initial(market, short, mark))
            if position is not None:
                cross_pnl += _pnl(position, mark)
        elif opening:
            collateral = position.collateral if position is not None else ZERO
            required = max(_initial(market, long, mark), _initial(market, short, mark))
            isolated_pending += max(ZERO, required - collateral)
    return Reservations(cross_initial, isolated_pending, fees, account.cash + cross_pnl)


def _check_initial(account: Account) -> None:
    try:
        reserved = reservations(account, enforce_limits=True)
    except PaperPerpError as exc:
        raise PerpAccountError('RISK_TIER_COVERAGE') from exc
    if reserved.available < 0:
        raise PerpAccountError('INITIAL_MARGIN')


def _cancel(account: Account, order: Order, reason: str) -> tuple[Account, Entry]:
    return (_order(account, replace(order, status='CANCELLED', reason=reason)),
            _entry(account, 'ORDER_CANCELLED', instrument=order.instrument, order_id=order.id, reason=reason))


def _stale_reducing(account: Account) -> tuple[Account, list[Entry]]:
    entries = []
    for order in account.orders:
        if not order.active or not order.reduce_only:
            continue
        try:
            _valid_order(account, order)
        except PerpAccountError as exc:
            account, entry = _cancel(account, order, str(exc))
            entries.append(entry)
    return account, entries


def _liquidations(account: Account, cause: str) -> tuple[Account, list[Entry]]:
    entries: list[Entry] = []
    for position in account.positions:
        market = account.market(position.instrument)
        if market.mode != 'ISOLATED':
            continue
        mark = account.mark(position.instrument)
        pnl = _pnl(position, mark)
        pocket = position.collateral + pnl
        maintenance = money(market.rules.maintenance_requirement(abs(position.size) * mark), up=True)
        if pocket > maintenance:
            continue
        transfer, covered = max(pocket, ZERO), max(-pocket, ZERO)
        account = _positions(account, None, position.instrument)
        account = replace(account, realized_pnl=account.realized_pnl + pnl,
            insurance_transferred=account.insurance_transferred + transfer,
            insurance_covered=account.insurance_covered + covered)
        entries.append(_entry(account, 'ISOLATED_LIQUIDATION', instrument=position.instrument,
            reason=cause, price=mark, size_delta=-position.size, collateral_delta=-position.collateral,
            realized_pnl=pnl, insurance_transfer=transfer, insurance_cover=covered))
        for order in account.orders:
            if order.active and order.instrument == position.instrument:
                account, entry = _cancel(account, order, 'ISOLATED_LIQUIDATION')
                entries.append(entry)
    cross = [p for p in account.positions if account.market(p.instrument).mode == 'CROSS']
    if cross:
        pnl = sum((_pnl(p, account.mark(p.instrument)) for p in cross), ZERO)
        maintenance = sum((money(account.market(p.instrument).rules.maintenance_requirement(
            abs(p.size) * account.mark(p.instrument)), up=True) for p in cross), ZERO)
        pocket = account.cash + pnl
        if pocket <= maintenance:
            transfer, covered = max(pocket, ZERO), max(-pocket, ZERO)
            for position in cross:
                mark = account.mark(position.instrument)
                entries.append(_entry(account, 'CROSS_POSITION_LIQUIDATED', instrument=position.instrument,
                    reason=cause, price=mark, size_delta=-position.size, realized_pnl=_pnl(position, mark)))
                account = _positions(account, None, position.instrument)
            entries.append(_entry(account, 'CROSS_ACCOUNT_LIQUIDATION', reason=cause,
                cash_delta=-account.cash, insurance_transfer=transfer, insurance_cover=covered))
            account = replace(account, cash=ZERO, realized_pnl=account.realized_pnl + pnl,
                insurance_transferred=account.insurance_transferred + transfer,
                insurance_covered=account.insurance_covered + covered)
            # Isolated closing orders remain backed by their own pocket; new
            # allocations and all cross orders relied on the lost cash pool.
            for order in account.orders:
                if order.active and (account.market(order.instrument).mode == 'CROSS' or not order.reduce_only):
                    account, entry = _cancel(account, order, 'CROSS_ACCOUNT_LIQUIDATION')
                    entries.append(entry)
    account, stale = _stale_reducing(account)
    return account, [*entries, *stale]


@exact
def assert_accounting(account: Account) -> None:
    """Exact cash/pocket identity, independent of current mark valuation."""
    collateral = sum((p.collateral for p in account.positions), ZERO)
    expected = (account.initial_cash + account.realized_pnl - account.fees_paid
                - account.funding_paid - account.insurance_transferred + account.insurance_covered)
    if account.cash + collateral != expected:
        raise PerpAccountError('Identidad contable incumplida')
    for position in account.positions:
        if (not position.size or position.basis <= 0 or position.collateral < 0
                or (account.market(position.instrument).mode == 'CROSS' and position.collateral != 0)):
            raise PerpAccountError('Posición residual no válida')


def _result(account: Account, entries: list[Entry]) -> Transition:
    assert_accounting(account)
    return Transition(account, tuple(entries))


@exact
def mark_all(account: Account, cursor: Cursor, marks: dict[str, Decimal]) -> Transition:
    account = _advance(account, cursor)
    if type(marks) is not dict or set(marks) != {m.instrument for m in account.markets}:
        raise PerpAccountError('El lote MARK debe cubrir todos los mercados del perfil')
    for price in marks.values():
        value(price, positive=True)
    account = replace(account, marks=tuple(sorted(marks.items())), mark_time_ns=cursor.time_ns)
    entries = [_entry(account, 'MARK', instrument=name, price=price) for name, price in account.marks]
    account, risk = _liquidations(account, 'MARK')
    return _result(account, [*entries, *risk])


@exact
def mark_with_rules(account: Account, cursor: Cursor, marks: dict[str, Decimal],
                    rules: dict[str, MarginRules]) -> Transition:
    """One observed batch, one risk check; no invented intermediate old-rule mark.

    Existing mark_all/replace_rules keep their published sequential semantics.
    This additive operation is for data received together by a new consumer.
    """
    account = _advance(account, cursor)
    names = {market.instrument for market in account.markets}
    if type(marks) is not dict or type(rules) is not dict or set(marks)!=names or set(rules)!=names:
        raise PerpAccountError('MARK y reglas deben cubrir todos los mercados')
    for price in marks.values():
        value(price,positive=True)
    markets = tuple(replace(market,rules=rules[market.instrument]) for market in account.markets)
    changed = [market.instrument for market,new in zip(account.markets,markets) if market.rules!=new.rules]
    account = replace(account,markets=markets,marks=tuple(sorted(marks.items())),mark_time_ns=cursor.time_ns)
    entries = [_entry(account,'MARK',instrument=name,price=price) for name,price in account.marks]
    entries.extend(_entry(account,'RULES_REPLACED',instrument=name,
        reason='CURRENT_RULES_AND_MARK_OBSERVED_TOGETHER') for name in changed)
    account,risk = _liquidations(account,'OBSERVED_MARK_AND_RULES')
    return _result(account,[*entries,*risk])


@exact
def submit_order(account: Account, cursor: Cursor, *, identifier: str, instrument: str,
                 side: Literal['BUY', 'SELL'], quantity: Decimal, price_bound: Decimal,
                 kind: Literal['MARKET', 'LIMIT'] = 'MARKET',
                 reduce_only: bool = False) -> Transition:
    account = _advance(account, cursor, fresh=True)
    account.market(instrument)
    if (type(identifier) is not str or re.fullmatch(r'[a-zA-Z0-9_.:-]{1,64}', identifier) is None
            or any(order.id == identifier for order in account.orders)
            or len(account.orders) >= 20000):
        raise PerpAccountError('ID de orden repetido/no válido o libro lleno')
    if side not in ('BUY', 'SELL') or kind not in ('MARKET', 'LIMIT') or type(reduce_only) is not bool:
        raise PerpAccountError('Tipo/lado de orden no válido')
    value(quantity, positive=True)
    value(price_bound, positive=True)
    order = Order(identifier, instrument, side, quantity, quantity, price_bound, kind, reduce_only)
    candidate = _order(account, order)
    try:
        _valid_order(candidate, order)
        if reduce_only and candidate.market(instrument).mode == 'ISOLATED':
            position = candidate.position(instrument)
            assert position is not None
            fee_budget = sum((_fee(candidate.market(instrument), o.remaining, o.price_bound)
                for o in candidate.orders if o.active and o.instrument == instrument and o.reduce_only), ZERO)
            if position.collateral + _pnl(position, candidate.mark(instrument)) < fee_budget:
                raise PerpAccountError('ISOLATED_FEE_RESERVE')
        elif not reduce_only:
            _check_initial(candidate)
        else:
            cross_equity = candidate.cash + sum((_pnl(p, candidate.mark(p.instrument))
                for p in candidate.positions if candidate.market(p.instrument).mode == 'CROSS'), ZERO)
            if cross_equity < _fee(candidate.market(instrument), quantity, price_bound):
                raise PerpAccountError('FEE_RESERVE')
    except (PerpAccountError, PaperPerpError) as exc:
        rejected = replace(order, status='REJECTED', reason=str(exc))
        return _result(_order(account, rejected), [_entry(account, 'ORDER_REJECTED',
            instrument=instrument, order_id=identifier, reason=str(exc))])
    return _result(candidate, [_entry(candidate, 'ORDER_ACCEPTED', instrument=instrument, order_id=identifier)])


@exact
def cancel_order(account: Account, cursor: Cursor, identifier: str, *,
                 reason: Literal['USER_CANCELLED', 'TIME_IN_FORCE_EXPIRED', 'END_OF_WINDOW',
                                 'OBSERVATION_INTERRUPTED'] = 'USER_CANCELLED') -> Transition:
    if reason not in ('USER_CANCELLED', 'TIME_IN_FORCE_EXPIRED', 'END_OF_WINDOW', 'OBSERVATION_INTERRUPTED'):
        raise PerpAccountError('Motivo de cancelación no válido')
    account = _advance(account, cursor)
    order = account.order(identifier)
    if not order.active:
        raise PerpAccountError('La orden ya tiene resultado terminal')
    account, entry = _cancel(account, order, reason)
    return _result(account, [entry])


def _execute(account: Account, order: Order, quantity: Decimal,
             price: Decimal) -> tuple[Account, Entry]:
    market = account.market(order.instrument)
    previous = account.position(order.instrument)
    signed = quantity if order.side == 'BUY' else -quantity
    old_size = previous.size if previous is not None else ZERO
    closing = min(quantity, abs(old_size)) if old_size * signed < 0 else ZERO
    opening = quantity - closing
    fee = _fee(market, quantity, price)
    closing_fee = money(fee * closing / quantity)
    opening_fee = fee - closing_fee
    cash, collateral_delta, realized = account.cash, ZERO, ZERO
    remaining: Position | None = previous
    if closing:
        assert previous is not None
        basis_closed = previous.basis if closing == abs(old_size) else money(previous.basis * closing / abs(old_size))
        realized = (money(closing * price) - basis_closed) * (1 if old_size > 0 else -1)
        remaining_size = old_size + (closing if signed > 0 else -closing)
        remaining = (replace(previous, size=remaining_size, basis=previous.basis - basis_closed)
                     if remaining_size else None)
        if market.mode == 'CROSS':
            cash += realized - closing_fee
        else:
            released = previous.collateral if remaining is None else money(previous.collateral * closing / abs(old_size))
            pocket_return = released + realized - closing_fee
            remaining_collateral = previous.collateral - released
            if pocket_return < 0:
                # The closed slice may consume the rest of its own pocket,
                # never another position's collateral or unrelated free cash.
                if remaining is None or remaining_collateral + pocket_return < 0:
                    raise PerpAccountError('ISOLATED_EXECUTION_EXCEEDS_COLLATERAL')
                remaining_collateral += pocket_return
            else:
                cash += pocket_return
            if remaining is not None:
                remaining = replace(remaining, collateral=remaining_collateral)
            collateral_delta -= previous.collateral - remaining_collateral
    if opening:
        open_size = opening if signed > 0 else -opening
        new_size = (remaining.size if remaining is not None else ZERO) + open_size
        basis = (remaining.basis if remaining is not None else ZERO) + money(opening * price)
        collateral = remaining.collateral if remaining is not None else ZERO
        if basis <= 0:
            raise PerpAccountError('SETTLEMENT_DUST')
        remaining = Position(order.instrument, new_size, basis, collateral)
        if market.mode == 'ISOLATED':
            # Do not borrow unrealized isolated profit. Fund immediate adverse
            # execution loss as well as initial margin from available cash.
            target = _initial(market, new_size, account.mark(order.instrument))
            target += max(ZERO, -_pnl(remaining, account.mark(order.instrument)))
            allocated = max(ZERO, target - collateral)
            cash -= allocated
            collateral_delta += allocated
            remaining = replace(remaining, collateral=collateral + allocated)
            if cash - opening_fee < 0:
                raise PerpAccountError('ISOLATED_CASH')
        cash -= opening_fee
    if remaining is not None and remaining.basis <= 0:
        raise PerpAccountError('SETTLEMENT_DUST')
    candidate = _positions(account, remaining, order.instrument)
    pending = order.remaining - quantity
    candidate = _order(candidate, replace(order, remaining=pending, status='PARTIAL' if pending else 'FILLED'))
    candidate = replace(candidate, cash=cash, fees_paid=account.fees_paid + fee,
                        realized_pnl=account.realized_pnl + realized)
    entry = _entry(candidate, 'FILL', instrument=order.instrument, order_id=order.id,
        size_delta=signed, price=price, cash_delta=cash-account.cash,
        collateral_delta=collateral_delta, realized_pnl=realized, fee=fee)
    return candidate, entry


@exact
def fill_order(account: Account, cursor: Cursor, identifier: str, *,
               quantity: Decimal, price: Decimal) -> Transition:
    account = _advance(account, cursor, fresh=True)
    order = account.order(identifier)
    if not order.active:
        raise PerpAccountError('La orden ya tiene resultado terminal')
    value(quantity, positive=True)
    value(price, positive=True)
    spec = account.market(order.instrument).specification
    if quantity > order.remaining or quantity % spec.size_step or price % spec.tick_size:
        raise PerpAccountError('Fill fuera de cantidad/granularidad')
    if (order.side == 'BUY' and price > order.price_bound) or (order.side == 'SELL' and price < order.price_bound):
        raise PerpAccountError('Fill fuera de la protección de precio')
    try:
        _valid_order(account, order)
        if not order.reduce_only:
            _check_initial(account)  # Admission is not a permanent promise.
        candidate, fill = _execute(account, order, quantity, price)
        candidate, stale = _stale_reducing(candidate)
        current = candidate.position(order.instrument)
        if (current is not None and candidate.market(order.instrument).mode == 'ISOLATED'
                and current.collateral + _pnl(current, candidate.mark(order.instrument)) < 0):
            raise PerpAccountError('NEGATIVE_ISOLATED_EQUITY')
        if order.reduce_only and candidate.market(order.instrument).mode == 'CROSS':
            cross_equity = candidate.cash + sum((_pnl(p, candidate.mark(p.instrument))
                for p in candidate.positions if candidate.market(p.instrument).mode == 'CROSS'), ZERO)
            if cross_equity < 0:
                raise PerpAccountError('NEGATIVE_CROSS_EQUITY')
        if not order.reduce_only:
            _maximum(candidate.market(order.instrument), candidate.position(order.instrument).size
                     if candidate.position(order.instrument) is not None else ZERO)
            _check_initial(candidate)  # Recheck actual P/L, fees and remaining orders.
    except (PerpAccountError, PaperPerpError) as exc:
        account, cancelled = _cancel(account, order, 'PRE_TRADE_' + str(exc))
        return _result(account, [cancelled])
    candidate, risk = _liquidations(candidate, 'FILL')
    return _result(candidate, [fill, *stale, *risk])


@exact
def settle_funding(account: Account, cursor: Cursor, *, instrument: str,
                   rate_percent: Decimal, settlement_mark: Decimal) -> Transition:
    account = _advance(account, cursor, fresh=True)
    market = account.market(instrument)
    value(rate_percent)
    value(settlement_mark, positive=True)
    if abs(rate_percent) > 100:
        raise PerpAccountError('Financiación fuera de rango')
    if settlement_mark != account.mark(instrument):
        raise PerpAccountError('El MARK del lote no coincide con la liquidación de funding')
    previous_time = dict(account.funding_through).get(instrument)
    if previous_time is not None and cursor.time_ns <= previous_time:
        raise PerpAccountError('Liquidación de funding repetida o anterior')
    settled = dict(account.funding_through)
    settled[instrument] = cursor.time_ns
    account = replace(account, funding_through=tuple(sorted(settled.items())))
    position = account.position(instrument)
    if position is None:
        return _result(account, [_entry(account, 'FUNDING_NO_POSITION', instrument=instrument, price=settlement_mark)])
    payment = money(position.size * settlement_mark * rate_percent / D('100'))
    if market.mode == 'CROSS':
        account = replace(account, cash=account.cash - payment)
    else:
        account = _positions(account, replace(position, collateral=position.collateral-payment), instrument)
    account = replace(account, funding_paid=account.funding_paid+payment)
    entry = _entry(account, 'FUNDING', instrument=instrument, price=settlement_mark,
        funding_payment=payment, cash_delta=-payment if market.mode == 'CROSS' else ZERO,
        collateral_delta=-payment if market.mode == 'ISOLATED' else ZERO)
    account, risk = _liquidations(account, 'FUNDING')
    return _result(account, [entry, *risk])


@exact
def replace_rules(account: Account, cursor: Cursor, *, instrument: str,
                  rules: MarginRules) -> Transition:
    account = _advance(account, cursor, fresh=True)
    market = replace(account.market(instrument), rules=rules)
    account = replace(account, markets=tuple(market if m.instrument == instrument else m for m in account.markets))
    entry = _entry(account, 'RULES_REPLACED', instrument=instrument,
                   reason='CURRENT_RULES_APPLIED_EXPERIMENTALLY')
    account, risk = _liquidations(account, 'RULES')
    return _result(account, [entry, *risk])
