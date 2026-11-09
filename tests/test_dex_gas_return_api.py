"""Protected native return API, actual own EVM sends and lost response recovery."""
from uuid import uuid4
from datetime import timedelta
from eth_account import Account
from tests.test_wallet_funding_api import prove

from app.live_execution.dex_gas_return_transport import BnbGasReturnSender
from tests.test_dex_bot_run_api import http_case
from tests.test_dex_deployments import PASSWORD


def test_return_api_checks_identity_confirms_one_native_send_and_recovers_after_vault_reopen(tmp_path,monkeypatch):
    posts=[]
    def sender(plan):
        actual=BnbGasReturnSender(plan,clock=lambda:plan.snapshot.observed_at);original=actual.send
        def send(raw):posts.append(original(raw));return posts[-1]
        actual.send=send;return actual
    with http_case(tmp_path,monkeypatch,return_sender_factory=sender) as (client,headers,runs,body,values,clock,mine,hashes,controls):
        vault,account,manager,draft,meta,rpc,_,binding=values
        base=runs.rsplit('/dex-bots/runs',1)[0]+'/dex-gas-returns'
        review={'request_id':str(uuid4()),'binding_id':binding['id'],'draft_id':body['draft_id'],'address':meta['owner'],
            'gas_price':'1000000000','max_gas_bnb':'0.000021'}
        assert client.post(base,json=review).status_code==401
        assert client.get(base).status_code==401
        assert client.post(base,headers={**headers,'Origin':'https://outside.invalid'},json=review).status_code==403
        assert client.post(base,headers=headers,json={**review,'recipient':meta['owner']}).status_code==422
        response=client.post(base,headers=headers,json=review);assert response.status_code==200,response.text
        prepared=response.json();assert prepared['status']=='PREPARED' and not posts
        assert client.post(base,headers=headers,json=review).json()['id']==prepared['id']
        url=base+'/'+prepared['id'];submit={'address':meta['owner'],'draft_id':body['draft_id'],
            'plan_digest':prepared['plan_digest'],'confirmation':'DEVOLVER_GAS_OPERADORA_DEX'}
        assert client.post(url+'/submit',json=submit).status_code==401
        assert client.post(url+'/submit',headers=headers,json={**submit,'plan_digest':'ab'*32}).status_code==409
        result=client.post(url+'/submit',headers=headers,json=submit);assert result.status_code==200,result.text
        confirmed=result.json();assert confirmed['status']=='FINALIZED' and len(posts)==1 and not hashes
        assert confirmed['receipt']['transferred_wei']==prepared['amount_wei']
        assert confirmed['receipt']['gas_fee_wei']=='21000000000000' and not confirmed['automatic_execution']
        assert client.post(url+'/submit',headers=headers,json=submit).json()['status']=='FINALIZED' and len(posts)==1
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        restored=client.get(url,headers=headers);assert restored.status_code==200,restored.text
        assert restored.json()['receipt']==confirmed['receipt'] and len(posts)==1
        assert client.get(base,headers=headers).json()[0]['id']==prepared['id']


def test_return_api_preserves_unknown_post_and_observes_only_its_original_hash(tmp_path,monkeypatch):
    posts=[]
    def sender(plan):
        actual=BnbGasReturnSender(plan,clock=lambda:plan.snapshot.observed_at);original=actual.send
        def send(raw):posts.append(original(raw));raise TimeoutError('Controlled response loss')
        actual.send=send;return actual
    with http_case(tmp_path,monkeypatch,return_sender_factory=sender) as (client,headers,runs,body,values,clock,mine,hashes,controls):
        _,account,_,_,meta,rpc,_,binding=values
        base=runs.rsplit('/dex-bots/runs',1)[0]+'/dex-gas-returns'
        review={'request_id':str(uuid4()),'binding_id':binding['id'],'draft_id':body['draft_id'],'address':meta['owner'],
            'gas_price':'1000000000','max_gas_bnb':'0.000021'}
        response=client.post(base,headers=headers,json=review);assert response.status_code==200,response.text
        prepared=response.json();url=base+'/'+prepared['id'];submit={'address':meta['owner'],'draft_id':body['draft_id'],
            'plan_digest':prepared['plan_digest'],'confirmation':'DEVOLVER_GAS_OPERADORA_DEX'}
        # Prevent immediate observation in this fixture, while the dedicated
        # sender still executes the real local transaction exactly once.
        controls['receipts']=False
        result=client.post(url+'/submit',headers=headers,json=submit);assert result.status_code==200,result.text
        assert result.json()['status']=='BROADCAST_ATTEMPTED' and result.json()['tx_hash']==posts[0] and len(posts)==1
        assert client.post(url+'/submit',headers=headers,json=submit).status_code==200 and len(posts)==1
        assert client.post(url+'/cancel',headers=headers,json={'address':meta['owner'],'confirmation':'CANCELAR_REVISION_DEVOLUCION_GAS'}).status_code==409
        assert client.post(url+'/abandon',headers=headers,json={'address':meta['owner'],'confirmation':'DESCARTAR_DEVOLUCION_NO_EMITIDA'}).status_code==409
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.post(url+'/observe',headers=headers,json={'address':meta['owner'],'tx_hash':posts[0]}).status_code==409
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        assert client.post(url+'/observe',headers=headers,json={'address':meta['owner'],'tx_hash':'0x'+'ab'*32}).status_code==409
        controls['receipts']=True
        observed=client.post(url+'/observe',headers=headers,json={'address':meta['owner'],'tx_hash':posts[0]})
        assert observed.status_code==200 and observed.json()['status']=='FINALIZED',observed.text
        assert len(posts)==1 and not hashes


def test_lock_during_native_submit_read_never_late_signs_or_posts(tmp_path,monkeypatch):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    entered,release=Event(),Event();gate={'enabled':False};posts=[]
    def before_read():
        if gate['enabled']:entered.set();assert release.wait(10)
    def sender(plan):
        actual=BnbGasReturnSender(plan,clock=lambda:plan.snapshot.observed_at);original=actual.send
        def send(raw):posts.append(original(raw));return posts[-1]
        actual.send=send;return actual
    with http_case(tmp_path,monkeypatch,before_read=before_read,return_sender_factory=sender) as (client,headers,runs,body,values,clock,mine,hashes,controls):
        _,_,_,_,meta,_,_,binding=values;base=runs.rsplit('/dex-bots/runs',1)[0]+'/dex-gas-returns'
        review={'request_id':str(uuid4()),'binding_id':binding['id'],'draft_id':body['draft_id'],'address':meta['owner'],
            'gas_price':'1000000000','max_gas_bnb':'0.000021'}
        prepared=client.post(base,headers=headers,json=review);assert prepared.status_code==200,prepared.text
        row=prepared.json();url=base+'/'+row['id']
        submit={'address':meta['owner'],'draft_id':body['draft_id'],'plan_digest':row['plan_digest'],'confirmation':'DEVOLVER_GAS_OPERADORA_DEX'}
        gate['enabled']=True
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(client.post,url+'/submit',headers=headers,json=submit)
            try:
                assert entered.wait(5)
                assert client.post('/real/vault/lock',headers=headers).status_code==200
            finally:release.set()
            assert pending.result(15).status_code==401
        assert not posts and not hashes
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.get(url,headers=headers).json()['status']=='PREPARED'
