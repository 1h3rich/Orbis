"""Independent financial oracles for v3 reductions and residual exposure."""
from decimal import Decimal as D, ROUND_FLOOR, localcontext

import pytest

from app.derivatives.perp_observed_types import observed_version
from app.lab.engine import LabError
from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_profile import read_profile
from app.lab.perp_simulation import PARTIAL_ENGINE,simulate_perpetual
from tests.perp_simulation_fixture import dataset,definition,profile
from tests.test_perp_limit_simulation import limit_profile


TARGETS=[{'target':'0.02','fraction':'0.25'},{'target':'0.05','fraction':'0.35'}]


def partial_profile(strategy, *, targets=TARGETS, limited=False, **changes):
    original=(limit_profile(strategy,offset='0',**changes) if limited else profile(strategy,**changes)).payload()
    return read_profile({**original,'schema':'orbis.lab.perp.profile.v3','partial_targets':targets})


@pytest.mark.parametrize('limited',[False,True])
@pytest.mark.parametrize('mode',['ISOLATED','CROSS'])
@pytest.mark.parametrize('direction,sign,path',[('LONG',1,'O_H_L_C'),('SHORT',-1,'O_L_H_C')])
def test_multiple_targets_and_final_exit_have_exact_quantity_fees_and_pnl(monkeypatch,limited,mode,direction,sign,path):
    data=dataset(monkeypatch,direction);strategy=definition(direction)
    configured=partial_profile(strategy,limited=limited,margin_mode=mode,trade_path=path)
    run=simulate_perpetual(data,configured,strategy)
    assert run['engine']==PARTIAL_ENGINE and run['metrics']['trades']==1
    assert run['metrics']['partial_exits']==2 and run['metrics']['open_position'] is False
    trade=run['trades'][0];first,second=run['partial_events']
    assert trade['partial_exits']==[first,second] and first['time_ns']<second['time_ns']<trade['exit_time_ns']
    assert all(event['status']=='FILLED' for event in (first,second))
    fills=[row for row in run['journal'] if row['kind']=='FILL']
    assert len(fills)==4 and trade['exit_reason']=='TAKE_PROFIT'
    assert run['account']['orders'][0]['kind']==('LIMIT' if limited else 'MARKET')
    assert all(row['reduce_only'] for row in run['account']['orders'][1:])
    with localcontext() as ctx:
        ctx.prec=100
        size=D(trade['quantity']);lot=D('.001')
        p1,p2=( (size*D(row['fraction'])/lot).to_integral_value(rounding=ROUND_FLOOR)*lot for row in TARGETS )
        targets=[D(100)+D(sign)*step for step in (2,5,8)]
        closes=[p1,p2,size-p1-p2]
        assert [D(row['price']) for row in fills]==[D(100),*targets]
        assert [abs(D(row['size_delta'])) for row in fills]==[size,*closes]
        assert [D(first['remaining_after']),D(second['remaining_after'])]==[size-p1,size-p1-p2]
        assert [first['journal_index'],second['journal_index']]==[fills[1]['index'],fills[2]['index']]
        realized=sum(qty*(price-100)*sign for qty,price in zip(closes,targets))
        fees=(size*100+sum(qty*price for qty,price in zip(closes,targets)))*D('.001')
        assert D(trade['realized_pnl'])==realized and D(trade['fees'])==fees
        assert D(trade['net_profit'])==realized-fees
        assert D(run['metrics']['cash'])==1000+realized-fees
    assert run['paper_handoff_available'] is run['real_authority'] is False
    assert observed_version(configured)==(3,'perp-observed-3.0')


