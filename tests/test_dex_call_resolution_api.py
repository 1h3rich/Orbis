"""Private call recovery, real owned EVM receipts, no secrets or sends by API."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import create_real_router
from app.portfolio import dex_operator_journal as operator
from tests.dex_evm_fixture import evm
from tests.test_dex_call_resolution import prepare,now
from tests.test_dex_call_resolution_operator import case as operator_case,lower_price,pending
from tests.test_dex_operator import write
from tests.test_wallet_swap import setup,PASSWORD
from tests.test_dex_deployments import PASSWORD as OPERATOR_PASSWORD
from tests.test_wallet_funding_api import prove


@contextmanager
def http_case(directory,*,executed=False,operator_source=False,original=False):
    @contextmanager
    def wallet_case():
        vault,account,_=setup(directory)
        try:
            with evm(directory/'evm',backend='anvil') as (meta,rpc):yield vault,account,meta,rpc,None,None,None
        finally:vault.lock()
    with (operator_case(directory,evm_backend='anvil') if operator_source else wallet_case()) as values:
        vault,account,meta,rpc,deployment,grant,_=values
        if operator_source:
            trade=lower_price(meta,rpc,int(deployment['nonce']))
            source,before,_=pending(values,trade);tx={**trade.transaction(),'gasPrice':hex(10**9)}
            if not executed:tx.update(to=meta['operator'],data='0x',gas='0x5208')
            password=OPERATOR_PASSWORD;kind='OPERATOR'
        else:
            if executed:
                write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
                for token in ('usdt','wbnb'):
                    for recipient in (meta['owner'],meta['pool']):write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**18])
                write(rpc,{**meta,'operator':meta['owner']},meta['usdt'],'approve(address,uint256)',
                    ['address','uint256'],[meta['router'],10**18])
            if original and executed:
                earlier, _, _ = prepare(vault, account, {**meta, "owner": meta["operator"]}, rpc)
                meta["other_pending"] = earlier["id"]
            plan,source,before=prepare(vault,account,meta,rpc)
            rpc('miner_stop',[])
            tx=plan['transaction']
            if not original:
                rpc('eth_sendTransaction',[tx])
                tx={**tx,'gasPrice':hex(2*10**9)}
                if not executed:tx.update(to=meta['owner'],data='0x',gas='0x5208')
            password=PASSWORD;kind='WALLET_SWAP'
        sent=rpc('eth_sendTransaction',[tx]);rpc('evm_mine',[]);rpc('miner_start',[1])
        vault.lock();reads=[];gate={'on':False};entered,release=Event(),Event()
        @contextmanager
        def reader():
            if gate['on']:entered.set();assert release.wait(10)
            def read(method,params):
                assert 'send' not in method.lower(),method
                reads.append(method)
                if method == "eth_getTransactionReceipt" and gate.get("pending"):
                    return None
                return rpc(method,params)
            yield read
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=reader))
        with TestClient(app) as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':password}).json()['token']}
            url=f'/real/accounts/{account}/dex-call-resolutions/{kind}/{source.id}'
            body={'address':meta['owner'],'tx_hash':sent,'accept_changes':True}
            yield client,headers,url,body,values,source,before,reads,gate,entered,release,password
            client.post('/real/vault/lock',headers=headers)


def command(body,preview):
    return {**body,'preview_digest':preview['preview_digest'],'source_state_digest':preview['source_state_digest'],
        'confirmation':'RESOLVER_LLAMADA_DEX'}


@pytest.mark.parametrize("executed", [False, True])
def test_check_original_accepts_only_verified_finalized_evm_receipts(tmp_path, executed):
    with http_case(tmp_path, executed=executed, original=True) as case:
        client, headers, url, body, values, source, _, reads, gate, *_ = case
        _, account, meta, rpc, *_ = values
        body = {key: body[key] for key in ("address", "tx_hash")}
        path = url+"/check-original"
        wallet_path = f"/real/accounts/{account}/wallet-swaps/{source.id}"
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers=headers, json=body).status_code == 409
        assert client.post(wallet_path+"/check", headers=headers, json={"evidence": {}}).status_code == 404
        prove(client, headers, account, Account.from_key("0x"+"11"*32))
        assert client.post(path, headers=headers, json={**body, "evidence": {}}).status_code == 422
        assert client.post(path, headers=headers, json=body).status_code == 409
        assert client.post(wallet_path+"/hash", headers=headers, json={"tx_hash": body["tx_hash"]}).is_success
        assert client.post(path, headers=headers, json={**body, "tx_hash": "0x"+"aa"*32}).status_code == 409
        assert not reads
        gate["pending"] = True
        pending = client.post(path, headers=headers, json=body)
        assert pending.status_code == 409 and "Todavía no hay un recibo finalizado" in pending.text
        status = client.get(url+"/resolution", headers={**headers, "X-Orbis-Wallet": meta["owner"]}).json()
        assert not status["resolved"] and status["source_status"] == "HASH_REPORTED"
        gate["pending"] = False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: client.post(path, headers=headers, json=body), range(2)))
        assert all(response.status_code == 200 for response in results), [response.text for response in results]
        result = results[0].json()
        assert result == results[1].json() and result["matches_original"] is True and not result["fee_changed"]
        assert result["outcome"] == ("EXECUTED" if executed else "REVERTED")
        assert result["receipt"]["gas_used"] == str(int(rpc("eth_getTransactionReceipt", [body["tx_hash"]])["gasUsed"], 16))
        expected_effect = {"amount_out_units": source.terms["amount_units"], "approved_units": None} if executed else None
        assert result["effect"] == expected_effect
        read_count = len(reads)
        retry = client.post(path, headers=headers, json=body)
        assert retry.json() == result and retry.headers["cache-control"] == "no-store" and len(reads) == read_count
        history = client.get(wallet_path.rsplit("/", 1)[0], headers=headers).json()
        if executed:
            pending, confirmed = history
            assert pending["id"] == meta["other_pending"] and pending["status"] == "WALLET_REQUESTED"
        else:
            confirmed, = history
        assert confirmed["resolution"] == result and not confirmed["booked"]
        assert all(value.lower() not in str(history).lower() for value in (meta["owner"], body["tx_hash"]))


def test_check_original_requires_separate_review_for_changed_signed_gas(tmp_path):
    with http_case(tmp_path, executed=True) as case:
        client, headers, url, body, values, source, _, reads, *_ = case
        _, account, meta, *_ = values
        prove(client, headers, account, Account.from_key("0x"+"11"*32))
        wallet_path = f"/real/accounts/{account}/wallet-swaps/{source.id}"
        assert client.post(wallet_path+"/hash", headers=headers, json={"tx_hash": body["tx_hash"]}).is_success
        response = client.post(url+"/check-original", headers=headers, json={key: body[key] for key in ("address", "tx_hash")})
        assert response.status_code == 409 and "Revisar resultado" in response.text and reads
        status = client.get(url+"/resolution", headers={**headers, "X-Orbis-Wallet": meta["owner"]}).json()
        assert not status["resolved"] and status["source_status"] == "HASH_REPORTED"
        preview = client.post(url+"/resolution-preview", headers=headers, json=body).json()
        assert preview["available"] and preview["fee_changed"]
        assert client.post(url+"/resolve", headers=headers, json=command(body, preview)).is_success


@pytest.mark.parametrize('executed',[False,True])
def test_proven_owner_reviews_then_confirms_once_and_get_recovers_after_reopen_without_rpc(tmp_path,executed):
    with http_case(tmp_path,executed=executed) as (client,headers,url,body,values,source,before,reads,gate,entered,release,password):
        _,account,meta,_,_,_,_=values;get_headers={**headers,'X-Orbis-Wallet':meta['owner']}
        assert client.post(url+'/resolution-preview',json=body).status_code==401
        assert client.get(url+'/resolution',headers=get_headers).status_code==409
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        assert client.post(url+'/resolution-preview',headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        assert client.post(url+'/resolution-preview',headers=headers,json={**body,'evidence':{}}).status_code==422
        assert client.post(url+'/resolution-preview',headers=headers,json={**body,'accept_changes':'true'}).status_code==422
        assert client.post(url+'/resolution-preview',headers=headers,json={**body,'address':meta['operator']}).status_code==409
        assert not reads
        status=client.get(url+'/resolution',headers=get_headers)
        assert status.status_code==200 and not status.json()['resolved'] and status.headers['cache-control']=='no-store'
        assert not reads
        response=client.post(url+'/resolution-preview',headers=headers,json={**body,'accept_changes':False})
        assert response.status_code==200 and not response.json()['available'],response.text
        response=client.post(url+'/resolution-preview',headers=headers,json=body)
        assert response.status_code==200,response.text
        preview=response.json();assert preview['outcome']==('EXECUTED' if executed else 'CANCELLED') and preview['available']
        search={key:value for key,value in body.items() if key!='tx_hash'}
        found=client.post(url+'/discover',headers=headers,json=search)
        assert found.status_code==200 and found.json()=={**preview,'discovered_tx_hash':body['tx_hash']},found.text
        assert found.headers['cache-control']=='no-store'
        assert client.post(url+'/discover',headers=headers,json=body).status_code==422
        commit=command(body,preview)
        assert client.post(url+'/resolve',headers=headers,json={**commit,'preview_digest':'f'*64}).status_code==409
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:client.post(url+'/resolve',headers=headers,json=commit),range(2)))
        assert all(r.status_code==200 for r in responses),[r.text for r in responses]
        assert responses[0].json()==responses[1].json();count=len(reads)
        stored=client.get(url+'/resolution',headers=get_headers)
        assert stored.json()==responses[0].json() and len(reads)==count
        wallet_path=f'/real/accounts/{account}/wallet-swaps/{source.id}'
        assert client.post(wallet_path+'/hash',headers=headers,json={'tx_hash':body['tx_hash']}).status_code==409
        assert client.post(wallet_path+'/rejected',headers=headers,json={'confirmation':'WALLET_RECHAZO_4001'}).status_code==409
        assert client.post(url+'/discover',headers=headers,json=search).json()==stored.json() and len(reads)==count
        assert meta['owner'].lower() not in stored.text.lower() and body['tx_hash'] not in stored.text
        assert client.post(url+'/resolve',headers=headers,json={**commit,'tx_hash':'0x'+'f'*64}).status_code==409 and len(reads)==count
        assert client.post('/real/vault/backup',headers=headers).status_code==200
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        assert client.get(url+'/resolution',headers=get_headers).status_code==401
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':password}).json()['token']}
        get_headers={**headers,'X-Orbis-Wallet':meta['owner']}
        assert client.get(url+'/resolution',headers=get_headers).status_code==409
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        assert client.get(url+'/resolution',headers=get_headers).json()==stored.json() and len(reads)==count


@pytest.mark.parametrize('change',['LOCK','SOURCE'])
def test_rpc_releases_private_locks_and_late_confirmation_cannot_adopt_changed_scope(tmp_path,change):
    with http_case(tmp_path) as (client,headers,url,body,values,source,before,reads,gate,entered,release,password):
        _,account,meta,_,_,_,_=values
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        response=client.post(url+'/resolution-preview',headers=headers,json=body);assert response.status_code==200,response.text
        commit=command(body,response.json());gate['on']=True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,url+'/resolve',headers=headers,json=commit)
            try:
                assert entered.wait(5)
                if change=='LOCK':assert client.post('/real/vault/lock',headers=headers).status_code==200
                else:
                    altered=client.post(f'/real/accounts/{account}/wallet-swaps/{source.id}/hash',headers=headers,json={'tx_hash':body['tx_hash']})
                    assert altered.status_code==200,altered.text
            finally:release.set()
            result=future.result(15)
        assert result.status_code==(401 if change=='LOCK' else 409),result.text
        if change=='LOCK':
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':password}).json()['token']}
            prove(client,headers,account,Account.from_key('0x'+'11'*32))
        state=client.get(url+'/resolution',headers={**headers,'X-Orbis-Wallet':meta['owner']})
        assert state.status_code==200 and not state.json()['resolved']


def test_operator_resolution_requires_stopped_authority_and_needs_no_restored_operator_secret(tmp_path):
    with http_case(tmp_path,operator_source=True,executed=True) as (client,headers,url,body,values,source,before,reads,gate,entered,release,password):
        vault,account,meta,_,_,grant,_=values
        prove(client,headers,account,Account.from_key('0x'+'11'*32))
        denied=client.post(url+'/resolution-preview',headers=headers,json=body)
        assert denied.status_code==409 and 'Detén' in denied.text and not reads
        search={key:value for key,value in body.items() if key!='tx_hash'}
        denied=client.post(url+'/discover',headers=headers,json=search)
        assert denied.status_code==409 and 'Detén' in denied.text and not reads
        vault.unlock(password)
        with vault.session() as db:operator.stop_grant(db,grant['id'],account_id=account,now=now())
        vault.lock()
        response=client.post(url+'/resolution-preview',headers=headers,json=body)
        assert response.status_code==200,response.text
        found=client.post(url+'/discover',headers=headers,json=search)
        assert found.status_code==200 and found.json()=={**response.json(),'discovered_tx_hash':body['tx_hash']},found.text
        result=client.post(url+'/resolve',headers=headers,json=command(body,response.json()))
        assert result.status_code==200 and result.json()['outcome']=='EXECUTED',result.text
        assert client.get(url+'/resolution',headers={**headers,'X-Orbis-Wallet':meta['owner']}).json()==result.json()
