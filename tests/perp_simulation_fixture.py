"""Owned synthetic candles, current snapshots and zero-rate hourly funding."""
from app.lab.engine import validate_strategy
from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_profile import PerpProfile
from app.lab.store import content_hash
from tests.perp_dataset_fixture import prepared


def definition(direction='LONG', **changes):
    raw={'direction':direction,'initial_capital':'1000','allocation':'0.5',
        'fee_rate':'0.001','slippage_rate':'0','stop_loss':'0.03','take_profit':'0.08',
        'entry':{'kind':'BULLISH_STREAK' if direction=='LONG' else 'BEARISH_STREAK','period':2}}
    raw.update(changes)
    return validate_strategy(raw)


def profile(strategy, **changes):
    raw={'schema':'orbis.lab.perp.profile.v1','strategy_version_id':'11111111-1111-4111-8111-111111111111',
        'strategy_hash':content_hash(strategy),'signal_engine':'lab-1.16','account_engine':'perp-account-0.1',
        'mode':'EXPERIMENTAL_CURRENT_RULES','margin_mode':'ISOLATED','leverage':'5',
        'trade_path':'O_H_L_C','mark_path':'O_H_L_C',
        'boundary_order':'PREVIOUS_CLOSE_THEN_FUNDING_THEN_OPEN','execution':'MARKET_WITH_ADVERSE_SLIPPAGE',
        'sizing':'CASH_ALLOCATION_INCLUDING_MARGIN_LOSS_BUFFER_AND_FEES','simultaneous_positions':1,
        'liquidation_action':'STOP_NEW_ENTRIES','end_action':'CLOSE_AT_LAST_MODELED_TRADE'}
    raw.update(changes)
    return PerpProfile.model_validate(raw)


def dataset(monkeypatch, direction='LONG'):
    raw=prepared(monkeypatch).payload()
    prices=[('90','95','90','95'),('95','100','95','100'),('100','110','90','100'),('100','100','100','100')]
    if direction=='SHORT':
        prices[0]=('110','110','105','105');prices[1]=('105','105','100','100')
    for bar,values in zip(raw['trade']['bars'],prices):
        bar.update(dict(zip(('o','h','l','c'),values)))
    for bar in raw['mark']['bars']:
        bar.update(o='100',h='100',l='100',c='100')
    for point in raw['funding']:
        point.update(mark_price='100',funding_rate='0')
    raw['rules']['risk_brackets'][0].update(initial_margin_rate='0.2',maintenance_margin_rate='0.05',max_leverage=5)
    return read_perp_dataset(raw)
