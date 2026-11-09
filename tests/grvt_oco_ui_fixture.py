"""Venue HTTP sintético para UI/API OCO; ningún cliente sale a la red."""

from copy import deepcopy
from decimal import Decimal
import time

import httpx

from app.live_execution.grvt_testnet_order import (
    AUTH_URL,BULK_ORDERS_URL,LOOKUP_URL,FILL_HISTORY_URL,FUNDING_HISTORY_URL,
    TRANSFER_HISTORY_URL,WITHDRAWAL_HISTORY_URL,DEPOSIT_HISTORY_URL,INTEREST_HISTORY_URL,
    GrvtTestnetOrderClient,
)
from app.live_execution.grvt_testnet_signer import INSTRUMENT_URL
from app.live_execution.grvt_testnet_quote import TESTNET_TICKER_URL
from app.live_execution.grvt_testnet_evidence import list_testnet_audits
from tests.test_grvt_testnet_history_continuity import HistoryVenue
from tests.test_grvt_testnet_journal import SUB,CID,NOW_NS,order
from tests.test_grvt_testnet_oco import ready


PASSWORD = 'long test passphrase'


class OcoUiVenue:
    def __init__(self,directory,*,clock_ns=time.time_ns,direction='LONG'):
        self.directory = directory
        self.clock_ns = clock_ns
        self.direction = direction
        vault = ready(directory,side='BUY' if direction == 'LONG' else 'SELL')
        self.baseline = list_testnet_audits(vault,SUB,CID)[-1]
        vault.lock()
        self.balance = Decimal(self.baseline['account']['settle_balance'])
        self.position = Decimal('0.01')*(1 if direction == 'LONG' else -1)
        self.calls,self.orders,self.states = [],{},{}
        self.fill_rows,self.funding_rows = [],[]
        self.created = 0
        self.bulk_sends,self.cancel_sends = 0,0
        self.lose_create_reply = False
        self.reject_auth = False
        self.last_price = '50000'

    def factory(self,key):
        return GrvtTestnetOrderClient(key,http_post=self.post,clock_ns=self.clock_ns)

    def public(self,url,body):
        self.calls.append(url)
        if url == INSTRUMENT_URL:
            return httpx.Response(200,json={'result':{'instrument':'BTC_USDT_Perp','kind':'PERPETUAL',
                'venues':['ORDERBOOK'],'instrument_hash':'123456789','base_decimals':3,
                'tick_size':'0.1','min_size':'0.001','min_notional':'10'}})
        if url == TESTNET_TICKER_URL:
            value = {'instrument':'BTC_USDT_Perp','event_time':str(self.clock_ns()),
                'best_bid_price':'49999','best_ask_price':'50001','best_bid_size':'10','best_ask_size':'10'}
            if self.last_price is not None: value['last_price'] = self.last_price
            return httpx.Response(200,json={'result':value})
        return HistoryVenue.public_rules(url,body)

    def native_row(self,cid):
        original = self.orders[cid]
        state,size,at = self.states[cid]
        return {**deepcopy(original),'order_id':'0xoco'+str(list(self.orders).index(cid)),
            'metadata':{**deepcopy(original['metadata']),'create_time':str(self.created)},
            'state':{'status':state,'traded_size':[size],'update_time':str(at)}}

    def post(self,url,body,headers):
        self.calls.append(url)
        if url == AUTH_URL:
            if self.reject_auth: return httpx.Response(401,json={'error':'synthetic authentication refusal'})
            return httpx.Response(200,json={'status':'success','sub_account_id':SUB},
                headers={'X-Grvt-Account-Id':'0x'+'a'*40,'Set-Cookie':'gravity=fake-testnet-cookie; Path=/'},
                request=httpx.Request('POST',url))
        if url in (TRANSFER_HISTORY_URL,WITHDRAWAL_HISTORY_URL,DEPOSIT_HISTORY_URL,INTEREST_HISTORY_URL):
            return httpx.Response(200,json={'result':[],'next':''})
        assert body['sub_account_id'] == SUB
        if url == LOOKUP_URL:
            if body['client_order_id'] == CID:
                value = order(); value['legs'][0]['is_buying_asset'] = self.direction == 'LONG'
                value['order_id'] = '0x123abc'; value['metadata']['create_time'] = str(NOW_NS+1)
                value['state'] = {'status':'FILLED','traded_size':['0.01'],'update_time':str(NOW_NS+1000)}
            else: value = self.native_row(body['client_order_id'])
            return httpx.Response(200,json={'result':value})
        if url.endswith('/account_summary'):
            return httpx.Response(200,json={'result':{'sub_account_id':SUB,'event_time':str(self.clock_ns()),
                'settle_currency':'USDT','margin_type':'SIMPLE_CROSS_MARGIN','sub_account_mode':'SINGLE_ASSET_MODE',
                'total_equity':str(self.balance),'unrealized_pnl':'0',
                'initial_margin':'1' if self.position else '0','maintenance_margin':'0.5' if self.position else '0',
                'positions':[{'sub_account_id':SUB,'instrument':'BTC_USDT_Perp','size':str(self.position)}] if self.position else [],
                'spot_balances':[{'currency':'USDT','balance':str(self.balance)}]}})
        if url.endswith('/open_orders'):
            return httpx.Response(200,json={'result':[self.native_row(cid) for cid in self.orders
                if self.states[cid][0] in ('PENDING','OPEN')]})
        if url == BULK_ORDERS_URL:
            if not body['orders']:
                self.cancel_sends += 1
                assert len(body['client_order_i_ds']) == 1 and body['client_order_i_ds'][0] in self.orders
                at = self.clock_ns()
                for cid,(status,size,_) in list(self.states.items()):
                    if status in ('OPEN','PENDING'): self.states[cid] = ('CANCELLED',size,at)
                return httpx.Response(200,json={'result':{'orders':[],'cancel_acks':[{'ack':True}]}})
            assert len(body['orders']) == 2 and not self.orders
            self.bulk_sends += 1; self.created = self.clock_ns()
            for value in body['orders']:
                cid = value['metadata']['client_order_id']; self.orders[cid] = deepcopy(value)
                self.states[cid] = ('OPEN','0',self.created)
            if self.lose_create_reply: raise httpx.ReadTimeout('synthetic lost provider reply')
            return httpx.Response(200,json={'result':{'orders':[self.native_row(cid) for cid in self.orders],'cancel_acks':[]}})
        if url == FILL_HISTORY_URL:
            rows = []
            for value in self.baseline['fills']['rows']:
                rows.append({**value,'event_time':str(value['event_time_ns']),'sub_account_id':SUB,
                    'order_id':'0x123abc','client_order_id':CID,'instrument':'BTC_USDT_Perp',
                    'is_buyer':self.direction == 'LONG','is_liquidation':False})
            return httpx.Response(200,json={'result':deepcopy(rows+self.fill_rows),'next':''})
        if url == FUNDING_HISTORY_URL:
            return httpx.Response(200,json={'result':deepcopy(self.funding_rows),'next':''})
        raise AssertionError('Unexpected synthetic venue endpoint')

    def trigger(self,*,role='TAKE_PROFIT',size='0.01'):
        assert self.orders and not self.fill_rows
        at = self.clock_ns()
        for cid,order_value in self.orders.items():
            if order_value['metadata']['trigger']['trigger_type'] != role:
                self.states[cid] = ('CANCELLED','0',at); continue
            self.states[cid] = ('FILLED' if size == '0.01' else 'CANCELLED',size,at)
            price = Decimal(order_value['metadata']['trigger']['tpsl']['trigger_price'])
            pnl = (price-50000)*Decimal(size)*(1 if self.direction == 'LONG' else -1)
            self.position += Decimal(size)*(-1 if self.direction == 'LONG' else 1)
            self.balance += pnl-Decimal('0.05')-Decimal('0.02')
            self.fill_rows.append({'sub_account_id':SUB,'client_order_id':cid,
                'order_id':self.native_row(cid)['order_id'],'trade_id':'native-oco-fill','event_time':str(at),
                'instrument':'BTC_USDT_Perp','is_buyer':self.direction == 'SHORT','is_taker':True,
                'is_liquidation':False,'size':size,'price':str(price),'fee':'0.05',
                'fee_currency':'USDT','realized_pnl':str(pnl),'builder_fee':'0'})
            self.funding_rows.append({'sub_account_id':SUB,'tx_id':'native-oco-fill','event_time':str(at),
                'instrument':'BTC_USDT_Perp','currency':'USDT','amount':'0.02'})
