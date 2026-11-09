"""Explicit hypothetical OHLC chronology; it is not observed tick history.

Each close precedes the next open at the same boundary timestamp. Interior
funding observations anchor MARK; an observation at the open is applied before
the modeled open MARK. Synthetic extreme times are shifted by 1 ns on collision.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.lab.engine import INTERVALS, LabError, number
from app.lab.perp_book_types import D, exact, money, value
from app.lab.perp_dataset import FundingPoint, PerpDataset
from app.lab.perp_profile import PathKind


@dataclass(frozen=True)
class PathPoint:
    time_ns: int
    trade: Decimal
    mark: Decimal
    funding: FundingPoint | None = None


@exact
def interpolate(start: PathPoint, end: PathPoint, time_ns: int) -> PathPoint:
    if not start.time_ns <= time_ns <= end.time_ns or start.time_ns >= end.time_ns:
        raise LabError('Tiempo fuera del segmento modelado')
    fraction = D(time_ns-start.time_ns) / D(end.time_ns-start.time_ns)
    return PathPoint(time_ns, money(start.trade+(end.trade-start.trade)*fraction),
                     money(start.mark+(end.mark-start.mark)*fraction))


def _at(knots: list[tuple[int,Decimal]], time_ns: int) -> Decimal:
    for (first, left), (last, right) in zip(knots, knots[1:]):
        if first <= time_ns <= last:
            return money(left+(right-left)*D(time_ns-first)/D(last-first))
    raise LabError('Segmento de precio incompleto')


def _price(raw: str) -> Decimal:
    parsed = value(number(raw,positive=True),positive=True)
    if money(parsed) != parsed:
        raise LabError('El modelo de trayectoria requiere precios de hasta 18 decimales')
    return parsed


@exact
def bar_path(dataset: PerpDataset, index: int, trade_path: PathKind,
             mark_path: PathKind) -> tuple[PathPoint,...]:
    if trade_path not in ('O_H_L_C','O_L_H_C') or mark_path not in ('O_H_L_C','O_L_H_C'):
        raise LabError('Trayectoria OHLC no disponible')
    if type(index) is not int or not 0 <= index < len(dataset.trade.bars):
        raise LabError('Vela fuera del dataset')
    trade, mark = dataset.trade.bars[index], dataset.mark.bars[index]
    start = trade.t*10**9
    end = start+INTERVALS[dataset.timeframe]*10**9
    settlements = {int(f.funding_time):f for f in dataset.funding if start <= int(f.funding_time) < end}
    low, high = _price(mark.l), _price(mark.h)
    if any(not low <= _price(f.mark_price) <= high for f in settlements.values()):
        raise LabError('Funding MARK fuera del rango OHLC: cronología inconsistente')
    times = [start,start+(end-start)//3,start+2*(end-start)//3,end]
    trade_keys = ('o','h','l','c') if trade_path=='O_H_L_C' else ('o','l','h','c')
    mark_keys = ('o','h','l','c') if mark_path=='O_H_L_C' else ('o','l','h','c')
    trades = [(t,_price(getattr(trade,key))) for t,key in zip(times,trade_keys)]
    marks = []
    for index,(t,key) in enumerate(zip(times,mark_keys)):
        if index in (1,2):
            while t in settlements:
                t += 1
        marks.append((t,_price(getattr(mark,key))))
    # Opening funding remains separate: actual settlement first, then modeled
    # open MARK. Interior observations become interpolation knots.
    marks.extend((t,_price(f.mark_price)) for t,f in settlements.items() if t!=start)
    marks.sort()
    joint = sorted({t for t,_ in trades}|{t for t,_ in marks})
    return tuple(PathPoint(t,_at(trades,t),_at(marks,t),settlements.get(t)) for t in joint)
