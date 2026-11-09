"""Manual experimental trajectories, independent owner cash outcomes."""
from decimal import Decimal as D, Inexact, ROUND_DOWN, localcontext
from fractions import Fraction
import json

import pytest
from pydantic import ValidationError

from app.lab.engine import LabError
from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_path import bar_path
from app.lab.perp_profile import PerpProfile
from app.lab.perp_simulation import simulate_perpetual
from tests.perp_dataset_fixture import START
from tests.perp_simulation_fixture import dataset,definition,profile


@pytest.mark.parametrize('direction,path,profit,reason',[
    ('LONG','O_H_L_C','193.826','TAKE_PROFIT'),
    ('LONG','O_L_H_C','-79.525375','STOP_OR_TRAILING'),
    ('SHORT','O_H_L_C','-79.674625','STOP_OR_TRAILING'),
    ('SHORT','O_L_H_C','194.224','TAKE_PROFIT')])
@pytest.mark.parametrize('mode',['ISOLATED','CROSS'])
def test_manual_path_scenarios_have_real_fees_and_no_future_signal(monkeypatch,direction,path,profit,reason,mode):
    data=dataset(monkeypatch,direction);strategy=definition(direction)
    result=simulate_perpetual(data,profile(strategy,trade_path=path,margin_mode=mode),strategy)
    assert result['status']=='COMPLETE' and len(result['trades'])==1
    trade=result['trades'][0]
    assert trade['quantity']=='24.875' and D(trade['net_profit'])==D(profit)
    assert trade['exit_reason']==reason
    assert trade['entry_time_ns']==str((START+2*3600)*10**9)
    assert trade['source_candle_t']==START+3600 and trade['signal_known_at_ns']==trade['entry_time_ns']
    assert D(result['metrics']['cash'])==D('1000')+D(profit)
    assert not result['metrics']['open_position'] and not result['metrics']['liquidated']
    assert result['mode']=='EXPERIMENTAL_CURRENT_RULES'
    assert result['paper_handoff_available'] is result['real_authority'] is False
    assert [e['index'] for e in result['journal']]==list(range(len(result['journal'])))
    assert all(type(e['cursor']['time_ns']) is str for e in result['journal'])
    assert [tuple((int(e['cursor']['time_ns']),e['cursor']['ordinal'])) for e in result['journal']]==sorted(
        (int(e['cursor']['time_ns']),e['cursor']['ordinal']) for e in result['journal'])
    json.dumps(result,allow_nan=False)


def test_stop_precedes_later_mark_liquidation_on_declared_path(monkeypatch):
    raw=dataset(monkeypatch).payload();raw['mark']['bars'][2].update(o='100',h='100',l='70',c='100')
    strategy=definition();data=read_perp_dataset(raw)
    result=simulate_perpetual(data,profile(strategy,trade_path='O_L_H_C',mark_path='O_L_H_C'),strategy)
    assert result['trades'][0]['exit_reason']=='STOP_OR_TRAILING'
    assert D(result['trades'][0]['net_profit'])==D('-79.525375')
    assert not result['metrics']['liquidated']
    # TRADE97 occurs with MARK91: a later MARK70 must not retroactively
    # liquidate a position already closed by the earlier stop.
    exit_ns=int(result['trades'][0]['exit_time_ns'])
    assert exit_ns<(START+2*3600+1200)*10**9


@pytest.mark.parametrize('mode,cash',[('ISOLATED','500.0125'),('CROSS','0')])
def test_liquidation_on_mark_is_not_a_trade_fill_at_threshold(monkeypatch,mode,cash):
    raw=dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(o='100',h='101',l='99',c='100')
    raw['mark']['bars'][2].update(o='100',h='100',l='50',c='100')
    strategy=definition();data=read_perp_dataset(raw)
    result=simulate_perpetual(data,profile(strategy,margin_mode=mode,trade_path='O_L_H_C',mark_path='O_L_H_C'),strategy)
    assert result['metrics']['liquidated'] and D(result['metrics']['cash'])==D(cash)
    assert result['trades'][0]['exit_reason']=='LIQUIDATION'
    assert result['trades'][0]['exit_price'] is None
    assert sum(e['kind']=='FILL' for e in result['journal'])==1
    assert not result['metrics']['open_position']
    size=Fraction(24875,1000)
    # Independent continuous one-tier equation: balance +q*(M-100)=q*M*5%.
    balance=Fraction('497.5') if mode=='ISOLATED' else Fraction('997.5125')
    threshold=(size*100-balance)/(size*Fraction('0.95'))
    offset=Fraction(1200*10**9)*(100-threshold)/50
    expected=(offset.numerator+offset.denominator-1)//offset.denominator
    assert int(result['trades'][0]['exit_time_ns'])==(START+2*3600)*10**9+expected


