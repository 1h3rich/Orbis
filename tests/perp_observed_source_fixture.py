"""Owned public endpoint data; no network or account credentials."""
from copy import deepcopy
from decimal import Decimal

from app.derivatives.funding import HOUR_NS


class OwnedPublicAdapter:
    def __init__(self,seed,clock):
        self.seed=seed;self.clock=clock;self.calls=[]
        self.rows={row.t:row.model_dump() for row in seed.history}
        self.funding=[row.model_dump() for row in seed.funding]
        self.fail=None;self.quote_override={};self.advance_quote_ns=0

    def _call(self,name):
        self.calls.append(name)
        if self.fail==name:raise ValueError('Owned public source failure')

    def get_margin_rules(self,symbol):
        self._call('rules')
        return {**self.seed.rules.model_dump(),'fetched_at_ns':self.clock['now']}

    def get_instrument_spec(self,symbol):
        self._call('specification');spec=self.seed.specification
        return {'instrument':spec.instrument,'fetched_at_ns':self.clock['now'],'kind':'PERPETUAL','venues':['ORDERBOOK'],
            'min_size':spec.min_size,'size_step':spec.size_step,'min_notional':spec.min_notional,
            'tick_size':spec.tick_size,'max_position_size':spec.max_position_size,
            'base_decimals':-Decimal(spec.size_step).as_tuple().exponent}

    def get_historical_candles(self,symbol,timeframe,start,end,*,now):
        self._call('candles')
        return [deepcopy(row) for stamp,row in sorted(self.rows.items()) if start<=stamp<end]

    def get_funding_history(self,symbol,opened,end):
        self._call('funding');return [deepcopy(row) for row in reversed(self.funding) if opened-8*HOUR_NS<=int(row['funding_time'])<=end]

    def get_ticker(self,symbol):
        self._call('quote');self.clock['now']+=self.advance_quote_ns
        return {**self.seed.quote.model_dump(),'event_time_ns':self.clock['now'],**self.quote_override}
