"""Protected HTTP DCA lifecycle reaches actual fills only in our own EVM."""
from contextlib import contextmanager,nullcontext
from datetime import timedelta
import json
import pytest
from uuid import uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.live_execution.dex_operator_transport import BnbOperatorSender
from app.portfolio.api import create_real_router
from tests.dex_dca_worker_fixture import dca_running_case
from tests.test_dex_bot_run_api import eventually
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
from tests.test_dex_deployments import PASSWORD
from tests.test_wallet_funding_api import prove


@contextmanager
def dca_http_case(directory,monkeypatch,*,before_read=lambda:None,before_send=lambda plan:None,volume=False):
    with dca_running_case(directory,activate=False,research=True,volume=volume) as case:
        values,_,_,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,_,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc']);posts=[];supervisors=[]
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=clock[0])
        connection.revoke();manager.revoke();vault.lock()
        def factory(v,c):
            def sender(plan):
                actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
                def send(raw):
                    before_send(plan)
                    result=original(raw);posts.append(result);return result
                actual.send=send;return actual
            supervisor=DexBotSupervisor(v,c,clock=lambda:clock[0],market_factory=lambda:market,interval_seconds=.1,
                worker_kwargs={'market':market,'rpc_factory':lambda:nullcontext(rpc),'sender_factory':sender})
            supervisors.append(supervisor);return supervisor
        @contextmanager
        def rpc_factory():before_read();yield rpc
        app=FastAPI();app.include_router(create_real_router(vault.path,dca_paper=market.research_source.paper,dex_rpc_factory=rpc_factory,dex_bot_supervisor_factory=factory))
        try:
            with TestClient(app) as client:
                headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
                prove(client,headers,account,Account.from_key('0x'+'11'*32));draft_id=str(uuid4())
                restored=client.post(f'/real/accounts/{account}/dex-operator/restore',headers=headers,json={
                    'address':meta['owner'],'request_id':draft_id,'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup),'ttl_seconds':86400})
                assert restored.status_code==200,restored.text
                body={'request_id':str(uuid4()),'binding_id':binding['id'],'draft_id':draft_id,'address':meta['owner'],
                    'ttl_seconds':900,'permission_mode':'SESSION','gas':400000,'gas_price':'1000000000',
                    'max_gas_bnb':'0.001','slippage_bps':25,'dca_activation_window_seconds':900,'confirmation':'ACTIVAR_BOT_DEX'}
                yield client,headers,f'/real/accounts/{account}/dex-bots/runs',body,case,posts
        finally:
            for supervisor in supervisors:supervisor.shutdown();supervisor.join(timeout=5)


def test_http_dca_activation_is_idempotent_then_rearm_preserves_base_dip_budget_and_reopen(tmp_path,monkeypatch):
    with dca_http_case(tmp_path,monkeypatch) as (client,headers,base,body,case,posts):
        values,_,_,_,clock,mine,market=case
        assert client.post(base,json=body).status_code==401
        assert client.post(base,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        assert client.post(base,headers=headers,json={**body,'dca_activation_window_seconds':59}).status_code==422
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        original=body['request_id'];assert active.json()['source_kind']=='DCA' and active.json()['execution_enabled']
        assert active.json()['dca']['limits']['activation_window_seconds']==900 and not posts
        assert client.post(base,headers=headers,json=body).status_code==200
        assert client.post(base,headers=headers,json={**body,'dca_activation_window_seconds':901}).status_code==409
        def detail(identifier):
            response=client.get(base+'/'+identifier,headers=headers);assert response.status_code==200,response.text
            assert response.headers['cache-control']=='no-store';return response.json()
        clock[0]+=timedelta(seconds=1);mine()
        bought=eventually(lambda:detail(original),lambda row:row['dca']['spent_units']==str(10**16))
        assert len(posts)==1 and bought['dca']['consumed_keys']==['BASE:'+clock[0].strftime('%Y-%m')]
        assert client.post(base+'/'+original+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:detail(original),lambda row:not row['worker']['running'])
        clock[0]+=timedelta(seconds=1);mine()
        rearm={**body,'request_id':str(uuid4()),'confirmation':'REARMAR_BOT_DEX'}
        denied=client.post(base+'/'+original+'/rearm',headers=headers,json={**rearm,'dca_activation_window_seconds':901})
        assert denied.status_code==409 and len(posts)==1
        current=client.post(base+'/'+original+'/rearm',headers=headers,json=rearm);assert current.status_code==200,current.text
        identifier=current.json()['id'];assert current.json()['previous_run_id']==original
        assert current.json()['dca']['spent_units']==str(10**16)
        assert client.post(base+'/'+original+'/rearm',headers=headers,json=rearm).status_code==200
        for _ in range(7):
            clock[0]+=timedelta(seconds=10);mine()
            # Wait for the current observation before advancing the controlled clock.
            eventually(lambda:detail(identifier),lambda row:row['head']['cursor']['last_checked_at']==clock[0].isoformat())
            if len(posts)==2:break
        settled=eventually(lambda:detail(identifier),lambda row:row['dca']['spent_units']==str(2*10**16))
        assert len(posts)==2 and len(settled['dca']['consumed_keys'])==2
        assert settled['dca']['monthly_spent_units']=={clock[0].strftime('%Y-%m'):str(2*10**16)}
        assert client.post(base+'/'+identifier+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:detail(identifier),lambda row:not row['worker']['running'])
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        recovered=detail(identifier)
        assert recovered['dca']==settled['dca'] and not recovered['execution_enabled'] and len(posts)==2


@pytest.mark.parametrize('revised',[False,True],ids=['below-threshold','revised-reference'])
def test_dex_volume_worker_never_sends_without_a_verified_match(tmp_path,monkeypatch,revised):
    with dca_http_case(tmp_path,monkeypatch,volume=True) as (client,headers,base,body,case,posts):
        _,_,_,_,clock,mine,market=case
        active=client.post(base,headers=headers,json=body)
        assert active.status_code==200,active.text
        assert active.json()['research_qualification']['evidence']['paper']['simulation_engine']=='dca-1.9'
        assert not posts
        if revised:
            market.rows[-4]['v']='9'
        else:
            market.rows[-1]['v']='1'
        clock[0]+=timedelta(seconds=1);mine()
        def detail():
            response=client.get(base+'/'+body['request_id'],headers=headers)
            assert response.status_code==200,response.text
            return response.json()
        if revised:
            row=eventually(detail,lambda result:result['stored_status']=='STOPPED')
            assert row['reason']=='DATA_INVALID' and not row['execution_enabled']
        else:
            row=eventually(detail,lambda result:result['dca'].get('last_volume') is not None)
            assert row['dca']['last_volume']['reason']=='DCA_VOLUME_BELOW_MINIMUM'
            assert row['dca']['last_volume']['signal']=={'volume_period':3,'volume_min_ratio':'2','volume_ratio':'0.1'}
            assert row['execution_enabled'] and row['dca']['consumed_keys']==[]
        assert ('BNB/USDT',market.rows[-4]['t'],4) in market.requests
        assert not posts and row['dca']['spent_units']=='0'


def test_cancellation_during_dca_public_read_never_late_starts_or_sends(tmp_path,monkeypatch):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    entered,release=Event(),Event()
    def before_read():entered.set();assert release.wait(10)
    with dca_http_case(tmp_path,monkeypatch,before_read=before_read) as (client,headers,base,body,case,posts):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(client.post,base,headers=headers,json=body)
            try:
                assert entered.wait(5)
                stopped=client.post(base+'/'+body['request_id']+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'})
                assert stopped.status_code==200 and stopped.json()['runtime_status']=='CANCELLED'
            finally:release.set()
            completed=pending.result(15)
        assert completed.status_code==409 and not posts
        assert client.get(base,headers=headers).json()==[]


@pytest.mark.parametrize('mine_on_rearm',[False,True])
def test_dca_rearm_preserves_receipt_with_verified_chain_clock_ahead_of_local_clock(tmp_path,monkeypatch,mine_on_rearm):
    """Receipt finality may lead local time by <=15s; a rearm must keep it."""
    from datetime import datetime,timezone
    holder={}
    def ahead(plan):
        rpc,clock=holder['rpc'],holder['clock']
        rpc('evm_setTime',[int((clock[0]+timedelta(seconds=5)).timestamp()*1000)])
        rpc('evm_mine',[])
    with dca_http_case(tmp_path,monkeypatch,before_send=ahead) as (client,headers,base,body,case,posts):
        values,_,_,_,clock,mine,_=case;rpc=values[5];holder.update(rpc=rpc,clock=clock)
        response=client.post(base,headers=headers,json=body);assert response.status_code==200,response.text
        original=response.json()['id'];clock[0]+=timedelta(seconds=1);mine()
        def detail(identifier):
            response=client.get(base+'/'+identifier,headers=headers);assert response.status_code==200,response.text
            return response.json()
        bought=eventually(lambda:detail(original),lambda row:row['dca']['spent_units']==str(10**16))
        assert len(posts)==1
        receipt=rpc('eth_getTransactionReceipt',[posts[0]])
        block=rpc('eth_getBlockByNumber',[receipt['blockNumber'],False])
        chain_at=datetime.fromtimestamp(int(block['timestamp'],16),timezone.utc)
        assert 0<(chain_at-clock[0]).total_seconds()<=15
        assert client.post(base+'/'+original+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:detail(original),lambda row:not row['worker']['running'])
        # A fresh block must preserve the already finalized receipt's time,
        # even when the controlled local clock still lags behind it.
        clock[0]+=timedelta(seconds=1)
        assert chain_at>clock[0]
        if mine_on_rearm:mine()
        request={**body,'request_id':str(uuid4()),'confirmation':'REARMAR_BOT_DEX'}
        response=client.post(base+'/'+original+'/rearm',headers=headers,json=request)
        assert response.status_code==200,response.text
        identifier=response.json()['id'];current=detail(identifier)
        assert current['dca']['spent_units']==bought['dca']['spent_units']
        assert current['dca']['consumed_keys']==bought['dca']['consumed_keys'] and len(posts)==1
        assert client.post(base+'/'+identifier+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:detail(identifier),lambda row:not row['worker']['running'])
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        restored=detail(identifier)
        assert restored['dca']['spent_units']==str(10**16) and not restored['execution_enabled'] and len(posts)==1
