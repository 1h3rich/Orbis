"""Protected HTTP accounting of local EVM fills, including review and reopen."""
from datetime import timedelta
from uuid import uuid4
from eth_account import Account
from tests.test_dex_bot_run_api import http_case,eventually
from tests.test_wallet_funding_api import prove
from tests.test_dex_deployments import PASSWORD


def test_receipt_accounting_requires_proof_pins_review_and_never_reexecutes_order(tmp_path,monkeypatch):
    with http_case(tmp_path,monkeypatch) as (client,headers,base,body,values,clock,mine,hashes,controls):
        account=values[1];wallet=values[4]['owner'];path=f'/real/accounts/{account}/dex-receipts'
        assert client.post(path+'/query',json={'address':wallet}).status_code==401
        assert client.post(path+'/query',headers={**headers,'Origin':'https://outside.invalid'},json={'address':wallet}).status_code==403
        owner=client.post('/real/accounts',headers=headers,json={'name':'Wallet propia','base_currency':'USDT'}).json()['id']
        prove(client,headers,owner,Account.from_key('0x'+'11'*32))
        linked=client.post(f'/real/accounts/{owner}/wallet-links',headers=headers,json={'address':wallet,'chain_id':'0x38','alias':'Principal','confirmation':'VINCULAR'})
        assert linked.status_code==200,linked.text
        for currency,amount in [('USDT','100'),('BNB','0.01')]:
            response=client.post(f'/real/accounts/{owner}/events',headers=headers,json={'request_id':str(uuid4()),'kind':'CONTRIBUTION',
                'quote_currency':currency,'amount':amount,'occurred_at':(clock[0]-timedelta(days=1)).isoformat()})
            assert response.status_code==200,response.text
        assert client.post(base,headers=headers,json=body).status_code==200
        clock[0]+=timedelta(seconds=1);mine()
        eventually(lambda:client.get(base+'/'+body['request_id'],headers=headers).json(),lambda row:bool(row.get('intents')) and row['intents'][0]['status']=='FINALIZED')
        query=client.post(path+'/query',headers=headers,json={'address':wallet,'limit':1})
        assert query.status_code==200 and query.headers['cache-control']=='no-store',query.text
        page=query.json();assert page['total']>=2 and page['next_offset']==1
        assert client.post(path+'/query',headers=headers,json={'address':wallet,'limit':1,'offset':1}).status_code==409
        assert client.post(path+'/query',headers=headers,json={'address':wallet,'limit':True}).status_code==422
        assert client.post(path+'/query',headers=headers,json={'address':wallet,'limit':1,'offset':1,'revision':page['revision']}).status_code==200
        rows=client.post(path+'/query',headers=headers,json={'address':wallet}).json()['items']
        receipt=next(row for row in rows if row['source_kind']=='OPERATOR');url=path+'/OPERATOR/'+receipt['source_id']
        assert client.post(url+'/booking-preview',headers=headers,json={'address':wallet,'raw_transaction':'0x'}).status_code==422
        assert client.post(url+'/booking-preview',headers=headers,json={'address':Account.from_key('0x'+'33'*32).address}).status_code==409
        preview=client.post(url+'/booking-preview',headers=headers,json={'address':wallet});assert preview.status_code==200,preview.text
        value=preview.json();assert value['target_account_id']==owner
        commit={'address':wallet,'trade_event_id':None,'gas_event_ids':[],'expected_revision':value['revision'],
            'preview_digest':value['preview_digest'],'confirmation':'CONTABILIZAR_RECIBO_DEX'}
        assert client.post(url+'/book',headers=headers,json={**commit,'preview_digest':'ab'*32}).status_code==409
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:client.post(url+'/book',headers=headers,json=commit),range(2)))
        assert all(result.status_code==200 for result in results),[result.text for result in results]
        assert sorted(result.json()['events_created'] for result in results)==[0,2]
        assert results[0].json()['booking_id']==results[1].json()['booking_id']
        done=next(result for result in results if result.json()['events_created']==2)
        assert client.post(url+'/book',headers=headers,json=commit).json()['events_created']==0
        chart=client.get(base+'/'+body['request_id']+'/executions-page',headers=headers).json()
        assert chart['booked_count']==1 and chart['financially_booked'] and chart['executions'][0]['event_id'] in done.json()['event_ids']
        assert client.post(path+'/query',headers=headers,json={'address':wallet,'offset':1,'revision':page['revision']}).status_code==409
        assert client.get(f'/real/accounts/{account}/snapshot',headers=headers).json()['cash']=={}
        snapshot=client.get(f'/real/accounts/{owner}/snapshot',headers=headers).json()
        assert snapshot['contributions']=={'USDT':'100','BNB':'0.01'} and len(snapshot['positions'])==1
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        assert client.post(path+'/query',headers=headers,json={'address':wallet}).status_code==401
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.post(path+'/query',headers=headers,json={'address':wallet}).status_code==409
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        assert client.post(url+'/book',headers=headers,json=commit).json()['events_created']==0
        assert len(hashes)==1
