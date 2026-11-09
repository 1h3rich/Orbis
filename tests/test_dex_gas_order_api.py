"""Protected observation of actual gas-only receipts on owned EVMs."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.api import create_real_router, RealAccess
from app.portfolio import dex_gas_order as orders, models as m
from app.portfolio.wallet_funding import _encoded
from tests.gas_order_fixture import gas_case
from tests.test_wallet_funding_api import prove


def events(vault):
    with vault.session() as db:
        return tuple((r.id,r.account_id,r.payload_digest) for r in db.scalars(select(m.RealEvent).order_by(m.RealEvent.id)))


@contextmanager
def http_case(directory,kind='CALL_RESOLUTION',*,typed=False):
    with gas_case(directory,kind,typed=typed) as original:
        vault=original.vault;before=events(vault);vault.lock()
        calls=[];holder={};gate={'entered':Event(),'release':Event(),'phase':None,'contexts':0,'fault':None}
        @contextmanager
        def reader():
            if gate['fault']=='factory':raise RuntimeError('private-rpc-endpoint-secret')
            gate['contexts']+=1;phase=gate['contexts']
            def rpc(method,params):
                assert not any(part in method.lower() for part in ('send','sign')),method
                calls.append((method,params))
                if gate['phase']==phase:
                    gate['phase']=None;gate['entered'].set();assert gate['release'].wait(10)
                if gate['fault']=='network':raise RuntimeError('private-rpc-endpoint-secret')
                result=original.rpc(method,params)
                if gate['fault'] in ('duplicate','wrong_block') and method=='eth_getBlockByNumber' and params[0].startswith('0x'):
                    result=deepcopy(result)
                    if gate['fault']=='duplicate':result['transactions']=[result['transactions'][0]]*2
                    else:result['hash']='0x'+'f'*64
                return result
            yield rpc
        def supervisor(current,connection):
            holder['vault']=current
            return DexBotSupervisor(current,connection,clock=lambda:max(datetime.now(timezone.utc),original.scope['now']))
        app=FastAPI();app.include_router(create_real_router(vault.path,dex_rpc_factory=reader,dex_bot_supervisor_factory=supervisor))
        with TestClient(app) as client:
            result=client.post('/real/vault/unlock',json={'passphrase':original.password})
            assert result.status_code==200,result.text
            case=SimpleNamespace(**{k:v for k,v in vars(original).items() if k!='vault'},client=client,
                vault=holder['vault'],headers={'X-Orbis-Vault-Token':result.json()['token']},before=before,calls=calls,gate=gate,
                url=f'/real/accounts/{original.account}/dex-receipts/{kind}/{original.id}/gas-order-proof')
            try:yield case
            finally:client.post('/real/vault/lock',headers=case.headers)


def owner(case):
    prove(case.client,case.headers,case.account,Account.from_key('0x'+'11'*32))


def post(case,**kwargs):
    return case.client.post(case.url,headers=case.headers,json={'address':case.meta['owner']},**kwargs)


@pytest.mark.parametrize('kind,typed',[(kind,False)for kind in m.GAS_ORDER_SOURCES]+[('DEPLOYMENT',True),('CALL_RESOLUTION',True)])
def test_each_source_is_discovered_once_and_reopens_without_rpc_or_ledger_writes(tmp_path,kind,typed):
    with http_case(tmp_path,kind,typed=typed) as case:
        assert case.client.get('/real/vault/status').json()['capabilities']['dex_gas_order_proof']
        assert case.client.post(case.url,json={'address':case.meta['owner']}).status_code==401
        assert post(case).status_code==409 and not case.calls
        owner(case)
        headers={**case.headers,'X-Orbis-Wallet':case.meta['owner']}
        pending=case.client.get(case.url,headers=headers)
        assert pending.status_code==200 and not pending.json()['saved'] and not case.calls
        result=post(case);assert result.status_code==200,result.text
        body=result.json();assert body['created'] and body['saved'] and body['source_kind']==kind
        assert body['proof']['purpose']=='NATIVE_GAS_ONLY' and body['proof']['gas_fee_wei']==str(int(case.scope['evidence']['receipt']['gasUsed'],16)*int(case.scope['evidence']['receipt']['effectiveGasPrice'],16))
        assert not body['trading_authority'] and not body['applied_to_ledger']
        assert result.headers['cache-control']=='no-store' and events(case.vault)==case.before
        calls=len(case.calls)
        for response in (post(case),case.client.get(case.url,headers=headers)):
            assert response.status_code==200 and response.json()['id']==body['id'] and not response.json()['created']
        assert len(case.calls)==calls
        with case.vault.session() as db:
            row,_=orders.gas_order_record(db,body['id']);raw=row.payload_json
            assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==1
        for private in (case.meta['owner'],case.meta['operator'],case.scope['evidence']['tx_hash'],case.scope['evidence']['transaction']['r']):
            assert private.lower() not in raw.lower() and private.lower() not in result.text.lower()
        assert case.client.post('/real/vault/lock',headers=case.headers).status_code==200
        case.headers={'X-Orbis-Vault-Token':case.client.post('/real/vault/unlock',json={'passphrase':case.password}).json()['token']}
        assert post(case).status_code==409
        owner(case);case.gate['fault']='network'
        assert post(case).json()['id']==body['id'] and len(case.calls)==calls


def test_scope_strict_shapes_origin_and_passive_observation(tmp_path,monkeypatch):
    with http_case(tmp_path) as case:
        owner(case);body={'address':case.meta['owner']}
        assert case.client.post(case.url,headers={**case.headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        assert case.client.post(case.url,headers=case.headers,json={**body,'tx_hash':'0x'+'1'*64}).status_code==422
        assert case.client.post(case.url.replace(case.id,str(uuid4())),headers=case.headers,json=body).status_code==409
        other_context=case.client.post('/real/accounts',headers=case.headers,json={'name':'Other owned context','base_currency':'BNB'}).json()['id']
        prove(case.client,case.headers,other_context,Account.from_key('0x'+'11'*32))
        assert case.client.post(case.url.replace(case.account,other_context),headers=case.headers,json=body).status_code==409
        other=Account.from_key('0x'+'33'*32);prove(case.client,case.headers,case.account,other)
        assert case.client.post(case.url,headers=case.headers,json={'address':other.address}).status_code==409
        assert not case.calls
        touched=[];original=RealAccess._touch
        def touch(access):touched.append(True);return original(access)
        monkeypatch.setattr(RealAccess,'_touch',touch)
        response=case.client.post(case.url,headers={**case.headers,'X-Orbis-Passive':'1'},json=body)
        assert response.status_code==200,response.text
        assert not touched and response.json()['created']
        assert post(case).status_code==200 and touched
        assert case.client.post(case.url.replace('CALL_RESOLUTION','INVENTED'),headers=case.headers,json=body).status_code==422


@pytest.mark.parametrize('phase',[1,2])
def test_lock_during_each_rpc_phase_revokes_the_result_without_blocking_or_saving(tmp_path,phase):
    with http_case(tmp_path) as case:
        owner(case);case.gate['phase']=phase
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(post,case);assert case.gate['entered'].wait(5)
            try:locked=case.client.post('/real/vault/lock',headers=case.headers)
            finally:case.gate['release'].set()
            assert locked.status_code==200
            assert future.result(timeout=10).status_code==401
        case.headers={'X-Orbis-Vault-Token':case.client.post('/real/vault/unlock',json={'passphrase':case.password}).json()['token']}
        with case.vault.session() as db:assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==0
        assert events(case.vault)==case.before


@pytest.mark.parametrize('fault',['network','factory','duplicate','wrong_block'])
def test_failed_observation_is_private_and_creates_nothing(tmp_path,fault):
    with http_case(tmp_path) as case:
        owner(case);case.gate['fault']=fault;response=post(case)
        assert response.status_code==(503 if fault in ('network','factory') else 409),response.text
        assert response.headers['cache-control']=='no-store' and 'private-rpc-endpoint-secret' not in response.text
        with case.vault.session() as db:assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==0
        assert events(case.vault)==case.before


def test_original_changed_after_discovery_is_reauthenticated_before_save(tmp_path):
    with http_case(tmp_path) as case:
        owner(case);case.gate['phase']=2
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(post,case);assert case.gate['entered'].wait(5)
            try:
                with case.vault.session() as db:
                    row=db.get(m.RealDexCallResolution,case.id);payload=json.loads(row.payload_json)
                    row.payload_json,row.payload_digest=_encoded({**payload,'nonce':'999'});db.commit()
            finally:case.gate['release'].set()
            assert future.result(timeout=10).status_code==409
        with case.vault.session() as db:assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==0


def test_concurrent_http_observations_save_one_record_and_do_not_book_the_receipt(tmp_path):
    with http_case(tmp_path) as case:
        owner(case)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:post(case),range(2)))
        assert all(r.status_code==200 for r in results),[r.text for r in results]
        assert sorted(r.json()['created'] for r in results)==[False,True]
        assert len({r.json()['id'] for r in results})==1 and events(case.vault)==case.before
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==1
            assert db.scalar(select(func.count()).select_from(m.RealDexReceiptBooking))==0


def test_executed_financial_receipt_is_rejected_by_gas_route_and_keeps_its_original_route(tmp_path,monkeypatch):
    from tests.test_dex_receipt_order_api import http_case as trade_case,verify_owner
    with trade_case(tmp_path,monkeypatch) as case:
        verify_owner(case)
        response=case.client.post(case.url.replace('/order-proof','/gas-order-proof'),
            headers=case.headers,json={'address':case.meta['owner']})
        assert response.status_code==409 and not case.calls
        with case.vault.session() as db:assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==0
        response=case.client.post(case.url,headers=case.headers,json={'address':case.meta['owner']})
        assert response.status_code==200,response.text
        assert response.json()['created'] and response.json()['proof']['schema']=='orbis.dex.receipt.order.proof.v1'


def test_protecting_view_during_observation_revokes_token_even_with_same_open_vault_generation(tmp_path,monkeypatch):
    with http_case(tmp_path) as case:
        owner(case);case.gate['phase']=2;generation=case.vault.session_generation
        original=RealAccess.operational_status
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(post,case);assert case.gate['entered'].wait(5)
            try:
                # Only model the existing retained operational lease. No worker
                # is started, authorized, stopped or sent to an external venue.
                monkeypatch.setattr(RealAccess,'operational_status',lambda _:{'active':True})
                response=case.client.post('/real/vault/protect-view',headers=case.headers)
                assert response.status_code==200 and response.json()['operating']
                assert case.vault.unlocked and case.vault.session_generation==generation
            finally:
                monkeypatch.setattr(RealAccess,'operational_status',original)
                case.gate['release'].set()
            assert future.result(timeout=10).status_code==401
        with case.vault.session() as db:assert db.scalar(select(func.count()).select_from(m.RealDexGasOrderProof))==0
        case.headers={'X-Orbis-Vault-Token':case.client.post('/real/vault/unlock',json={'passphrase':case.password}).json()['token']}
