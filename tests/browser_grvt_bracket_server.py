"""Servidor auxiliar: interfaz y persistencia reales, sólo proveedor ficticio."""

import os
from pathlib import Path

directory=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
os.environ['ORBIS_DATABASE_URL']='sqlite://'
for name,file in (('ORBIS_LAB_PATH','bootstrap-lab.db'),('ORBIS_AUTOMATION_PATH','bootstrap-dca.db'),
    ('ORBIS_PAPER_PERP_PATH','bootstrap-perp.db'),('ORBIS_SPOT_STRATEGY_PATH','bootstrap-spot.db'),('ORBIS_REAL_VAULT_PATH','bootstrap-real.db')):
    os.environ[name]=str(directory/file)

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.portfolio.api import create_real_router
from app.web.mount import register_ui
from tests.grvt_bracket_ui_fixture import BracketUiVenue
import time

venue=BracketUiVenue(directory); venue.lost=os.environ.get('ORBIS_TEST_BRACKET_LOST_REPLY')=='1'
if os.environ.get('ORBIS_TEST_NATIVE_RECOVERY')=='ORPHAN': venue.fill_entry=False
if os.environ.get('ORBIS_TEST_BOUNDED_BOT')=='1' or os.environ.get('ORBIS_TEST_NATIVE_RECOVERY'):
    offset=[0]
    venue.clock_ns=lambda:time.time_ns()+offset[0]
    venue.advance_bot_clock=lambda seconds:offset.__setitem__(0,offset[0]+int(seconds*1_000_000_000))
if os.environ.get('ORBIS_TEST_AUTOMATIC_BOT')=='1':
    pending=venue.origin.seed('LONG',defer_open=True)
    venue.source={**pending,'sub_account_id':venue.cap.sub_account_id}
venue.lose_cancel_reply=os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_LOST_REPLY')=='1'
venue.lose_close_reply=venue.lose_cancel_reply
if os.environ.get('ORBIS_TEST_STRATEGY_CLOSE_PARTIAL')=='1': venue.close_fill_size='0.1'
stream_factory=None
if os.environ.get('ORBIS_TEST_EXIT_WATCH')=='1':
    import json,time
    from app.marketdata.grvt_stream import GrvtTickerStream,TESTNET_URL
    venue.watch_mark='100'; venue.watch_connected=False; venue.watch_closed=False
    class WatchSocket:
        def __init__(self): self.initial=True; self.sequence=0
        def send(self,message):
            self.subscribe=json.loads(message)
            assert self.subscribe['params']['selectors']==[venue.cap.instrument+'@500']
        def recv(self,*,timeout):
            if self.initial:
                self.initial=False
                return json.dumps({'id':1,'method':'subscribe','result':{'stream':'v1.ticker.s',
                    'subs':[venue.cap.instrument+'@500']}})
            time.sleep(.3); self.sequence+=1
            if os.environ.get('ORBIS_TEST_NATIVE_PARTIAL')=='1':
                from decimal import Decimal
                from app.derivatives.paper_math import PerpQuote
                stamp=venue.clock_ns()
                venue.origin.clock['now']=stamp//1_000_000_000; venue.origin.monitor.clock_ns=venue.clock_ns
                assert venue.origin.monitor.on_risk_quote(venue.source['session_id'],PerpQuote(
                    venue.cap.instrument,stamp,Decimal('100'),Decimal('99.99'),Decimal('100.01'))) in ('OBSERVED','NO_POSITION')
            return json.dumps({'stream':'v1.ticker.s','selector':venue.cap.instrument+'@500',
                'sequence_number':str(self.sequence),'prev_sequence_number':str(self.sequence-1),
                'feed':{'instrument':venue.cap.instrument,'event_time':str(venue.clock_ns()),
                    'mark_price':venue.watch_mark,'best_bid_price':'99.99','best_ask_price':'100.01'}})
        def close(self): venue.watch_closed=True
    def connector(url,**kwargs):
        assert url==TESTNET_URL; venue.watch_connected=True; return WatchSocket()
    stream_factory=lambda symbol,clock:GrvtTickerStream((symbol,),connector=connector,clock_ns=clock,network='TESTNET')
app=FastAPI()
def public(url,body):
    reply=venue.public(url,body)
    if os.environ.get('ORBIS_TEST_PROVIDER_MINIMUM') and url.endswith('/instrument'):
        payload=reply.json(); payload['result']['min_notional']=os.environ['ORBIS_TEST_PROVIDER_MINIMUM']
        return venue.origin.response(url,payload)
    return reply
app.include_router(create_real_router(directory/'real.db',lab_path=venue.origin.lab.path,
    paper_perp_path=venue.origin.paper.path,grvt_testnet_factory=venue.factory,
    grvt_testnet_bracket_public_post=public,grvt_testnet_exit_stream_factory=stream_factory,
    grvt_testnet_bracket_clock_ns=lambda:venue.clock_ns()))