def test_funding_at_entry_boundary_is_before_entry_then_next_payment_is_booked(monkeypatch):
    raw=dataset(monkeypatch).payload()
    for bar in raw['trade']['bars'][2:]:bar.update(o='100',h='100',l='100',c='100')
    # The huge boundary rate must not be charged retrospectively to the entry.
    for point in raw['funding']:
        if int(point['funding_time'])==(START+2*3600)*10**9:point['funding_rate']='50'
        if int(point['funding_time'])==(START+3*3600)*10**9:point['funding_rate']='0.1'
    strategy=definition(max_holding_bars=1);data=read_perp_dataset(raw)
    result=simulate_perpetual(data,profile(strategy),strategy)
    assert D(result['metrics']['funding_paid'])==D('2.4875')
    assert D(result['metrics']['cash'])==D('992.5375')
    assert result['trades'][0]['exit_reason']=='TIME_LIMIT'
    boundary=[e['kind'] for e in result['journal'] if e['cursor']['time_ns']==str((START+2*3600)*10**9)]
    assert boundary.index('FUNDING_NO_POSITION')<boundary.index('FILL')


def test_window_uses_prefix_for_signal_but_never_inherits_prefix_positions(monkeypatch):
    data=dataset(monkeypatch);strategy=definition()
    full=simulate_perpetual(data,profile(strategy),strategy)
    heldout=simulate_perpetual(data,profile(strategy),strategy,entry_start_index=2)
    assert heldout['signals'][0]['warmup_context'] is True
    assert heldout['trades']==full['trades']
    assert heldout['window']['requested_start_ns']==str((START+2*3600)*10**9)
    assert heldout['metrics']['cash']==full['metrics']['cash']


def test_replay_and_future_suffix_are_independent_of_external_decimal_context(monkeypatch):
    data=dataset(monkeypatch);strategy=definition();bound=profile(strategy)
    original=simulate_perpetual(data,bound,strategy)
    with localcontext() as context:
        context.prec=4;context.rounding=ROUND_DOWN;context.traps[Inexact]=True
        replay=simulate_perpetual(data,bound,strategy)
    assert replay==original
    raw=data.payload()
    raw['trade']['bars'][3].update(o='200',h='200',l='200',c='200')
    raw['mark']['bars'][3].update(o='200',h='200',l='100',c='200')
    changed=simulate_perpetual(read_perp_dataset(raw),bound,strategy)
    assert changed['trades']==original['trades']
    assert changed['dataset_hash']!=original['dataset_hash']


@pytest.mark.parametrize('changes',[{'mode':'REALISTIC'},{'leverage':5},{'leverage':'0'},
    {'simultaneous_positions':True},{'simultaneous_positions':2},{'account_engine':'perp-account-0.0'},
    {'execution':'LIMIT_MAKER'},{'boundary_order':'FUNDING_AFTER_ENTRY'},{'unknown':'x'}])
def test_unimplemented_or_inexact_profile_is_rejected(changes):
    raw=profile(definition()).payload();raw.update(changes)
    with pytest.raises((ValueError,ValidationError)):
        PerpProfile.model_validate(raw)


def test_profile_strategy_tampering_and_inconsistent_mark_funding_are_rejected(monkeypatch):
    data=dataset(monkeypatch);strategy=definition();bound=profile(strategy)
    with pytest.raises(LabError,match='definición'):
        simulate_perpetual(data,bound,definition(stop_loss='0.04'))
    raw=data.payload();raw['funding'][2]['mark_price']='101'
    with pytest.raises(LabError,match='fuera del rango'):
        simulate_perpetual(read_perp_dataset(raw),bound,strategy)


def test_synthetic_paths_preserve_extremes_and_funding_is_an_actual_mark_anchor(monkeypatch):
    raw=dataset(monkeypatch).payload()
    # Shift all hourly funding by20min so the third observation collides
    # with a synthetic one-third MARK knot in a one-hour candle.
    for point in raw['funding']:
        point['funding_time']=str(int(point['funding_time'])+1200*10**9)
    # End coverage needs the last settlement inside the4h window and the
    # unique anchor remains before its start, as the dataset validator proves.
    raw['mark']['bars'][2].update(o='100',h='110',l='90',c='100')
    data=read_perp_dataset(raw)
    points=bar_path(data,2,'O_L_H_C','O_H_L_C')
    stamp=(START+2*3600+1200)*10**9
    anchor=next(p for p in points if p.time_ns==stamp)
    extreme=next(p for p in points if p.time_ns==stamp+1)
    assert anchor.funding is not None and anchor.mark==D('100') and extreme.mark==D('110')
    assert min(p.mark for p in points)==D('90') and max(p.mark for p in points)==D('110')


