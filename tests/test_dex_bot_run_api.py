"""Real protected HTTP activation to disposable EVM fills, never mainnet."""
from contextlib import nullcontext,contextmanager
from datetime import timedelta
import json,time
from uuid import uuid4
import pytest

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.live_execution.dex_operator_transport import BnbOperatorSender
from app.portfolio.api import create_real_router
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
from tests.test_dex_deployments import PASSWORD
from tests.test_wallet_funding_api import prove


def eventually(read,condition):
    deadline=time.monotonic()+15
    while True:
        value=read()
        if condition(value):return value
        assert time.monotonic()<deadline,value
        time.sleep(.05)


def test_api_activates_once_confirms_buy_and_sell_and_recovers_stopped_without_autorun(tmp_path,monkeypatch):
    with running_case(tmp_path,activate=False,research=True) as (values,_,_,_,clock,mine,market):
        vault,account,manager,draft,meta,rpc,_,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc']);posts=[];supervisors=[]
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=clock[0])
        manager.revoke();vault.lock()
        def sender(plan):
            actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
            def send(raw):posts.append(plan.buy);return original(raw)
            actual.send=send;return actual
        def factory(v,c):
            result=DexBotSupervisor(v,c,clock=lambda:clock[0],market_factory=lambda:market,interval_seconds=.1,
                worker_kwargs={'market':market,'rpc_factory':lambda:nullcontext(rpc),'sender_factory':sender})
            supervisors.append(result);return result
        app=FastAPI();app.include_router(create_real_router(vault.path,spot_paper=market.research_source.paper,dex_rpc_factory=lambda:nullcontext(rpc),dex_bot_supervisor_factory=factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            base=f'/real/accounts/{account}/dex-bots/runs';identifier=str(uuid4());restored_id=str(uuid4())
            body={'request_id':identifier,'binding_id':binding['id'],'draft_id':restored_id,'address':meta['owner'],
                'ttl_seconds':900,'gas':400000,'gas_price':'1000000000','max_gas_bnb':'0.001',
                'slippage_bps':25,'confirmation':'ACTIVAR_BOT_DEX'}
            assert client.post(base,json=body).status_code==401
            assert client.get(base).status_code==401
            assert client.post(base,headers=headers,json=body).status_code==409 and not posts
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
            restored=client.post(f'/real/accounts/{account}/dex-operator/restore',headers=headers,json={
                'address':meta['owner'],'request_id':restored_id,'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup)})
            assert restored.status_code==200,restored.text
            assert client.post(base,headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
            assert client.post(base,headers=headers,json={**body,'claim':'untrusted'}).status_code==422
            active=client.post(base,headers=headers,json=body)
            assert active.status_code==200,active.text
            assert active.headers['cache-control']=='no-store' and active.json()['execution_enabled']
            assert active.json()['engine']=='orbis.dex.bot.long.v1' and active.json()['permission_mode']=='SESSION'
            assert not posts
            replay=client.post(base,headers=headers,json=body)
            assert replay.status_code==200 and replay.json()['id']==identifier and len(supervisors)==1
            assert client.post(base,headers=headers,json={**body,'gas':400001}).status_code==409
            clock[0]+=timedelta(seconds=1);mine()
            def detail():
                response=client.get(base+'/'+identifier,headers=headers);assert response.status_code==200,response.text
                return response.json()
            bought=eventually(detail,lambda x:int(x['position']['owned_wbnb'])>0)
            assert bought['worker']['last_receipt_status']=='FINALIZED' and posts==[True]
            market.bid,market.ask='1.2','1.21';clock[0]+=timedelta(seconds=1);mine()
            sold=eventually(detail,lambda x:int(x['position']['owned_wbnb'])==0 and posts==[True,False])
            assert sold['position']['cost_usdt']=='0'
            stopped=client.post(base+'/'+identifier+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'})
            assert stopped.status_code==200 and not stopped.json()['execution_enabled'],stopped.text
            assert client.post('/real/vault/lock',headers=headers).status_code==200
            assert client.get(base,headers=headers).status_code==401
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            recovered=detail()
            assert recovered['runtime_status']=='STOPPED' and not recovered['execution_enabled'] and posts==[True,False]
            assert client.get(base,headers=headers).json()[0]['id']==identifier
        for supervisor in supervisors:supervisor.join(timeout=5)


@contextmanager
def http_case(directory,monkeypatch,*,before_read=lambda:None,lost_response=False,ttl_seconds=900,permission_mode=None,return_sender_factory=None):
    from datetime import datetime,timezone
    policy_changes={'expires_at':int(datetime.now(timezone.utc).timestamp())+7200} if ttl_seconds>900 else None
    with running_case(directory,activate=False,research=True,draft_ttl_seconds=ttl_seconds,policy_changes=policy_changes) as (values,_,_,_,clock,mine,market):
        vault,account,manager,draft,meta,rpc,_,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc']);hashes=[];controls={'receipts':not lost_response,'market':market};supervisors=[]
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=clock[0])
        manager.revoke();vault.lock()
        def read(method,params):
            if not controls['receipts'] and method=='eth_getTransactionReceipt':return None
            return rpc(method,params)
        def factory(v,c):
            def sender(plan):
                actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
                def send(raw):
                    result=original(raw);hashes.append(result)
                    if lost_response:raise TimeoutError('controlled response loss')
                    return result
                actual.send=send;return actual
            result=DexBotSupervisor(v,c,clock=lambda:clock[0],market_factory=lambda:market,interval_seconds=.1,
                worker_kwargs={'market':market,'rpc_factory':lambda:nullcontext(read),'sender_factory':sender})
            supervisors.append(result);controls['supervisor']=result;return result
        @contextmanager
        def rpc_factory():before_read();yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,spot_paper=market.research_source.paper,dex_rpc_factory=rpc_factory,dex_bot_supervisor_factory=factory,dex_return_sender_factory=return_sender_factory))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32));draft_id=str(uuid4())
            restored=client.post(f'/real/accounts/{account}/dex-operator/restore',headers=headers,json={
                'address':meta['owner'],'request_id':draft_id,'password':BACKUP_PASSWORD,'backup_json':json.dumps(backup),'ttl_seconds':ttl_seconds})
            assert restored.status_code==200,restored.text
            body={'request_id':str(uuid4()),'binding_id':binding['id'],'draft_id':draft_id,'address':meta['owner'],
                'ttl_seconds':ttl_seconds,'gas':400000,'gas_price':'1000000000','max_gas_bnb':'0.001','slippage_bps':25,'confirmation':'ACTIVAR_BOT_DEX'}
            if permission_mode is not None:body['permission_mode']=permission_mode
            yield client,headers,f'/real/accounts/{account}/dex-bots/runs',body,values,clock,mine,hashes,controls
        for supervisor in supervisors:supervisor.join(timeout=5)


def test_activation_review_checks_stop_and_order_gas_before_connecting_signer(tmp_path,monkeypatch):
    from app.live_execution.dex_trade import STOP_GAS_LIMIT

    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,clock,mine,hashes,controls):
        _,_,_,draft,_,rpc,_,_ = values
        fields = ('binding_id','draft_id','address','gas','gas_price','max_gas_bnb','slippage_bps')
        request = {key:body[key] for key in fields}
        endpoint = base+'/activation-review'
        assert client.post(endpoint,json=request).status_code==401
        assert client.post(endpoint,headers={**headers,'Origin':'https://outside.invalid'},json=request).status_code==403
        response = client.post(endpoint,headers=headers,json=request)
        assert response.status_code==200,response.text
        review = response.json()
        assert response.headers['cache-control']=='no-store' and not review['automatic_execution']
        assert body['address'].lower() not in response.text.lower()
        assert review['wallet']['available_usdt_units']!='0' and review['wallet']['bot_position_wbnb_units']=='0'
        assert review['gas']['shortfall_wei']=='0' and review['gas']['order_wei']=='400000000000000'
        assert not client.get(base,headers=headers).json() and not hashes

        reserve=int(review['gas']['stop_reserve_wei'])
        high_price={**request,'gas':21000,'gas_price':str(reserve//STOP_GAS_LIMIT+1),'max_gas_bnb':'0.1'}
        rejected_price=client.post(endpoint,headers=headers,json=high_price)
        assert rejected_price.status_code==409 and 'precio de gas supera' in rejected_price.json()['detail']
        rpc('evm_setAccountBalance',[draft['operator_address'],hex(reserve)])
        mine()
        short=client.post(endpoint,headers=headers,json=request).json()['gas']
        assert int(short['available_wei'])==reserve and short['shortfall_wei']==short['order_wei']
        blocked=client.post(base,headers=headers,json=body)
        assert blocked.status_code==409 and 'una orden y la reserva STOP' in blocked.json()['detail'],blocked.text
        assert not client.get(base,headers=headers).json() and not hashes
        assert not controls['supervisor'].connection.session.status()['connected']

        rpc('evm_setAccountBalance',[draft['operator_address'],hex(int(short['required_wei']))])
        mine()
        active=client.post(base,headers=headers,json=body)
        assert active.status_code==200 and active.json()['execution_enabled'],active.text
        assert not hashes
        client.post(base+'/'+body['request_id']+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'})


@pytest.mark.parametrize('action',['stop','lock','preemptive'])
def test_cancelling_before_or_during_activation_reads_never_late_starts_a_worker(tmp_path,monkeypatch,action):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    entered,release=Event(),Event()
    def before_read():entered.set();assert release.wait(10)
    with http_case(tmp_path,monkeypatch,before_read=before_read) as (client,headers,base,body,values,clock,mine,hashes,controls):
        url=base+'/'+body['request_id']+'/stop'
        if action=='preemptive':
            cancelled=client.post(url,headers=headers,json={'confirmation':'DETENER_BOT_DEX'})
            assert cancelled.status_code==200 and cancelled.json()['runtime_status']=='CANCELLED'
            denied=client.post(base,headers=headers,json=body)
            assert denied.status_code==409 and not entered.is_set() and not hashes
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result=pool.submit(client.post,base,headers=headers,json=body)
                try:
                    assert entered.wait(5)
                    if action=='stop':
                        stopped=client.post(url,headers=headers,json={'confirmation':'DETENER_BOT_DEX'})
                        assert stopped.status_code==200 and stopped.json()['runtime_status']=='CANCELLED'
                    else:assert client.post('/real/vault/lock',headers=headers).status_code==200
                finally:release.set()
                completed=result.result(15)
                assert completed.status_code==(409 if action=='stop' else 401),completed.text
        if action=='lock':headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.get(base,headers=headers).json()==[] and not hashes


def test_original_receipt_can_be_recovered_after_lost_response_and_vault_reopen_without_signing(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch,lost_response=True) as (client,headers,base,body,values,clock,mine,hashes,controls):
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        clock[0]+=timedelta(seconds=1);mine()
        def detail():return client.get(base+'/'+body['request_id'],headers=headers).json()
        unresolved=eventually(detail,lambda x:bool(x['intents']) and x['reason']=='OUTCOME_UNKNOWN')
        assert len(hashes)==1 and unresolved['position']['owned_wbnb']=='0' and unresolved['worker']['pending_receipt']
        intent=unresolved['intents'][0]['id']
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        controls['receipts']=True;url=base+'/'+body['request_id']+'/intents/'+intent+'/observe'
        request={'address':values[4]['owner'],'tx_hash':hashes[0]}
        assert client.post(url,headers=headers,json=request).status_code==409
        prove(client,headers,values[1],Account.from_key('0x'+'11'*32))
        assert client.post(url,headers=headers,json={**request,'tx_hash':'0x'+'ab'*32}).status_code==409
        confirmed=client.post(url,headers=headers,json=request)
        assert confirmed.status_code==200,confirmed.text
        assert confirmed.json()['intents'][0]['status']=='FINALIZED' and int(confirmed.json()['position']['owned_wbnb'])>0
        assert not confirmed.json()['worker']['pending_receipt']
        assert not confirmed.json()['execution_enabled'] and len(hashes)==1
        assert client.post(url,headers=headers,json=request).json()['intents'][0]['status']=='FINALIZED'


def test_explicit_long_permission_survives_protecting_and_reauthenticating_view_but_not_lock(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch,ttl_seconds=3600,permission_mode='OPERATIONAL') as (client,headers,base,body,values,clock,mine,hashes,controls):
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        until=active.json()['expires_at']
        assert active.json()['permission_mode']=='OPERATIONAL'
        protected=client.post('/real/vault/protect-view',headers=headers)
        assert protected.status_code==200 and protected.json()['operating'],protected.text
        assert client.get(base,headers=headers).status_code==401
        status=client.get('/real/vault/status').json()
        assert status['unlocked'] and status['view_protected'] and status['operating']['until']==until
        clock[0]+=timedelta(seconds=1);mine()
        eventually(lambda:len(hashes),lambda n:n==1)
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        row=client.get(base+'/'+body['request_id'],headers=headers).json()
        assert row['execution_enabled'] and row['expires_at']==until
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        assert not client.get('/real/vault/status').json()['unlocked'] and len(hashes)==1


def test_operational_expiry_closes_an_unattended_vault_without_renewing_permission(tmp_path,monkeypatch):
    from datetime import datetime
    with http_case(tmp_path,monkeypatch,ttl_seconds=3600,permission_mode='OPERATIONAL') as (client,headers,base,body,values,clock,mine,hashes,controls):
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        assert active.json()['engine']=='orbis.dex.bot.long.v2'
        assert client.post('/real/vault/protect-view',headers=headers).json()['operating']
        clock[0]=datetime.fromisoformat(active.json()['expires_at'])+timedelta(seconds=1)
        closed=eventually(lambda:client.get('/real/vault/status').json(),lambda state:not state['unlocked'])
        assert not closed.get('operating',{}).get('active',False) and not hashes


def test_extended_foreground_permission_still_stops_when_the_view_is_protected(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch,ttl_seconds=3600,permission_mode='SESSION') as (client,headers,base,body,values,clock,mine,hashes,controls):
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        assert active.json()['engine']=='orbis.dex.bot.long.v2' and active.json()['permission_mode']=='SESSION'
        protected=client.post('/real/vault/protect-view',headers=headers)
        assert protected.status_code==200 and not protected.json()['operating']
        assert not client.get('/real/vault/status').json()['unlocked'] and not hashes


def test_a_legacy_activation_cannot_silently_gain_a_long_or_background_permission(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,clock,mine,hashes,controls):
        denied=client.post(base,headers=headers,json={**body,'ttl_seconds':901})
        assert denied.status_code==409 and 'expresamente' in denied.json()['detail'] and not hashes
        active=client.post(base,headers=headers,json=body);assert active.status_code==200,active.text
        changed=client.post(base,headers=headers,json={**body,'permission_mode':'OPERATIONAL'})
        assert changed.status_code==409 and active.json()['permission_mode']=='SESSION'
        assert client.post('/real/vault/protect-view',headers=headers).json()['operating'] is False
