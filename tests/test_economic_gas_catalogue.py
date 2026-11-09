"""Opt-in gas catalogue and review on owned signed receipts and encrypted data."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.api import create_real_router
from app.portfolio import models as m
from tests.test_economic_gas_acceptance import ready
from tests.test_wallet_funding_api import prove
from tests.test_economic_review_api import confirmation


@contextmanager
def http_case(directory,kind='CALL_RESOLUTION',*,booked=False):
    with ready(directory,kind,booked=booked) as (case,_):
        case.vault.lock();start=datetime.now(timezone.utc);base=max(start,case.scope['now'])+timedelta(seconds=3)
        holder={}
        def supervisor(vault,connection):
            holder['vault']=vault
            return DexBotSupervisor(vault,connection,clock=lambda:base+(datetime.now(timezone.utc)-start))
        def no_rpc():raise AssertionError('Catalogue/review must not contact a node')
        app=FastAPI();app.include_router(create_real_router(case.vault.path,dex_rpc_factory=no_rpc,dex_bot_supervisor_factory=supervisor))
        with TestClient(app) as client:
            response=client.post('/real/vault/unlock',json={'passphrase':case.password});assert response.status_code==200,response.text
            headers={'X-Orbis-Vault-Token':response.json()['token']}
            prove(client,headers,case.account,Account.from_key('0x'+'11'*32))
            try:yield SimpleNamespace(**{**vars(case),'vault':holder['vault']},client=client,headers=headers,
                url=f'/real/accounts/{case.account}/economic-review')
            finally:client.post('/real/vault/lock',headers=headers)


@pytest.mark.parametrize('kind',list(m.GAS_ORDER_SOURCES))
def test_catalogue_gas_is_explicit_authenticated_distinct_and_has_no_phantom_trade(tmp_path,kind):
    with http_case(tmp_path,kind) as case:
        old=case.client.post(case.url+'/sources',headers=case.headers,json={'address':case.meta['owner']})
        assert old.status_code==200,old.text
        assert all(row['action'] in ('BUY','SELL') and 'gas_only' not in row for row in old.json()['sources'])
        body={'address':case.meta['owner'],'include_gas':True}
        assert case.client.post(case.url+'/sources',json=body).status_code==401
        response=case.client.post(case.url+'/sources',headers=case.headers,json=body)
        assert response.status_code==200,response.text
        rows=response.json()['sources'];keys=[(r['source_kind'],r['source_id']) for r in rows]
        assert len(keys)==len(set(keys))
        found=[row for row in rows if (row['source_kind'],row['source_id'])==(case.kind,case.id)]
        assert len(found)==1
        row=found[0];assert row['gas_only'] is True and row['action']=='GAS'
        assert row['quantity'] is None and row['gross_amount'] is None and not row['trade_candidates']
        assert row['order_checked'] and not row['booked'] and row['context_verified']
        assert response.headers['cache-control']=='no-store'
        for private in (case.meta['owner'],case.meta['operator'],case.scope['evidence']['tx_hash']):
            assert private.lower() not in response.text.lower()
        invalid={**body,'include_gas':'true'}
        assert case.client.post(case.url+'/sources',headers=case.headers,json=invalid).status_code==422


def test_first_opt_in_reviews_previously_booked_gas_without_requiring_a_new_receipt(tmp_path):
    with http_case(tmp_path,booked=True) as case:
        response=case.client.post(case.url+'/preview',headers=case.headers,json={'address':case.meta['owner']})
        assert response.status_code==409
        body={'address':case.meta['owner'],'include_gas':True}
        response=case.client.post(case.url+'/preview',headers=case.headers,json=body)
        assert response.status_code==200,response.text
        data=response.json();assert data['mode']=='EXISTING' and data['review']['events_to_create']==0
        assert data['review']['checked_receipts']==1
        response=case.client.post(case.url+'/confirm',headers=case.headers,json=confirmation(body,data))
        assert response.status_code==200,response.text
        assert response.json()['created']
        response=case.client.post(case.url+'/confirm',headers=case.headers,json=confirmation(body,data))
        assert response.status_code==200 and not response.json()['created']


def test_opt_in_is_bound_through_new_batch_revalidation_with_previously_booked_gas(tmp_path):
    from tests.test_economic_gas_acceptance import next_approval,register
    # Keep one known gas booking and create another signed gas receipt before
    # reauthentication. The request must carry the opt-in through both stages.
    with ready(tmp_path,booked=True) as (case,_):
        later=next_approval(case);register(later)
        from app.portfolio.economic_receipt_source import ReceiptSelection
        from app.portfolio import economic_receipt_acceptance as batches
        selected=[ReceiptSelection(later.kind,later.id,later.account,later.meta['owner'],gas_only=True)]
        at=max(datetime.now(timezone.utc),later.scope['now'])+timedelta(seconds=1)
        from uuid import uuid4
        options={'batch_id':str(uuid4()),'recorded_at':at,'include_gas':True}
        proposal=batches.preview_receipt_acceptance(case.vault,case.target,selected,**options)
        result=batches.confirm_receipt_acceptance(case.vault,case.target,selected,**options,
            preview_digest=proposal['preview_digest'],now=at+timedelta(seconds=1))
        assert result['created'] and result['events_created']==1