register_ui(app)

@app.middleware('http')
async def isolated(request,call_next):
    if not request.url.path.startswith(('/ui','/real','/__test')):
        return JSONResponse({'detail':'Fuera de la prueba aislada'},status_code=404)
    return await call_next(request)

@app.get('/__test/state')
def state():
    return {'source':venue.source,'bulk_sends':venue.bulk_sends,'cancel_sends':venue.cancel_sends,
        'close_sends':venue.close_sends,'position':str(venue.position),'balance':str(venue.balance),
        'watch_connected':getattr(venue,'watch_connected',False),'watch_closed':getattr(venue,'watch_closed',False)}

@app.post('/__test/watch-trigger')
def watch_trigger():
    venue.watch_mark='108.1'; return {'triggered':True}

@app.post('/__test/expire-protections')
def expire_protections():
    assert os.environ.get('ORBIS_TEST_NATIVE_RECOVERY')=='EXPIRED'
    expiration=int(venue.recorded['contracts'][1]['signature']['expiration'])
    venue.advance_bot_clock((expiration-venue.clock_ns())//1_000_000_000+2)
    return {'expired':True}

@app.post('/__test/trigger')
def trigger():
    venue.trigger(fill_size='0.2' if os.environ.get('ORBIS_TEST_NATIVE_PARTIAL')=='1' else None)
    return {'triggered':True}

@app.post('/__test/lock-sources')
def lock_sources():
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    paper_perp_access.lock(venue.origin.paper.path); lab_access.lock(venue.origin.lab.path)
    return {'locked':True}

@app.post('/__test/close-source')
def close_source():
    from decimal import Decimal
    assert venue.origin.observe_risk_mark(venue.source['session_id'],Decimal('110'))=='EXECUTED'
    return {'closed':True}

@app.post('/__test/bot-signal')
def bot_signal():
    import time
    assert os.environ.get('ORBIS_TEST_AUTOMATIC_BOT')=='1'
    venue.origin.clock['now']=venue.clock_ns()//1_000_000_000; venue.origin.monitor.clock_ns=venue.clock_ns
    venue.origin.monitor.poll(venue.source['session_id'])
    return {'observed':True}

@app.get('/__test/bot-records')
def bot_records():
    assert os.environ.get('ORBIS_TEST_AUTOMATIC_BOT')=='1'
    from app.live_execution.grvt_bot_archive import bot_run_history
    return {'items':[{'id':row['id'],'status':row['status'],'terminal':row['terminal'] is not None,
        'entry_count':row.get('entry_count',1),'completed_entries':row.get('completed_entries',1)}
        for row in bot_run_history(venue.vault,venue.cap.sub_account_id)['items']]}

@app.post('/__test/bot-next')
def bot_next():
    assert os.environ.get('ORBIS_TEST_BOUNDED_BOT')=='1'
    from tests.test_grvt_bot_continuous import next_paper_open
    next_paper_open(venue,venue.source)
    return {'observed':True}

@app.post('/__test/market-reduction')
def market_reduction():
    from copy import deepcopy
    from decimal import Decimal

    assert os.environ.get('ORBIS_TEST_GRVT_MARKET_SAMPLE')=='1'
    parent,stop,_=(venue.states[order['metadata']['client_order_id']] for order in venue.recorded['contracts'])
    cid=str(2**63+8101)
    assert cid not in venue.states and venue.position==Decimal('0.5')
    stamp=venue.clock_ns()
    reduction=deepcopy(parent)
    reduction.update(order_id='0xexternaltestioc',time_in_force='IMMEDIATE_OR_CANCEL',reduce_only=True,
        metadata={'client_order_id':cid,'create_time':str(stamp)},
        state={'status':'CANCELLED','traded_size':['0.1'],'book_size':['0'],'update_time':str(stamp),'reject_reason':'IOC_CANCEL'})
    reduction['legs'][0].update(size='0.2',is_buying_asset=False)
    stop['state'].update(book_size=['0.4'],update_time=str(stamp))
    venue.states[cid]=reduction
    venue.add_fill(reduction,'0.1',stamp)
    return {'order_id':reduction['order_id']}


@app.post('/__test/market-native-stop')
def market_native_stop():
    assert os.environ.get('ORBIS_TEST_GRVT_MARKET_SAMPLE')=='1'
    assert str(venue.position)=='0.4'
    venue.trigger('STOP_LOSS',fill_size='0.4')
    stop=venue.states[venue.recorded['contracts'][1]['metadata']['client_order_id']]
    stop['state'].update(status='CANCELLED',book_size=['0'],reject_reason='REDUCE_ONLY_LIMIT')
    return {'triggered':True}
