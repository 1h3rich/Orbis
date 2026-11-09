"""Independent accounts/windows, causal prefixes and explicit path hypotheses."""
from decimal import Decimal, localcontext, ROUND_DOWN

import pytest

from app.lab.engine import LabError
from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_simulation import simulate_perpetual
from app.lab.perp_validation import PerpValidationPlan, calculate_perp_validation, window_specs
from app.lab.store import content_hash
from tests.perp_validation_fixture import validation_inputs


def test_holdout_uses_previous_closed_signal_but_fresh_cash_and_no_inherited_position(monkeypatch):
    data,strategy,profile=validation_inputs(monkeypatch,60)
    plan=PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT',train_fraction='0.5')
    result=calculate_perp_validation(data,profile,strategy,plan)
    assert [w['bars'] for w in result['windows']]==[30,30]
    train,reserve=result['windows']
    assert reserve['from_t']==data.start+30*3600 and train['to_t']+3600==reserve['from_t']
    assert train['metrics']==reserve['metrics'] or train['metrics']['final_equity']==reserve['metrics']['final_equity']
    detail=simulate_perpetual(data,profile,strategy,entry_start_index=30)
    assert content_hash(detail)==reserve['result_hash']
    assert detail['trades'][0]['source_candle_t']==data.start+29*3600
    assert detail['trades'][0]['entry_time_ns']==str((data.start+30*3600)*10**9)
    assert Decimal(detail['trades'][0]['quantity'])==Decimal('24.999')
    assert Decimal(detail['metrics']['final_equity'])==Decimal('1024.999')
    assert detail['trades'][0]['exit_reason']=='END_OF_WINDOW_ARTIFICIAL_CLOSE'
    assert result['reserve_bars']==30 and result['reserve_trades']==1
    assert result['execution_authority']=='NONE'


@pytest.mark.parametrize('folds',[2,3,5])
def test_rolling_reserves_are_disjoint_and_each_account_restarts(monkeypatch,folds):
    data,strategy,profile=validation_inputs(monkeypatch,30*(folds+1)+7)
    result=calculate_perp_validation(data,profile,strategy,PerpValidationPlan(method='ROLLING_FIXED_VERSION',folds=folds))
    reserves=[w for w in result['windows'] if w['role']=='RESERVE']
    assert len(reserves)==folds
    assert all(w['bars']>=30 and Decimal(w['metrics']['final_equity'])==Decimal('1024.999') for w in reserves)
    assert all(a['end_index']==b['entry_start_index'] for a,b in zip(reserves,reserves[1:]))
    assert reserves[-1]['end_index']==len(data.trade.bars)
    assert result['reserve_bars']==sum(w['bars'] for w in reserves)
    assert 'net_equity_change' not in result  # no fake concatenated portfolio


def test_sensitivity_tests_all_four_independent_paths_without_oos_claim(monkeypatch):
    data,strategy,profile=validation_inputs(monkeypatch,4)
    result=calculate_perp_validation(data,profile,strategy,PerpValidationPlan(method='PATH_SENSITIVITY'))
    assert len(result['windows'])==4 and result['reserve_bars']==result['reserve_trades']==0
    assert {(w['profile']['trade_path'],w['profile']['mark_path']) for w in result['windows']}=={
        ('O_H_L_C','O_H_L_C'),('O_H_L_C','O_L_H_C'),('O_L_H_C','O_H_L_C'),('O_L_H_C','O_L_H_C')}
    assert 'PATH_SCENARIOS_ARE_NOT_OUT_OF_SAMPLE' in result['warnings']
    assert all(w['role']=='SCENARIO' for w in result['windows'])


def test_later_prices_do_not_change_development_result_and_decimal_context_is_owned(monkeypatch):
    data,strategy,profile=validation_inputs(monkeypatch,60)
    plan=PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT',train_fraction='0.5')
    original=calculate_perp_validation(data,profile,strategy,plan)
    raw=data.payload()
    for bar in raw['trade']['bars'][30:]:bar.update(o='200',h='205',l='190',c='202')
    changed=calculate_perp_validation(read_perp_dataset(raw),profile,strategy,plan)
    # The full original dataset identity intentionally differs; execution before
    # the cut, its metrics and events must not depend on that future suffix.
    assert original['windows'][0]['metrics']==changed['windows'][0]['metrics']
    before=simulate_perpetual(data,profile,strategy,end_index=30)
    after=simulate_perpetual(read_perp_dataset(raw),profile,strategy,end_index=30)
    assert before['trades']==after['trades'] and before['journal']==after['journal']
    with localcontext() as context:
        context.prec=6;context.rounding=ROUND_DOWN
        assert calculate_perp_validation(data,profile,strategy,plan)==original


@pytest.mark.parametrize('raw',[
    {'method':'CHRONOLOGICAL_HOLDOUT'},
    {'method':'CHRONOLOGICAL_HOLDOUT','train_fraction':0.7},
    {'method':'CHRONOLOGICAL_HOLDOUT','train_fraction':'0.9'},
    {'method':'CHRONOLOGICAL_HOLDOUT','train_fraction':'0.5','folds':2},
    {'method':'ROLLING_FIXED_VERSION','folds':True},
    {'method':'ROLLING_FIXED_VERSION','folds':1},
    {'method':'ROLLING_FIXED_VERSION','folds':3,'train_fraction':'0.5'},
    {'method':'PATH_SENSITIVITY','folds':2},
    {'method':'PATH_SENSITIVITY','extra':1},
])
def test_plan_rejects_unimplemented_or_ambiguous_parameters(raw):
    with pytest.raises(ValueError):PerpValidationPlan.model_validate(raw)


def test_short_reserve_and_rolling_windows_fail_before_simulation(monkeypatch):
    data,_,profile=validation_inputs(monkeypatch,59)
    for plan in [PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT',train_fraction='0.5'),PerpValidationPlan(method='ROLLING_FIXED_VERSION',folds=2)]:
        with pytest.raises(LabError,match='30 velas'):window_specs(data,profile,plan)


def test_liquidation_in_development_never_stops_new_reserve_account(monkeypatch):
    data,strategy,profile=validation_inputs(monkeypatch,60)
    raw=data.payload();raw['mark']['bars'][10].update(l='10')
    result=calculate_perp_validation(read_perp_dataset(raw),profile,strategy,
        PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT',train_fraction='0.5'))
    train,reserve=result['windows']
    assert train['metrics']['liquidated'] is True
    assert reserve['metrics']['liquidated'] is False
    assert Decimal(reserve['metrics']['final_equity'])==Decimal('1024.999')


def test_funding_at_reserve_open_precedes_entry_but_next_hour_really_changes_cash(monkeypatch):
    data,strategy,profile=validation_inputs(monkeypatch,60)
    raw=data.payload();cut=data.start+30*3600
    for point in raw['funding']:
        if int(point['funding_time']) in (cut*10**9,(cut+3600)*10**9):
            point['funding_rate']='1'
    changed=read_perp_dataset(raw)
    detail=simulate_perpetual(changed,profile,strategy,entry_start_index=30)
    assert Decimal(detail['metrics']['funding_paid'])==Decimal('24.999')
    assert Decimal(detail['metrics']['final_equity'])==Decimal('1000')
    report=calculate_perp_validation(changed,profile,strategy,
        PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT',train_fraction='0.5'))
    assert report['windows'][1]['metrics']==detail['metrics']
    assert Decimal(report['windows'][0]['metrics']['funding_paid'])==0
