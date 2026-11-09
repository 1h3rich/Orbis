"""Owned hourly inputs, constant MARK and closed bullish TRADE candles."""
from copy import deepcopy

from app.lab.perp_dataset import read_perp_dataset
from tests.perp_simulation_fixture import dataset, definition, profile


def validation_inputs(monkeypatch, count=120, *, direction='LONG', margin_mode='ISOLATED'):
    raw=dataset(monkeypatch).payload()
    start=raw['start'];raw['end']=start+count*3600
    trade=deepcopy(raw['trade']['bars'][0]);mark=deepcopy(raw['mark']['bars'][0])
    low,close=('99','101') if direction=='LONG' else ('98','99')
    raw['trade']['bars']=[{**trade,'t':start+i*3600,'o':'100','h':'102','l':low,'c':close} for i in range(count)]
    raw['mark']['bars']=[{**mark,'t':start+i*3600,'o':'100','h':'100','l':'100','c':'100'} for i in range(count)]
    funding=deepcopy(raw['funding'][0])
    raw['funding']=[{**funding,'funding_time':str((start+i*3600)*10**9),'mark_price':'100','funding_rate':'0'} for i in range(-1,count)]
    data=read_perp_dataset(raw)
    strategy=definition(direction,fee_rate='0',stop_loss='0.5',take_profit='0.5')
    return data,strategy,profile(strategy,margin_mode=margin_mode)
