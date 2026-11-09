"""Explicit DEX rearm keeps actual fills and budgets in an owned local EVM."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest

from tests.test_dex_bot_run_api import http_case,eventually
from tests.test_dex_deployments import PASSWORD
from tests.test_dex_operator import write


def test_rearm_retains_open_position_trailing_and_gas_then_closes_without_buying_twice(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,clock,mine,hashes,controls):
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        original=body['request_id']
        def gas_budget():
            response=client.get(base.rsplit('/runs',1)[0]+'/bindings',headers=headers)
            assert response.status_code==200,response.text
            return response.json()[0]['gas']
        initial_gas=gas_budget()
        def detail(identifier):
            response=client.get(base+'/'+identifier,headers=headers);assert response.status_code==200,response.text
            return response.json()
        clock[0]+=timedelta(seconds=1);mine()
        bought=eventually(lambda:detail(original),lambda row:int(row['position']['owned_wbnb'])>0 and row['head']['cursor']['entry_candle_t'] is not None)
        market=controls['market'];market.bid,market.ask='1.05','1.06'
        clock[0]+=timedelta(seconds=1);mine()
        eventually(lambda:detail(original),lambda row:row['head']['cursor']['trailing_high']=='1.05')
        assert client.post(base+'/'+original+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        stopped=eventually(lambda:detail(original),lambda row:not row['worker']['running'])
        consumed_gas=gas_budget();assert int(consumed_gas['gas_spent_wei'])>0
        assert int(consumed_gas['remaining_gas_wei'])<int(initial_gas['remaining_gas_wei'])
        assert len(hashes)==1
        clock[0]+=timedelta(seconds=1);mine()
        request={**body,'request_id':str(uuid4()),'permission_mode':'SESSION','confirmation':'REARMAR_BOT_DEX'}
        assert client.post(base+'/'+original+'/rearm',json=request).status_code==401
        assert client.post(base+'/'+original+'/rearm',headers={**headers,'Origin':'https://outside.invalid'},json=request).status_code==403
        resumed=client.post(base+'/'+original+'/rearm',headers=headers,json=request)
        assert resumed.status_code==200,resumed.text
        current=resumed.json();assert current['engine']=='orbis.dex.bot.long.v3' and current['execution_enabled']
        assert current['previous_run_id']==original and current['position']==stopped['position']
        assert gas_budget()==consumed_gas
        assert current['head']['cursor']['entry_candle_t']==stopped['head']['cursor']['entry_candle_t']
        assert Decimal(current['head']['cursor']['trailing_high'])>=Decimal(stopped['head']['cursor']['trailing_high'])
        replay=client.post(base+'/'+original+'/rearm',headers=headers,json=request)
        assert replay.status_code==200 and replay.json()['id']==current['id'] and len(hashes)==1
        assert client.post(base,headers=headers,json={**request,'confirmation':'ACTIVAR_BOT_DEX'}).status_code==409
        assert client.post(base+'/'+original+'/rearm',headers=headers,json={**request,'ttl_seconds':899}).status_code==409
        meta,rpc=values[4:6]
        write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[9950,False])
        market.bid,market.ask='1.2','1.21';clock[0]+=timedelta(seconds=1);mine()
        closed=eventually(lambda:detail(current['id']),lambda row:row['position']['owned_wbnb']=='0' and len(hashes)==2)
        assert closed['position']['buy_spent']==stopped['position']['buy_spent']
        assert closed['position']['entries']==stopped['position']['entries']
        assert int(closed['position']['realized_loss'])>0
        assert len(detail(original)['intents'])==1 and len(closed['intents'])==1
        def fills(identifier):
            response=client.get(base+'/'+identifier+'/executions-page',headers=headers)
            assert response.status_code==200,response.text
            assert response.headers['cache-control']=='no-store'
            return response.json()
        prefix,continuation=fills(original),fills(current['id'])
        assert prefix['total']==1 and continuation['total']==2
        assert continuation['family_run_ids']==[original,current['id']]
        assert continuation['executions'][0]==prefix['executions'][0]
        assert [fill['side'] for fill in continuation['executions']]==['BUY','SELL']
        assert len({fill['fill_id'] for fill in continuation['executions']})==2

        assert client.post(base+'/'+current['id']+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:detail(current['id']),lambda row:not row['worker']['running'])
        clock[0]+=timedelta(seconds=1);mine()
        third_request={**request,'request_id':str(uuid4())}
        third=client.post(base+'/'+current['id']+'/rearm',headers=headers,json=third_request)
        assert third.status_code==200,third.text
        assert third.json()['position']==closed['position'] and len(hashes)==2
        backup=client.post('/real/vault/backup',headers=headers)
        assert backup.status_code==200,backup.text
        stale=client.post(base+'/'+original+'/rearm',headers=headers,json={**request,'request_id':str(uuid4())})
        assert stale.status_code==409 and len(hashes)==2
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        recovered=detail(third.json()['id'])
        assert not recovered['execution_enabled'] and recovered['position']==closed['position'] and len(hashes)==2
        assert detail(original)['position']==stopped['position']


def test_an_uncertain_old_execution_blocks_rearm_without_replacing_its_permission(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch,lost_response=True) as (client,headers,base,body,values,clock,mine,hashes,controls):
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        clock[0]+=timedelta(seconds=1);mine()
        original=body['request_id']
        eventually(lambda:client.get(base+'/'+original,headers=headers).json(),
            lambda row:row.get('reason')=='OUTCOME_UNKNOWN' and row['worker']['pending_receipt'])
        clock[0]+=timedelta(seconds=1);mine()
        request={**body,'request_id':str(uuid4()),'permission_mode':'SESSION','confirmation':'REARMAR_BOT_DEX'}
        denied=client.post(base+'/'+original+'/rearm',headers=headers,json=request)
        assert denied.status_code==409 and 'pendiente' in denied.text,denied.text
        assert len(client.get(base,headers=headers).json())==1 and len(hashes)==1


def test_stop_during_rearm_review_prevents_a_late_permission_or_worker(tmp_path,monkeypatch):
    entered,release=Event(),Event();gate={'enabled':False}
    def before_read():
        if gate['enabled']:entered.set();assert release.wait(10)
    with http_case(tmp_path,monkeypatch,before_read=before_read) as (client,headers,base,body,values,clock,mine,hashes,controls):
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        original=body['request_id']
        assert client.post(base+'/'+original+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:client.get(base+'/'+original,headers=headers).json(),lambda row:not row['worker']['running'])
        clock[0]+=timedelta(seconds=1);mine();gate['enabled']=True
        request={**body,'request_id':str(uuid4()),'permission_mode':'SESSION','confirmation':'REARMAR_BOT_DEX'}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(client.post,base+'/'+original+'/rearm',headers=headers,json=request)
            try:
                assert entered.wait(5)
                stopped=client.post(base+'/'+request['request_id']+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'})
                assert stopped.status_code==200 and stopped.json()['runtime_status']=='CANCELLED'
            finally:release.set()
            completed=pending.result(15)
        assert completed.status_code==409,completed.text
        assert len(client.get(base,headers=headers).json())==1 and not hashes


@pytest.mark.parametrize('forgery',['trailing','spent'])
def test_rehashed_rearm_baseline_or_trailing_cannot_replace_the_verified_predecessor(tmp_path,monkeypatch,forgery):
    from app.portfolio.models import RealDexBotRun
    from app.portfolio.wallet_funding import _decoded,_encoded
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,clock,mine,hashes,controls):
        assert client.post(base,headers=headers,json=body).status_code==200
        original=body['request_id'];clock[0]+=timedelta(seconds=1);mine()
        eventually(lambda:client.get(base+'/'+original,headers=headers).json(),lambda row:int(row['position']['owned_wbnb'])>0)
        assert client.post(base+'/'+original+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:client.get(base+'/'+original,headers=headers).json(),lambda row:not row['worker']['running'])
        clock[0]+=timedelta(seconds=1);mine()
        request={**body,'request_id':str(uuid4()),'permission_mode':'SESSION','confirmation':'REARMAR_BOT_DEX'}
        resumed=client.post(base+'/'+original+'/rearm',headers=headers,json=request)
        assert resumed.status_code==200,resumed.text
        identifier=resumed.json()['id']
        assert client.post(base+'/'+identifier+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        vault=values[0];vault.unlock(PASSWORD)
        try:
            with vault.session() as db:
                row=db.get(RealDexBotRun,identifier);terms=_decoded(row.payload_json,row.payload_digest)
                state=_decoded(row.state_json,row.state_digest)
                if forgery=='trailing':
                    terms['resume']['initial_cursor']['trailing_high']='999'
                    if state['head']['sequence_no']==-1:state['head']['cursor']['trailing_high']='999'
                else:terms['baseline']['counters']['buy_spent']=str(int(terms['baseline']['counters']['buy_spent'])+1)
                row.payload_json,row.payload_digest=_encoded(terms)
                if state['head']['sequence_no']==-1:state['head']['digest']=row.payload_digest
                row.state_json,row.state_digest=_encoded(state);db.commit()
        finally:vault.lock()
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.get(base+'/'+identifier,headers=headers).status_code==409
        assert client.post('/real/vault/backup',headers=headers).status_code==409
        assert len(hashes)==1


def test_rearm_waits_for_a_stopped_worker_to_exit_without_archiving_a_failed_epoch(tmp_path,monkeypatch):
    entered,release=Event(),Event()
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,clock,mine,hashes,controls):
        market=controls['market'];original=market.get_paper_quote
        def stalled(symbol):
            entered.set();assert release.wait(15)
            return original(symbol)
        assert client.post(base,headers=headers,json=body).status_code==200
        market.get_paper_quote=stalled
        try:
            assert entered.wait(5)
            stopped=client.post(base+'/'+body['request_id']+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'})
            assert stopped.status_code==200 and stopped.json()['worker']['running'],stopped.text
            clock[0]+=timedelta(seconds=1);mine()
            request={**body,'request_id':str(uuid4()),'permission_mode':'SESSION','confirmation':'REARMAR_BOT_DEX'}
            denied=client.post(base+'/'+body['request_id']+'/rearm',headers=headers,json=request)
            assert denied.status_code==409,denied.text
            rows=client.get(base,headers=headers).json()
            assert len(rows)==1,'A stopped but still live worker must not create a failed permission epoch'
            assert not hashes
        finally:release.set()
        eventually(lambda:client.get(base+'/'+body['request_id'],headers=headers).json(),lambda row:not row['worker']['running'])
        market.get_paper_quote=original
        accepted=client.post(base+'/'+body['request_id']+'/rearm',headers=headers,json=request)
        assert accepted.status_code==200,accepted.text
        assert accepted.json()['execution_enabled'] and not hashes
