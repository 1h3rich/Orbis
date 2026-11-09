"""Bounded UI selections from *computed replay*, never from loaded checkpoints.

Header counts only choose a display window. Nothing is returned until the whole
prefix, header and indexes have passed the original replay verification.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import deque
import json

from app.derivatives.perp_observed_types import ObservedError,ObservedState
from app.derivatives.perp_observed_wire import rules_input
from app.lab.engine import INTERVALS
from app.lab.perp_account import equity
from app.lab.perp_book_types import exact


MARGIN_CHANGES_LIMIT = 20


class ObservedReplayView:
    def __init__(self,*,before_t: int|None=None,candle_limit: int=1000,trade_offset: int=0,
                 trade_limit: int=50,marker_offset: int=0,marker_limit: int=5000):
        if (before_t is not None and (type(before_t) is not int or before_t<1)
                or any(type(n) is not int or n<0 for n in (trade_offset,marker_offset))
                or type(candle_limit) is not int or not 1<=candle_limit<=2000
                or type(trade_limit) is not int or not 1<=trade_limit<=100
                or type(marker_limit) is not int or not 1<=marker_limit<=5000):
            raise ObservedError('Ventana de inspección no válida')
        self.before_t=before_t;self.candle_limit=candle_limit;self.trade_offset=trade_offset
        self.trade_limit=trade_limit;self.marker_offset=marker_offset;self.marker_limit=marker_limit
        self.trades=[];self.markers=[];self.recent=deque(maxlen=30)
        self.trade_count=0;self.marker_count=0
        self.margin_initial = None
        self.margin_changes = deque(maxlen=MARGIN_CHANGES_LIMIT)
        self.margin_change_count = 0
        self.previous_rules = None

    def bind(self,header) -> None:
        # These are selection parameters, not a restored financial state.
        raw=json.loads(header['checkpoint_json']);history=raw['history'];step=INTERVALS[raw['timeframe']]
        end=min(self.before_t or history['last_t']+step,history['last_t']+step)
        end_index=min(history['count'],max(0,(end-history['first_t']+step-1)//step))
        self.from_ns=(history['first_t']+max(0,end_index-self.candle_limit)*step)*10**9
        self.through_ns=self.before_t*10**9 if self.before_t is not None else max(
            int(raw['last_received_ns']),int(raw['last_control_ns']))+1
        self.trade_end=max(0,raw['closed_trades']-self.trade_offset)
        self.trade_start=max(0,self.trade_end-self.trade_limit)

    def _consume_margin(self, sequence: int, state: ObservedState) -> None:
        rules = state.account.markets[0].rules
        previous = self.previous_rules
        self.previous_rules = rules
        if previous is None:
            self.margin_initial = self._margin_point(sequence, state)
            return
        changes = []
        if rules.max_size != previous.max_size:
            changes.append('POSITION_LIMIT')
        if rules.tiers != previous.tiers:
            changes.append('RISK_TIERS')
        if changes:
            self.margin_change_count += 1
            self.margin_changes.append({**self._margin_point(sequence, state), 'changes': changes})

    @staticmethod
    def _margin_point(sequence: int, state: ObservedState) -> dict:
        return {'sequence': sequence, 'applied_at_ns': str(state.last_received_ns),
                'rules': rules_input(state.account.markets[0].rules).model_dump(mode='json')}

    def consume(self,sequence: int,evidence: dict,state: ObservedState) -> None:
        self._consume_margin(sequence, state)
        for trade in evidence['trades']:
            if self.trade_start<=self.trade_count<self.trade_end:
                self.trades.append({'index':self.trade_count,**trade})
            self.trade_count+=1
        direction=json.loads(state.definition_json)['direction']
        reasons={row.get('order_id'):row.get('reason') for row in evidence['events'] if row.get('kind')=='ORDER_INTENT'}
        reasons.update((row['order_id'], 'ENTRY_LIMIT') for row in evidence['events']
                       if row.get('kind') == 'LIMIT_FILLED_OBSERVED_QUOTE')
        partials={row['order_id']:row['target_index'] for row in evidence['events']
                  if row.get('kind')=='PARTIAL_TARGET_FILLED'}
        for index,entry in enumerate(evidence['journal']):
            if entry['kind']!='FILL' or entry['price'] is None:continue
            stamp=int(entry['cursor']['time_ns'])
            if not self.from_ns<=stamp<self.through_ns:continue
            if self.marker_offset<=self.marker_count<self.marker_offset+self.marker_limit:
                marker={'id':str(sequence)+':'+str(index),'time_ns':str(stamp),
                    'price':entry['price'],'entry':(not entry['size_delta'].startswith('-'))==(direction=='LONG'),
                    'direction':direction,'reason':reasons.get(entry['order_id']),'order_id':entry['order_id']}
                if entry['order_id'] in partials:
                    marker['partial_index']=partials[entry['order_id']]
                self.markers.append(marker)
            self.marker_count+=1
        self.recent.append({'sequence':sequence,'events':evidence['events'],
                            'signals':evidence['signals'],'journal':evidence['journal']})

    @exact
    def payload(self,state: ObservedState) -> dict:
        stamps=[bar.t for bar in state.history]
        end=bisect_left(stamps,self.before_t) if self.before_t is not None else len(stamps)
        start=max(0,end-self.candle_limit);book=state.account;value=equity(book)
        return {'verification':'OBSERVED_REPLAY_VERIFIED','environment':'PAPER',
            'timeframe':state.timeframe,'metrics':{'initial_capital':format(book.initial_cash,'f'),
                'equity':format(value,'f'),'net_profit':format(value-book.initial_cash,'f'),
                'cash':format(book.cash,'f'),'realized_pnl':format(book.realized_pnl,'f'),
                'fees_paid':format(book.fees_paid,'f'),'funding_paid':format(book.funding_paid,'f')},
            'candles':[bar.json() for bar in state.history[start:end]],'has_older':start>0,
            'candle_count':len(state.history),'markers':self.markers,'marker_total':self.marker_count,
            'marker_offset':self.marker_offset,'has_more_markers':self.marker_offset+len(self.markers)<self.marker_count,
            'trades':list(reversed(self.trades)),'trade_total':self.trade_count,'trade_offset':self.trade_offset,
            'margin_rules': {
                'initial': self.margin_initial, 'changes': list(reversed(self.margin_changes)),
                'change_total': self.margin_change_count,
                'last_capture_at_ns': str(book.markets[0].rules.fetched_at_ns),
            },
            'recent_events':list(self.recent),'execution_authority':'NONE'}
