"""LAB/PAPER, API y SQLCipher reales; intercambio GRVT controlado en memoria."""

from copy import deepcopy
from decimal import Decimal,localcontext
import time

import httpx

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,ORDER_URL,BULK_ORDERS_URL,FILL_HISTORY_URL,FUNDING_HISTORY_URL,LOOKUP_URL
from tests.test_grvt_testnet_bracket_submit import live_group,PASSWORD


class BracketUiVenue:
    def __init__(self,directory,direction='LONG'):
        self.directory=directory
        (self.origin,self.source,self.vault,self.initial_orders,self.cap,self.instrument,self.transport,
            self.public,self.recorded)=live_group(directory,direction)
        self.clock_ns=time.time_ns
        self.recorded['clock_ns']=lambda:self.clock_ns()
        self.lose_cancel_reply=False; self.lose_close_reply=False
        self.fill_entry=True
        self.close_fill_size=None
        self.fills=[]; self.position=Decimal(0); self.balance=Decimal('10000')
        self.bulk_sends=0; self.cancel_sends=0; self.close_sends=0; self.lost=False
        self.states=self.recorded['venue_state']
        original_public=self.public
        def public(url,body):
            if url=='https://market-data.testnet.grvt.io/full/v1/kline':
                start=int(body['start_time'])//1_000_000_000
                end=(int(body['end_time'])+1)//1_000_000_000
                rows=[{'instrument':self.cap.instrument,'open_time':str(t*1_000_000_000),
                    'close_time':str((t+3600)*1_000_000_000),'open':'100','high':'101','low':'99',
                    'close':'100','volume_b':'10'} for t in range(start,end,3600)]
                return self.origin.response(url,{'result':list(reversed(rows)),'next':''})
            response=original_public(url,body)
            if url.endswith('/ticker'):
                payload=response.json(); payload['result']['mark_price']='100'
                return self.origin.response(url,payload)
            return response
        self.public=public

    def add_fill(self,order,size,stamp,*,realized=Decimal(0),price=None):
        leg=order['legs'][0]; price=price or leg['limit_price']
        self.fills.append({'sub_account_id':self.cap.sub_account_id,'order_id':order['order_id'],
            'client_order_id':order['metadata']['client_order_id'],'trade_id':'ui-bracket-fill-'+str(len(self.fills)),
            'event_time':str(stamp),'instrument':self.cap.instrument,'is_buyer':leg['is_buying_asset'],
            'is_taker':True,'is_liquidation':False,'size':size,'price':price,'fee':'0.01',
            'fee_currency':'USDT','realized_pnl':str(realized),'builder_fee':'0'})
        with localcontext() as ctx:
            ctx.prec=100
            self.position+=Decimal(size)*(1 if leg['is_buying_asset'] else -1)
            self.balance+=realized-Decimal('0.01')

    def trigger(self,role='TAKE_PROFIT',*,fill_size=None,cancel_sibling=True):
        assert role in ('STOP_LOSS','TAKE_PROFIT')
        orders=[self.states[order['metadata']['client_order_id']] for order in self.recorded['contracts']]
        parent,stop,target=orders; stamp=self.clock_ns(); size=fill_size or parent['legs'][0]['size']
        filled,other=(stop,target) if role=='STOP_LOSS' else (target,stop)
        if cancel_sibling: other['state'].update(status='CANCELLED',update_time=str(stamp))
        with localcontext() as ctx:
            ctx.prec=100
            total=Decimal(filled['state']['traded_size'][0])+Decimal(size)
            assert total<=Decimal(parent['legs'][0]['size'])
            filled['state'].update(status='FILLED' if total==Decimal(parent['legs'][0]['size']) else 'OPEN',
                traded_size=[format(total,'f')],update_time=str(stamp))
            sign=Decimal(1) if parent['legs'][0]['is_buying_asset'] else Decimal(-1)
            pnl=(Decimal(filled['legs'][0]['limit_price'])-Decimal(parent['legs'][0]['limit_price']))*Decimal(size)*sign
        self.add_fill(filled,size,stamp,realized=pnl)

    def factory(self,key):
        def post(url,body,headers):
            if url==BULK_ORDERS_URL and not body['orders']:
                self.cancel_sends+=1; stamp=str(self.clock_ns())
                for order in self.states.values():
                    if order['state']['status'] in ('OPEN','PENDING'):
                        order['state'].update(status='CANCELLED',update_time=stamp)
                if self.lose_cancel_reply: raise httpx.ReadTimeout('synthetic lost cancel reply')
                return self.origin.response(url,{'result':{'orders':[],'cancel_acks':[{'ack':True}]}})
            if url==BULK_ORDERS_URL:
                response=self.transport._post(url,body,headers); self.bulk_sends+=1
                first=self.states[body['orders'][0]['metadata']['client_order_id']]
                if self.fill_entry:
                    self.add_fill(first,first['legs'][0]['size'],int(first['state']['update_time']))
                else:
                    first['state'].update(status='CANCELLED',traded_size=['0'])
                if self.lost: raise httpx.ReadTimeout('synthetic lost linked reply')
                return response
            if url==ORDER_URL:
                self.close_sends+=1; stamp=self.clock_ns(); order=deepcopy(body['order'])
                cid=order['metadata']['client_order_id']; size=order['legs'][0]['size']
                filled=self.close_fill_size or size
                self.states[cid]={**order,'order_id':'0xuiemergency'+str(self.close_sends),
                    'metadata':{**order['metadata'],'create_time':str(stamp)},
                    'state':{'status':'FILLED' if filled==size else 'CANCELLED','traded_size':[filled],'update_time':str(stamp)}}
                parent=self.states[self.recorded['contracts'][0]['metadata']['client_order_id']]
                with localcontext() as ctx:
                    ctx.prec=100
                    sign=Decimal(1) if parent['legs'][0]['is_buying_asset'] else Decimal(-1)
                    pnl=(Decimal('100')-Decimal(parent['legs'][0]['limit_price']))*Decimal(filled)*sign
                self.add_fill(self.states[cid],filled,stamp,price='100',realized=pnl)
                if self.lose_close_reply: raise httpx.ReadTimeout('synthetic lost close reply')
                return self.origin.response(url,{'result':{**order,'order_id':self.states[cid]['order_id']}})
            if url==LOOKUP_URL and 'order_id' in body:
                row=next((order for order in self.states.values() if order['order_id']==body['order_id']),None)
                return self.origin.response(url,{'result':deepcopy(row)})
            if url.endswith('/account_summary'):
                return self.origin.response(url,{'result':{'sub_account_id':self.cap.sub_account_id,
                    'event_time':str(self.clock_ns()),'settle_currency':'USDT','margin_type':'SIMPLE_CROSS_MARGIN',
                    'sub_account_mode':'SINGLE_ASSET_MODE','total_equity':str(self.balance),
                    'initial_margin':'1' if self.position else '0','maintenance_margin':'0.5' if self.position else '0',
                    'unrealized_pnl':'0','positions':[{'sub_account_id':self.cap.sub_account_id,
                        'instrument':self.cap.instrument,'size':str(self.position)}] if self.position else [],
                    'spot_balances':[{'currency':'USDT','balance':str(self.balance)}]}})
            if url==FILL_HISTORY_URL:
                rows=[deepcopy(row) for row in self.fills if int(body['start_time'])<int(row['event_time'])<=int(body.get('end_time',self.clock_ns()+2_000_000_000))]
                return self.origin.response(url,{'result':rows,'next':''})
            if url==FUNDING_HISTORY_URL or url.endswith(('/transfer_history','/withdrawal_history','/deposit_history','/interest_payment_history')):
                return self.origin.response(url,{'result':[],'next':''})
            return self.transport._post(url,body,headers)
        return GrvtTestnetOrderClient(key,http_post=post,clock_ns=lambda:self.clock_ns())
