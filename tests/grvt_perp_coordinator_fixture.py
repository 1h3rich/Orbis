"""Real source/worker/coordinator/journal, only venue I/O and time are owned."""
from decimal import Decimal
from threading import Event

import httpx

from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from tests.grvt_perp_bot_fixture import perp_bot_case
from tests.perp_observed_fixture import bar
from tests.test_grvt_testnet_signer import KEY,SIGNER


class OwnedPerpCoordinator:
    def __init__(self,directory,monkeypatch,*,direction='LONG',mode='ISOLATED',policy_changes=None,optimized=False):
        self.owned=perp_bot_case(directory,monkeypatch,direction=direction,mode=mode,
            policy_changes={'max_entries':1,**(policy_changes or {})},create_grant=False,optimized=optimized)
        self.case=self.owned.case
        self.ready=Event();self.next=Event();self.observing=Event()
        self.close_on_watch=True;self.worker_fail_on_watch=False
        self.on_watch=lambda:None
        self.direction=direction;self.mode=mode
        self.recorded=set();self.run_id=None
        self.service=GrvtMainnetBotService(self.case.vault,client_factory=self.factory,
            public_post=self.case.public,clock_ns=lambda:self.case.clock[0],waiter=self.waiter)

    def source(self,checked):
        return GrvtPerpSignalSource(adapter=self.owned.adapter,clock_ns=lambda:self.case.clock[0],
            require_permission=checked)

    def worker_waiter(self,stop,seconds):
        self.ready.set()
        while not self.next.wait(.01):
            if stop.is_set():return
        self.next.clear()

    def waiter(self,stop,seconds):
        if stop.is_set():return
        jobs=list(self.service._jobs.values())
        if jobs and jobs[-1].perp_worker.snapshot()['status'] not in ('RUNNING','PREPARED'):
            self.case.clock[0]+=int(seconds*10**9)
            self.owned.clock['now']=self.case.clock[0]
            return
        import time
        until=time.monotonic()+15
        while not self.ready.wait(.01):
            if stop.is_set():return
            if time.monotonic()>=until:raise AssertionError('Owned worker did not confirm a public record')
        self.ready.clear()
        if stop.is_set():return
        # Only advance while the independent producer is parked and no
        # private collection is in flight. We never backfill quote intervals.
        at=self.case.clock[0]+int(seconds*10**9)
        self.case.clock[0]=self.owned.clock['now']=at
        end=at//(60*10**9)*60
        stamp=max(self.owned.adapter.rows)+60
        while stamp<end:
            self.owned.adapter.rows[stamp]=bar(stamp,self.direction).json();stamp+=60
        jobs=list(self.service._jobs.values())
        watching=bool(jobs and jobs[-1].status=='WATCHING')
        if watching:
            self.observing.set()
            self.on_watch()
            if self.worker_fail_on_watch:self.owned.adapter.fail='quote'
            if self.close_on_watch:
                self.owned.adapter.quote_override={'bid':'151' if self.direction=='LONG' else '48',
                    'ask':'152' if self.direction=='LONG' else '49','mark_price':'100'}
        self.next.set()
        # A failed source exits before its waiter. Let the manager see that
        # status without blocking or substituting a fake source worker.
        if self.worker_fail_on_watch and watching:
            jobs[-1].perp_worker.join(15)
        elif not self.ready.wait(15) and not stop.is_set():
            raise AssertionError('Owned source did not complete its next poll')

    def factory(self,key):
        client=self.case.factory();post=client._post
        def private(url,body,headers):
            lost=None
            try:reply=post(url,body,headers)
            except httpx.ReadTimeout as error:lost=error;reply=None
            if (url.endswith('/bulk_orders') and len(body.get('orders',[]))==3
                    and body['orders'][0]['metadata']['client_order_id'] not in self.recorded):
                parent=body['orders'][0]['metadata']['client_order_id'];self.recorded.add(parent)
                entry=self.case.settings['orders'][parent];size=entry['legs'][0]['size']
                stamp=entry['metadata']['create_time'];fee=Decimal('.02')
                self.case.settings['histories']['fill_history']=[{
                    'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':'coordinator-entry-'+parent,
                    'order_id':entry['order_id'],'client_order_id':parent,'event_time':stamp,
                    'is_buyer':self.direction=='LONG','is_taker':True,'size':size,'price':'100',
                    'fee':str(fee),'fee_currency':'USDT','realized_pnl':'0','builder_fee':'0','is_liquidation':False}]
                margin=Decimal(size)*20;maintenance=Decimal(size)
                balance=Decimal(self.case.settings['account_balance'])-fee
                self.case.settings.update(account_balance=str(balance),account_equity=str(balance),
                    initial_margin=str(margin),maintenance_margin=str(maintenance),positions=[{
                        'sub_account_id':'123456','instrument':'BTC_USDT_Perp','event_time':stamp,
                        'size':size if self.direction=='LONG' else '-'+size,'margin_type':self.mode,'leverage':'5',
                        'isolated_balance':str(margin) if self.mode=='ISOLATED' else None,
                        'isolated_im':str(margin) if self.mode=='ISOLATED' else None,
                        'isolated_mm':str(maintenance) if self.mode=='ISOLATED' else None}])
            if lost is not None:raise lost
            return reply
        client._post=private
        return client

    def start(self,**changes):
        args=dict(api_key='synthetic-mainnet-api-key',private_key=KEY,expected_signer=SIGNER,
            expected_ticket=self.service.ticket('123456'),keep_operating=True,
            confirmation='ACTIVAR_BOT_PERPETUO_GRVT_MAINNET',source_factory=self.source,worker_waiter=self.worker_waiter)
        args.update(changes)
        started=self.service.start_perp(self.owned.origin,self.owned.terms,self.owned.policy,**args)
        self.run_id=started['id']
        return started

    def close(self):
        self.service.stop_all();self.next.set()
        for job in self.service._jobs.values():
            if job.perp_worker is not None:job.perp_worker.join(15)
        from tests.test_grvt_mainnet_bot_service import wait_for
        wait_for(lambda:not any(job.active for job in self.service._jobs.values()),seconds=20)
        self.case.vault.lock()
