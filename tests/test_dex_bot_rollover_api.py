"""Owner-confirmed renewal → explicit resumed worker in our disposable EVM."""
from datetime import timedelta
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from tests.test_dex_bot_run_api import http_case,eventually


def test_owner_renewal_api_preserves_limits_and_requires_explicit_resumption(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,clock,mine,hashes,state):
        assert client.post(base,headers=headers,json=body).status_code==200
        previous=body['request_id'];meta,rpc=values[4:6]
        def detail():
            response=client.get(base+'/'+previous,headers=headers);assert response.status_code==200,response.text
            return response.json()
        clock[0]+=timedelta(seconds=1);mine()
        bought=eventually(detail,lambda row:int(row['position']['owned_wbnb'])>0)
        assert client.post(base+'/'+previous+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(detail,lambda row:not row['worker']['running'])
        clock[0]+=timedelta(seconds=1);mine()
        renewal=base.rsplit('/runs',1)[0]+'/renewals'
        request={'request_id':str(uuid4()),'previous_run_id':previous,'draft_id':body['draft_id'],'address':body['address'],
            'expires_at':int(clock[0].timestamp())+3600,'gas':'1000000','gas_price':'1000000000','max_gas_bnb':'0.002'}
        assert client.post(renewal,json=request).status_code==401
        assert client.post(renewal,headers={**headers,'Origin':'https://outside.invalid'},json=request).status_code==403
        assert client.post(renewal,headers=headers,json={**request,'limits':{}}).status_code==422
        response=client.post(renewal,headers=headers,json=request)
        assert response.status_code==200,response.text
        plan=response.json();assert response.headers['cache-control']=='no-store'
        assert plan['rollover']['consumed']['buy_spent']==bought['position']['buy_spent'] and len(hashes)==1
        replay=client.post(renewal,headers=headers,json=request)
        assert replay.status_code==200 and replay.json()['plan_digest']==plan['plan_digest']
        assert client.post(renewal,headers=headers,json={**request,'expires_at':request['expires_at']+1}).status_code==409
        apply_url=renewal+'/'+plan['id']+'/apply';apply={'address':body['address'],'confirmation':'APLICAR_RENOVACION_BOT_DEX'}
        assert client.post(apply_url,headers=headers,json=apply).status_code==409
        controls=base.rsplit('/dex-bots/',1)[0]+'/dex-controls/'+plan['id']
        claim={'address':body['address'],'operator':plan['transaction'].get('operator',None),
            'plan_digest':plan['plan_digest'],'confirmation':'CONFIGURAR_EJECUTOR_DEX'}
        # Only the disposable operator address is used; the API resolves it
        # from the verified draft, never accepts arbitrary signing authority.
        operator=client.post(base.rsplit('/dex-bots/',1)[0]+'/dex-operator/status',headers=headers,
            json={'address':body['address']}).json()['operator_address']
        claim['operator']=operator
        claimed=client.post(controls+'/claim',headers=headers,json=claim)
        assert claimed.status_code==200 and claimed.json()['claim_granted'],claimed.text
        tx=rpc('eth_sendTransaction',[plan['transaction']]);receipt=rpc('eth_getTransactionReceipt',[tx])
        block=rpc('eth_getBlockByNumber',[receipt['blockNumber'],False])
        assert client.post(controls+'/hash',headers=headers,json={'tx_hash':tx}).status_code==200
        proof={'chain_id':'0x38','tx_hash':tx,'transaction':rpc('eth_getTransactionByHash',[tx]),'receipt':receipt,
            'block':block,'finalized':block,'pool':meta['pool'],'runtime':rpc('eth_getCode',[plan['transaction']['to'],block['number']])}
        checked=client.post(controls+'/observe',headers=headers,json={'address':body['address'],'tx_hash':tx})
        assert checked.status_code==200 and checked.json()['status']=='FINALIZED',checked.text
        assert client.post(controls+'/check',headers=headers,json={'evidence':proof}).status_code==200
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:client.post(apply_url,headers=headers,json=apply),range(2)))
        assert all(result.status_code==200 for result in results),[result.text for result in results]
        applied=results[0];assert results[1].json()['id']==applied.json()['id']
        successor=applied.json();assert successor['resume_from_run_id']==previous and not successor['execution_enabled']
        assert successor['root_binding_id']==body['binding_id'] and successor['effective_limits']==plan['limits']
        assert client.post(apply_url,headers=headers,json=apply).json()['id']==successor['id'] and len(hashes)==1
        clock[0]+=timedelta(seconds=1);mine()
        activate={**body,'binding_id':successor['id'],'request_id':str(uuid4()),'permission_mode':'SESSION'}
        assert client.post(base,headers=headers,json=activate).status_code==409
        activate['request_id']=str(uuid4());activate['confirmation']='REARMAR_BOT_DEX'
        resumed=client.post(base+'/'+previous+'/rearm',headers=headers,json=activate)
        assert resumed.status_code==200,resumed.text
        assert 'research_validation_id' not in activate
        assert resumed.json()['research_qualification']['evidence']==detail()['research_qualification']['evidence']
        assert resumed.json()['engine']=='orbis.dex.bot.long.v4' and resumed.json()['position']['owned_wbnb']==bought['position']['owned_wbnb']
        assert len(hashes)==1
        state['market'].bid,state['market'].ask='1.2','1.21';clock[0]+=timedelta(seconds=1);mine()
        current=activate['request_id']
        eventually(lambda:client.get(base+'/'+current,headers=headers).json(),lambda row:row['position']['owned_wbnb']=='0' and len(hashes)==2)
        assert client.post(base+'/'+current+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:client.get(base+'/'+current,headers=headers).json(),lambda row:not row['worker']['running'])
        assert client.post('/real/vault/backup',headers=headers).status_code==200


def test_lock_during_renewal_rpc_does_not_wait_for_network_or_commit_a_late_review(tmp_path,monkeypatch):
    entered,release=Event(),Event();gate={'enabled':False}
    def before_read():
        if gate['enabled']:entered.set();assert release.wait(15)
    with http_case(tmp_path,monkeypatch,before_read=before_read) as (client,headers,base,body,values,clock,mine,hashes,state):
        assert client.post(base,headers=headers,json=body).status_code==200
        previous=body['request_id']
        assert client.post(base+'/'+previous+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:client.get(base+'/'+previous,headers=headers).json(),lambda row:not row['worker']['running'])
        clock[0]+=timedelta(seconds=1);mine()
        controls_url=base.rsplit('/dex-bots/',1)[0]+'/dex-controls'
        before=client.get(controls_url,headers=headers).json();gate['enabled']=True
        renewal=base.rsplit('/runs',1)[0]+'/renewals'
        request={'request_id':str(uuid4()),'previous_run_id':previous,'draft_id':body['draft_id'],'address':body['address'],
            'expires_at':int(clock[0].timestamp())+3600,'gas':'1000000','gas_price':'1000000000','max_gas_bnb':'0.002'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(client.post,renewal,headers=headers,json=request)
            try:
                assert entered.wait(5)
                locked=pool.submit(client.post,'/real/vault/lock',headers=headers)
                assert locked.result(3).status_code==200
                assert not pending.done() and not release.is_set()
            finally:release.set()
            assert pending.result(10).status_code==401
        from tests.test_dex_deployments import PASSWORD
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.get(controls_url,headers=headers).json()==before and not hashes
