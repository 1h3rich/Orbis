"""Public-wire fixture, no exchange credentials or user storage."""
from copy import deepcopy

from app.marketdata.adapters.grvt import GrvtAdapter

START=1735689600
END=START+4*3600
NOW=1793879000123456789
SYMBOL='BTC/USDT:PERP'
INSTRUMENT='BTC_USDT_Perp'
PASSWORD='owned perp dataset test password'


def candle(index,*,kind='TRADE'):
    price=100+index+(7 if kind=='MARK' else 0)
    return {'instrument':INSTRUMENT,'open_time':str((START+index*3600)*10**9),
        'close_time':str((START+(index+1)*3600)*10**9),
        'open':str(price),'high':str(price+2),'low':str(price-2),'close':str(price+1),
        'volume_b':'12.34'}


def rules():
    return {'instrument':INSTRUMENT,'max_position_size':'100',
        'risk_brackets':[{'tier':1,'notional_floor':'0','notional_cap':None,
            'initial_margin_rate':'0.02','maintenance_margin_rate':'0.01',
            'max_leverage':50,'cumulative_maintenance_amount':'0'}]}


def specification():
    return {'instrument':INSTRUMENT,'kind':'PERPETUAL','venues':['ORDERBOOK'],
        'base_decimals':3,'min_size':'0.001','min_notional':'5',
        'tick_size':'0.01','max_position_size':'100'}


def source(monkeypatch,hook=None):
    calls=[]
    monkeypatch.setattr('app.marketdata.adapters.grvt.time.time_ns',lambda:NOW)
    def post(url,body):
        calls.append((url,deepcopy(body)))
        if hook:hook(url,body)
        if url.endswith('/kline'):
            return {'result':[candle(i,kind=body['type']) for i in (3,2,1,0)],'next':''}
        if url.endswith('/funding'):
            return {'result':[{'instrument':INSTRUMENT,'funding_time':str((START+i*3600)*10**9),
                'funding_rate':'0.01' if i%2 else '-0.02','mark_price':str(107+i),
                'funding_interval_hours':1} for i in (3,2,1,0,-1)],'next':''}
        if url.endswith('/margin_rules'):return rules()
        if url.endswith('/instrument'):return {'result':specification()}
        raise AssertionError(url)
    return GrvtAdapter(http_post=post),calls


def prepared(monkeypatch):
    from app.lab.perp_dataset import prepare_perp_dataset
    adapter,_=source(monkeypatch)
    return prepare_perp_dataset(SYMBOL,'1h',START,END,adapter=adapter,clock=lambda:NOW)
