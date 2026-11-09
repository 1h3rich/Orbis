"""Sizing and event times for the explicit single-market OHLC model."""
from dataclasses import replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.derivatives.paper_math import InstrumentSpec
from app.lab.engine import LabError
from app.lab.perp_account import risk_pockets
from app.lab.perp_book_types import Account, D, Market, QUANTUM, ZERO, exact, money
from app.lab.perp_path import PathPoint, interpolate


@exact
def adverse_price(raw: Decimal, buying: bool, slippage: Decimal, tick: Decimal) -> Decimal:
    slipped = raw*(D(1)+slippage if buying else D(1)-slippage)
    units = (slipped/tick).to_integral_value(rounding=ROUND_CEILING if buying else ROUND_FLOOR)
    return units*tick


@exact
def limit_bound(close: Decimal, offset: Decimal, tick: Decimal, *, buying: bool) -> Decimal:
    raw = close * (1 - offset if buying else 1 + offset)
    rounding = ROUND_FLOOR if buying else ROUND_CEILING
    return (raw / tick).to_integral_value(rounding=rounding) * tick


@exact
def limit_fill_price(quote: Decimal, bound: Decimal, slippage: Decimal,
                     tick: Decimal, *, buying: bool) -> Decimal | None:
    if (buying and quote > bound) or (not buying and quote < bound):
        return None
    slipped = adverse_price(quote, buying, slippage, tick)
    price = min(slipped, bound) if buying else max(slipped, bound)
    return price if price > 0 else None


@exact
def partial_reduction(initial: Decimal, fraction: Decimal, remaining: Decimal,
                      price: Decimal, spec: InstrumentSpec) -> tuple[Decimal, bool]:
    size = (initial * fraction / spec.size_step).to_integral_value(rounding=ROUND_FLOOR) * spec.size_step
    return size, spec.min_size <= size < remaining and size * price >= spec.min_notional


@exact
def opening_size(cash: Decimal, allocation: Decimal, market: Market,
                 price: Decimal, mark: Decimal, *, long: bool, fee_price: Decimal | None = None) -> Decimal:
    """Budget includes initial margin, adverse MARK loss buffer and fees.

    Try each tier independently; non-monotone/discontinuous tier tables don't
    justify a single global binary search. Never borrow immediate MARK profit.
    """
    budget = money(cash*allocation)
    if budget <= 0:
        return ZERO
    spec, best = market.specification, ZERO
    fee_basis = price if fee_price is None else fee_price
    limits = [market.rules.max_size]
    if spec.max_size is not None:
        limits.append(spec.max_size)
    for tier in market.rules.tiers:
        if market.leverage > tier.max_leverage:
            continue
        rate = max(tier.initial_rate,D(1)/market.leverage)
        buffer = max(ZERO,(price-mark) if long else (mark-price))
        per_unit = mark*rate+buffer+fee_basis*market.fee_rate
        cap = min(*limits,budget/per_unit)
        if tier.cap is not None:
            cap = min(cap,tier.cap/mark)
        quantity = (cap/spec.size_step).to_integral_value(rounding=ROUND_FLOOR)*spec.size_step
        if tier.cap is not None and quantity*mark >= tier.cap:
            quantity -= spec.size_step
        # Ceil fees/margin can use at most two settlement quanta more than the
        # continuous estimate. One lot usually suffices; bound the correction.
        for _ in range(3):
            if (quantity < spec.min_size or quantity*price < spec.min_notional
                    or quantity*mark < tier.floor):
                break
            needed = (money(quantity*mark*rate,up=True)+money(quantity*buffer,up=True)
                      +money(quantity*fee_basis*market.fee_rate,up=True)+2*QUANTUM)
            if needed <= budget:
                best = max(best,quantity)
                break
            correction = ((needed-budget)/(per_unit*spec.size_step)).to_integral_value(rounding=ROUND_CEILING)
            quantity -= max(D(1),correction)*spec.size_step
    return best