def test_stop_and_funding_use_only_the_open_remainder(monkeypatch):
    raw=dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(l='98',c='100')
    raw['trade']['bars'][3].update(l='90',c='95')
    raw['funding'][-1]['funding_rate']='0.01'
    strategy=definition(take_profit='0.4')
    result=simulate_perpetual(read_perp_dataset(raw),partial_profile(strategy),strategy)
    trade,=result['trades'];assert trade['exit_reason']=='STOP_OR_TRAILING'
    residual=D(trade['partial_exits'][-1]['remaining_after'])
    funding=[row for row in result['journal'] if row['kind']=='FUNDING']
    assert len(funding)==1 and D(funding[0]['funding_payment'])==residual*D('100')*D('.01')/100
    assert D(trade['funding_paid'])==D(funding[0]['funding_payment'])
    assert D(result['account']['orders'][-1]['quantity'])==residual


def test_dust_target_is_skipped_and_final_stop_still_works(monkeypatch):
    strategy=definition(take_profit='0.4')
    run=simulate_perpetual(dataset(monkeypatch),partial_profile(strategy,targets=[
        {'target':'0.02','fraction':'0.000001'}]),strategy)
    event,=run['partial_events'];assert event['status']=='SKIPPED_MINIMUM' and D(event['quantity'])==0
    assert event['remaining_before']==event['remaining_after']
    assert run['metrics']['partial_exits']==0 and run['trades'][0]['exit_reason']=='STOP_OR_TRAILING'
    assert len(run['account']['orders'])==2


def test_gap_past_ladder_uses_open_price_and_consumes_each_target_once(monkeypatch):
    raw=dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(o='100',h='100',l='100',c='100')
    raw['trade']['bars'][3].update(o='110',h='110',l='110',c='110')
    strategy=definition()
    run=simulate_perpetual(read_perp_dataset(raw),partial_profile(strategy),strategy)
    assert [row['target_index'] for row in run['partial_events']]==[1,2]
    assert all(row['time_ns']==str(raw['trade']['bars'][3]['t']*10**9) and D(row['price'])==110 for row in run['partial_events'])
    assert D(run['trades'][0]['exit_price'])==110


def test_mark_liquidation_precedes_later_trade_target(monkeypatch):
    raw=dataset(monkeypatch).payload()
    raw['mark']['bars'][2].update(o='100',h='100',l='65',c='100')
    strategy=definition(stop_loss='0.8')
    configured=partial_profile(strategy,targets=[{'target':'0.07','fraction':'0.25'}],mark_path='O_L_H_C')
    run=simulate_perpetual(read_perp_dataset(raw),configured,strategy)
    assert run['partial_events']==[] and run['trades'][0]['exit_reason']=='LIQUIDATION'
    assert run['metrics']['liquidated'] and not run['metrics']['open_position']


def test_limit_ladder_ignores_targets_touched_before_the_entry(monkeypatch):
    strategy=definition(take_profit='0.4',stop_loss='0.8')
    configured=read_profile({**limit_profile(strategy).payload(),'schema':'orbis.lab.perp.profile.v3','partial_targets':TARGETS})
    run=simulate_perpetual(dataset(monkeypatch),configured,strategy)
    partial,=run['partial_events'];trade,=run['trades']
    assert D(trade['entry_price'])==98 and partial['target_index']==1
    assert int(partial['time_ns'])>int(trade['entry_time_ns'])


@pytest.mark.parametrize('targets',[
    [{'target':'0.02','fraction':'1'}],
    [{'target':'0.02','fraction':'0.6'},{'target':'0.04','fraction':'0.4'}],
    [{'target':'0.04','fraction':'0.2'},{'target':'0.02','fraction':'0.2'}],
    [{'target':'0.02','fraction':'0.2'},{'target':'0.020','fraction':'0.3'}],
])
def test_impossible_ladders_are_rejected(targets):
    with pytest.raises(ValueError):
        partial_profile(definition(),targets=targets)


def test_partial_cannot_overlap_the_final_target(monkeypatch):
    strategy=definition(take_profit='0.05')
    with pytest.raises(LabError,match='objetivo final'):
        simulate_perpetual(dataset(monkeypatch),partial_profile(strategy),strategy)