def test_discontinuous_risk_tier_is_checked_even_when_segment_endpoints_are_healthy(monkeypatch):
    raw=dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(o='100',h='100',l='100',c='100')
    raw['mark']['bars'][2].update(o='100',h='110',l='100',c='100')
    first=raw['rules']['risk_brackets'][0];first['notional_cap']='2500'
    raw['rules']['risk_brackets'].append({'tier':2,'notional_floor':'2500','notional_cap':None,
        'initial_margin_rate':'0.4','maintenance_margin_rate':'0.25','max_leverage':5,
        'cumulative_maintenance_amount':'0'})
    strategy=definition();result=simulate_perpetual(read_perp_dataset(raw),profile(strategy),strategy)
    assert result['trades'][0]['exit_reason']=='LIQUIDATION'
    assert D(result['metrics']['cash'])==D('500.0125')
    # At the new tier equity510 <MM625. At MARK110 equity746.25 >MM684.0625:
    # checking only the high node would miss this earlier liquidation.
    expected=Fraction(1200*10**9)*(Fraction(2500)/Fraction('24.875')-100)/10
    expected_ns=(expected.numerator+expected.denominator-1)//expected.denominator
    assert int(result['trades'][0]['exit_time_ns'])==(START+2*3600)*10**9+expected_ns


def test_exit_rule_known_at_close_executes_next_open_with_its_explanation(monkeypatch):
    raw=dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(o='100',h='110',l='100',c='110')
    raw['trade']['bars'][3].update(o='110',h='110',l='110',c='110')
    strategy=definition(stop_loss='0.5',take_profit='0.5',exit={'kind':'CLOSE_ABOVE_SMA','period':2})
    result=simulate_perpetual(read_perp_dataset(raw),profile(strategy),strategy)
    trade=result['trades'][0]
    assert trade['exit_reason']=='EXIT_RULE_NEXT_OPEN'
    assert trade['exit_time_ns']==str((START+3*3600)*10**9)
    assert trade['exit_signal_known_at_ns']==trade['exit_time_ns']
    assert trade['exit_explanation']['matched'] is True
    assert D(trade['net_profit'])==D('243.52625')


def test_trailing_high_is_not_used_retroactively_inside_its_candle(monkeypatch):
    raw=dataset(monkeypatch).payload()
    raw['trade']['bars'][2].update(o='100',h='120',l='100',c='110')
    raw['trade']['bars'][3].update(o='110',h='110',l='110',c='110')
    strategy=definition(stop_loss='0.5',take_profit='0.5',trailing_stop='0.05')
    result=simulate_perpetual(read_perp_dataset(raw),profile(strategy),strategy)
    trade=result['trades'][0]
    assert trade['exit_time_ns']==str((START+3*3600)*10**9)
    assert D(trade['exit_price'])==D('110') and D(trade['net_profit'])==D('243.52625')


def test_artificial_close_uses_trade_not_mark_and_holdout_does_not_inherit_a_position(monkeypatch):
    data=dataset(monkeypatch);strategy=definition(stop_loss='0.5',take_profit='0.5')
    full=simulate_perpetual(data,profile(strategy),strategy)
    assert full['trades'][0]['exit_reason']=='END_OF_WINDOW_ARTIFICIAL_CLOSE'
    assert D(full['metrics']['cash'])==D('995.025')
    heldout=simulate_perpetual(data,profile(strategy),strategy,entry_start_index=3)
    assert not heldout['trades'] and not heldout['metrics']['open_position']
    assert D(heldout['metrics']['cash'])==D('1000')
    assert not any(e['kind']=='FILL' for e in heldout['journal'])


def test_rejected_exit_halts_with_exposure_instead_of_inventing_close(monkeypatch):
    data=dataset(monkeypatch);strategy=definition(slippage_rate='0.99')
    result=simulate_perpetual(data,profile(strategy),strategy)
    assert result['status']=='HALTED_EXECUTION' and result['metrics']['open_position']
    assert result['halt_reason'].startswith('EXIT_PRE_TRADE_CANCELLED:')
    assert not result['trades']
    assert sum(e['kind']=='FILL' for e in result['journal'])==1
    assert int(result['window']['processed_through_ns'])<int(result['window']['requested_end_ns'])


def test_interior_funding_anchor_is_charged_after_entry_at_its_exact_time(monkeypatch):
    raw=dataset(monkeypatch).payload()
    for bar in raw['trade']['bars'][2:]:bar.update(o='100',h='100',l='100',c='100')
    for point in raw['funding']:
        point['funding_time']=str(int(point['funding_time'])+1200*10**9)
        point['funding_rate']='0.1' if int(point['funding_time'])==(START+2*3600+1200)*10**9 else '0'
    strategy=definition();result=simulate_perpetual(read_perp_dataset(raw),profile(strategy),strategy)
    charges=[e for e in result['journal'] if e['kind']=='FUNDING' and D(e['funding_payment'])!=0]
    assert len(charges)==1 and D(charges[0]['funding_payment'])==D('2.4875')
    assert charges[0]['cursor']['time_ns']==str((START+2*3600+1200)*10**9)
    assert D(result['metrics']['cash'])==D('992.5375')


@pytest.mark.parametrize('start,end',[(True,None),(-1,None),(0,True),(0,5),(2,2)])
def test_invalid_execution_window_is_rejected(monkeypatch,start,end):
    data=dataset(monkeypatch);strategy=definition()
    with pytest.raises(LabError,match='Ventana'):
        simulate_perpetual(data,profile(strategy),strategy,entry_start_index=start,end_index=end)
