"""Rounding can make maintenance health nonmonotone inside one risk tier."""
from dataclasses import replace
from decimal import Decimal as D
import random

from app.lab.perp_account import new_account,risk_pockets,mark_all
from app.lab.perp_book_types import Cursor,Position,QUANTUM,money,exact
from app.lab.perp_execution_math import first_margin_breach
from app.lab.perp_path import PathPoint,interpolate
from tests.test_perp_account import market,BTC,NOW


@exact
def case(offset,delta):
    start_price=D('100')+D(offset)*QUANTUM
    end_price=start_price+D(delta)*QUANTUM
    size,basis=D('0.001'),D('0.1')
    config=market('ISOLATED')
    pnl=money(size*start_price)-basis
    maintenance=money(config.rules.maintenance_requirement(size*start_price),up=True)
    collateral=maintenance-pnl+QUANTUM
    book=new_account(D('1000'),(config,))
    book=replace(book,cash=book.cash-collateral,
        positions=(Position(BTC,size,basis,collateral),),marks=((BTC,start_price),),
        mark_time_ns=NOW,cursor=Cursor(NOW,0))
    return book,PathPoint(NOW,D('100'),start_price),PathPoint(NOW+128,D('100'),end_price)


def hit(book,point):
    return risk_pockets(replace(book,marks=((BTC,point.mark),)))[0].breached


def test_first_breach_is_found_when_rounded_health_recovers_before_endpoint():
    book,start,end=case(600,-1000)
    assert not hit(book,start) and not hit(book,end)
    stamp=first_margin_breach(book,start,end)
    assert stamp==NOW+13
    assert hit(book,interpolate(start,end,stamp))
    assert not hit(book,interpolate(start,end,stamp-1))
    transition=mark_all(book,Cursor(stamp,0),{BTC:interpolate(start,end,stamp).mark})
    assert not transition.account.positions
    assert any(e.kind=='ISOLATED_LIQUIDATION' for e in transition.entries)


def test_bounded_quantum_paths_match_exhaustive_nanosecond_oracle():
    rng=random.Random(481927)
    for _ in range(80):
        book,start,end=case(rng.randint(-2500,2500),rng.randint(-2500,2500))
        expected=next((t for t in range(start.time_ns,end.time_ns+1)
                       if hit(book,interpolate(start,end,t))),None)
        assert first_margin_breach(book,start,end)==expected
