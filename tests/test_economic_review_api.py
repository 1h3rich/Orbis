"""Actual HTTP/SQLCipher/signed receipts; no owner keys, network or real funds."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json

import pytest
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio import economic_acceptance as archive
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event,account_snapshot
from app.portfolio.economic_projection_review import _snapshot
from decimal import Decimal as D
from uuid import uuid4
from tests.test_dex_receipt_order_api import http_case,verify_owner
from tests.test_economic_receipt_source import counts


@contextmanager
def api_case(directory,monkeypatch,kind='CALL_RESOLUTION'):
    holder={}
    def supervisor(v,c,*,clock):
        start=datetime.now(timezone.utc);base=max(start,clock())+timedelta(seconds=2)
        item=DexBotSupervisor(v,c,clock=lambda:base+(datetime.now(timezone.utc)-start));holder['supervisor']=item;return item
    monkeypatch.setattr('tests.test_dex_receipt_order_api.DexBotSupervisor',supervisor)
    with http_case(directory,monkeypatch,kind)as case:
        verify_owner(case)
        generic=case.client.post(f'/real/accounts/{case.account}/dex-receipts/query',headers=case.headers,
            json={'address':case.meta['owner'],'limit':100,'offset':0})
        assert generic.status_code==200,generic.text
        receipts={(row['source_kind'],row['source_id'])for row in generic.json()['items']if row['action']in ('BUY','SELL')and not row['booked']}
        manual=case.client.get(f'/real/accounts/{case.account}/wallet-swaps',headers=case.headers)
        assert manual.status_code==200,manual.text
        for row in manual.json():
            if row['action']=='SWAP'and not row['booked']and (row['status']=='FINALIZED'or row.get('effective_outcome')=='EXECUTED'):
                receipts.add((row.get('booking_source_kind','WALLET_SWAP'),row.get('booking_source_id',row['id'])))
        for source_kind,identifier in receipts:
            proof=case.client.post(f'/real/accounts/{case.account}/dex-receipts/{source_kind}/{identifier}/order-proof',
                headers=case.headers,json={'address':case.meta['owner']})
            assert proof.status_code==200,proof.text
        body={'address':case.meta['owner'],'receipts':[{'source_kind':kind,'source_id':identifier}for kind,identifier in sorted(receipts)]}
        yield SimpleNamespace(**vars(case),base=f'/real/accounts/{case.account}/economic-review',body=body,supervisor=holder['supervisor'])


def confirmation(body,preview):
    return {**body,**{key:preview[key]for key in ('mode','batch_id','recorded_at','preview_digest')},'confirmation':'ACEPTAR_CRITERIO_CONTABLE'}


@pytest.mark.parametrize('kind',['CALL_RESOLUTION','OPERATOR'])
def test_http_batch_preview_confirm_result_reopen_and_current_method_review(tmp_path,monkeypatch,kind):
    with api_case(tmp_path,monkeypatch,kind)as case:
        before=counts(case.vault);case.gate['failure']=True;rpc_calls=len(case.calls)
        with case.vault.session()as db:actual=_snapshot(account_snapshot(db,case.ledger))
        preview=case.client.post(case.base+'/preview',headers=case.headers,json=case.body)
        assert preview.status_code==200,preview.text
        data=preview.json();assert data['mode']=='BATCH'and data['operating']['can_accept']
        assert data['root_account_id']==case.ledger and data['root_account_id']!=case.account
        assert data['review']['events_to_create']==2*len(case.body['receipts'])
        comparison=next(row for row in data['review']['accounts']if row['account_id']==case.ledger)
        assert comparison['current']==actual and comparison['current_error']is None
        assert comparison['current']!=comparison['registration'] and comparison['changed']
        assert counts(case.vault)==before and not data['moves_funds']and 'draft'not in data
        assert case.meta['owner'].lower()not in preview.text.lower()and case.meta['operator'].lower()not in preview.text.lower()
        assert preview.headers['cache-control']=='no-store'
        saved=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(case.body,data))
        assert saved.status_code==200,saved.text
        assert saved.json()['created']and saved.json()['is_current']and saved.json()['events_created']==data['review']['events_to_create']
        query={'address':case.meta['owner'],'preview_digest':data['preview_digest']}
        recovered=case.client.post(case.base+'/result',headers=case.headers,json=query)
        assert recovered.status_code==200 and recovered.json()['saved']and recovered.json()['is_current']
        repeated=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(case.body,data))
        assert repeated.status_code==200 and not repeated.json()['created']and repeated.json()['events_created']==0
        current=case.client.post(case.base+'/preview',headers=case.headers,json={'address':case.meta['owner']})
        assert current.status_code==200,current.text
        assert current.json()['mode']=='EXISTING'and current.json()['review']['events_to_create']==0
        assert all(row['current']==row['network']and not row['changed']for row in current.json()['review']['accounts'])
        second=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation({'address':case.meta['owner']},current.json()))
        assert second.status_code==200,second.text
        assert second.json()['created']and second.json()['is_current']
        assert not case.client.post(case.base+'/result',headers=case.headers,json=query).json()['is_current']
        with case.vault.session()as db:
                append_event(db,case.ledger,str(uuid4()),EventInput('CONTRIBUTION','USDT',case.supervisor.now()+timedelta(seconds=30),amount=D('1')))
        old=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(case.body,data))
        assert old.status_code==200 and not old.json()['created']and not old.json()['is_current']
        assert len(case.calls)==rpc_calls


def test_source_catalog_is_private_and_identifies_existing_manual_trade_without_writes(tmp_path,monkeypatch):
    from app.portfolio.dex_call_booking import _sources
    from app.portfolio.dex_deployments import _identity
    from app.portfolio.wallet_swap_booking import _trade
    with api_case(tmp_path,monkeypatch)as case:
        with case.vault.session()as db:
            source=next(item for item in _sources(db,case.meta['owner'],_identity(db,'wallet',case.meta['owner']))
                if (item.kind,item.id)==(case.kind,case.source_id))
            trade=_trade(source.trade_terms,source.receipt,datetime.fromisoformat(source.receipt['block_at']))
            manual=append_event(db,case.ledger,str(uuid4()),trade).id
        before=counts(case.vault);case.gate['failure']=True;rpc_calls=len(case.calls)
        response=case.client.post(case.base+'/sources',headers=case.headers,json={'address':case.meta['owner']})
        assert response.status_code==200,response.text
        assert response.headers['cache-control']=='no-store'
        assert response.json()['root_account_id']==case.ledger
        rows=response.json()['sources']
        assert {(row['source_kind'],row['source_id'])for row in rows}=={
            (row['source_kind'],row['source_id'])for row in case.body['receipts']}
        row=next(row for row in rows if row['source_id']==case.source_id)
        assert row['context_verified']and row['order_checked']and not row['booked']
        assert [item['id']for item in row['trade_candidates']]==[manual]
        assert row['gas_candidates']==[]
        assert case.meta['owner'].lower()not in response.text.lower()
        assert case.meta['operator'].lower()not in response.text.lower()
        assert counts(case.vault)==before and len(case.calls)==rpc_calls
        assert case.client.post(case.base+'/sources',json={'address':case.meta['owner']}).status_code==401
        assert case.client.post(case.base+'/sources',headers=case.headers,json={'address':case.meta['operator']}).status_code==409


def test_current_method_failure_is_reported_without_raw_order_fallback(monkeypatch):
    from app.portfolio.economic_review_api import current_snapshots
    from app.portfolio.economic_projection import ProjectionError
    def invalid(_):raise ProjectionError('La revisión actual requiere un nuevo orden')
    monkeypatch.setattr('app.portfolio.accounting_view.replay_accounting_view',invalid)
    assert current_snapshots((),('a',),object())=={'a':(None,'La revisión actual requiere un nuevo orden')}


def test_strict_authorization_context_and_client_data_do_not_create_records(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch)as case:
        before=counts(case.vault)
        assert case.client.post(case.base+'/preview',json=case.body).status_code==401
        assert case.client.post(case.base+'/preview',headers={**case.headers,'Origin':'https://outside.invalid'},json=case.body).status_code==403
        assert case.client.post(case.base+'/preview',headers=case.headers,json={**case.body,'balances':{'USDT':'999'}}).status_code==422
        wrong={**case.body,'receipts':[{**case.body['receipts'][0],'context_account_id':case.ledger}]}
        assert case.client.post(case.base+'/preview',headers=case.headers,json=wrong).status_code==409
        assert case.client.post(case.base+'/preview',headers=case.headers,json={**case.body,'address':case.meta['operator']}).status_code==409
        missing=case.client.post(case.base+'/result',headers=case.headers,json={'address':case.meta['owner'],'preview_digest':'0'*64})
        assert missing.status_code==200 and not missing.json()['saved']and missing.headers['cache-control']=='no-store'
        assert counts(case.vault)==before


def test_protected_view_with_live_crypto_generation_revokes_the_pending_http_commit(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch)as case:
        preview=case.client.post(case.base+'/preview',headers=case.headers,json=case.body)
        assert preview.status_code==200,preview.text
        before=counts(case.vault);generation=case.vault.session_generation;original=archive._draft
        operational=case.supervisor.operational_status
        case.supervisor.operational_status=lambda:{'active':True,'count':1,'until':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}
        try:
            with ThreadPoolExecutor(max_workers=1)as pool:
                def protect(*args):
                    value=original(*args)
                    response=pool.submit(lambda:case.client.post('/real/vault/protect-view',headers=case.headers)).result(timeout=10)
                    assert response.status_code==200 and response.json()['operating']
                    assert case.vault.session_generation==generation
                    return value
                with monkeypatch.context()as patch:
                    patch.setattr(archive,'_draft',protect)
                    saved=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(case.body,preview.json()))
                assert saved.status_code==401,saved.text
                assert case.vault.unlocked and case.vault.session_generation==generation and counts(case.vault)==before
        finally:
            case.supervisor.operational_status=operational
            result=case.client.post('/real/operations/stop',json={'confirmation':'DETENER_Y_BLOQUEAR'})
            assert result.status_code==200