@exact
def first_trade_trigger(start: PathPoint, end: PathPoint, level: Decimal,
                        *, below: bool) -> int | None:
    def hit(t: int) -> bool:
        price = interpolate(start,end,t).trade
        return price <= level if below else price >= level
    if hit(start.time_ns):
        return start.time_ns
    if not hit(end.time_ns):
        return None
    low, high = start.time_ns,end.time_ns
    while high-low > 1:
        middle = (low+high)//2
        if hit(middle):
            high = middle
        else:
            low = middle
    return high


@exact
def first_margin_breach(account: Account, start: PathPoint, end: PathPoint) -> int | None:
    if not account.positions:
        return None
    market = account.markets[0]
    if len(account.markets)!=1:
        raise ValueError('La cronología actual requiere un solo instrumento')
    def hit(t: int) -> bool:
        point = interpolate(start,end,t)
        snapshot = replace(account,marks=((market.instrument,point.mark),))
        return any(p.breached for p in risk_pockets(snapshot))
    def crossing(first: int, last: int, level: Decimal, *, strict: bool = False) -> int | None:
        upward = end.mark>=start.mark
        def passed(t: int) -> bool:
            price = interpolate(start,end,t).mark
            if upward:
                return price>level if strict else price>=level
            return price<level if strict else price<=level
        if passed(first):return first
        if not passed(last):return None
        while last-first>1:
            middle=(first+last)//2
            if passed(middle):last=middle
            else:first=middle
        return last
    cuts = {start.time_ns,end.time_ns}
    delta = end.mark-start.mark
    size = abs(account.positions[0].size)
    if delta:
        for tier in market.rules.tiers[1:]:
            boundary = crossing(start.time_ns,end.time_ns,tier.floor/size,strict=delta<0)
            if boundary is not None:
                cuts.update(t for t in (boundary-1,boundary,boundary+1) if start.time_ns<=t<=end.time_ns)
    ordered = sorted(cuts)
    for low,high in zip(ordered,ordered[1:]):
        if hit(low):
            return low
        if high-low<=1:
            if hit(high):return high
            continue
        left,right = interpolate(start,end,low).mark,interpolate(start,end,high).mark
        tier = market.rules.tier_for(size*interpolate(start,end,(low+high)//2).mark)
        position = account.positions[0]
        sign = D(1) if position.size>0 else D(-1)
        balance = position.collateral if market.mode=='ISOLATED' else account.cash
        constant = balance-sign*position.basis+tier.cumulative_maintenance_amount
        slope = position.size-size*tier.maintenance_rate
        # The true quantized health is within[-1.5Q,+0.5Q] of the affine
        # expression. It can dip to zero and recover by1Q even when the
        # endpoints are healthy. Enumerate its rounding discontinuities only
        # in this narrow band, rather than assuming monotone rounded health.
        first,last = sorted(((-QUANTUM-constant)/slope,(2*QUANTUM-constant)/slope))
        first,last = max(first,min(left,right)),min(last,max(left,right))
        if first>last:
            if hit(high):return high
            continue
        levels = {first,last}
        for scale,offset,half in ((size,ZERO,True),
                (size*tier.maintenance_rate,tier.cumulative_maintenance_amount,False)):
            begin = int(((scale*first-offset)/QUANTUM).to_integral_value(rounding=ROUND_FLOOR))-1
            finish = int(((scale*last-offset)/QUANTUM).to_integral_value(rounding=ROUND_CEILING))+1
            if finish-begin>4096:
                raise LabError('No se puede resolver el margen a esta precisión monetaria sin ambigüedad')
            for units in range(begin,finish+1):
                threshold = ((D(units)+(D('0.5') if half else ZERO))*QUANTUM+offset)/scale
                if first<=threshold<=last:levels.add(threshold)
        candidates = {low,high}
        for level in levels:
            for strict in (False,True):
                stamp = crossing(low,high,level,strict=strict)
                if stamp is not None:
                    candidates.update(t for t in (stamp-1,stamp,stamp+1) if low<=t<=high)
        for stamp in sorted(candidates):
            if hit(stamp):return stamp
    return end.time_ns if hit(end.time_ns) else None
