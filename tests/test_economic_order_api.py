"""Private chronology choices with genuine signed receipts and own SQLCipher."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from uuid import uuid4
import json

import pytest
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event,list_original_events,account_snapshot
from tests.test_economic_review_api import api_case,confirmation
from tests.test_economic_receipt_source import counts
from tests.test_economic_gas_catalogue import http_case as gas_case


def contribution(case,account,at):
    with case.vault.session()as db:
        return append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','USDT',at,amount=D('1'))).id


def get_options(case,base,body):
    response=case.client.post(base+'/order-options',headers=case.headers,json=body)
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    for address in (case.meta['owner'],case.meta['operator']):assert address.lower()not in response.text.lower()
    return response.json()


def explicit(body,data):
    return {**body,'order_options_digest':data['revision'],'tie_orders':[
        {'account_id':group['account_id'],'at':group['at'],'nodes':[node['id']for node in group['nodes']]}
        for group in data['groups']]}


def test_new_signed_batch_order_precedes_allocation_and_survives_confirm_recovery(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch,'OPERATOR')as case:
        source=case.client.post(case.base+'/sources',headers=case.headers,json={'address':case.meta['owner']}).json()['sources'][0]
        manual=contribution(case,case.ledger,datetime.fromisoformat(source['occurred_at']))
        before=counts(case.vault);case.gate['failure']=True;calls=len(case.calls)
        data=get_options(case,case.base,case.body)
        group=next(g for g in data['groups']if 'event:'+manual in [n['id']for n in g['nodes']])
        assert len(group['chain_order'])==2 and len(group['nodes'])==3
        body=explicit(case.body,data)
        review=case.client.post(case.base+'/preview',headers=case.headers,json=body)
        assert review.status_code==200,review.text
        assert counts(case.vault)==before and len(case.calls)==calls
        reversed_body={**body,'tie_orders':[{**g,'nodes':list(reversed(g['nodes']))}for g in body['tie_orders']]}
        bad=case.client.post(case.base+'/preview',headers=case.headers,json=reversed_body)
        assert bad.status_code==409 and counts(case.vault)==before
        saved=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(body,review.json()))
        assert saved.status_code==200,saved.text
        assert saved.json()['created']
        repeated=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(body,review.json()))
        assert repeated.status_code==200 and not repeated.json()['created']
        existing={'address':case.meta['owner']}
        options=get_options(case,case.base,existing)
        assert all(g['accepted']for g in options['groups'])
        again=case.client.post(case.base+'/preview',headers=case.headers,json=explicit(existing,options))
        assert again.status_code==200,again.text
        assert all(item['current']==item['network']for item in again.json()['review']['accounts'])


def test_changed_book_or_selection_rejects_old_options_and_rolls_back_staging(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch)as case:
        data=get_options(case,case.base,case.body);body=explicit(case.body,data)
        contribution(case,case.ledger,case.supervisor.now()+timedelta(seconds=1))
        before=counts(case.vault)
        response=case.client.post(case.base+'/preview',headers=case.headers,json=body)
        assert response.status_code==409 and 'actualiza el orden'in response.text,response.text
        assert counts(case.vault)==before
        data=get_options(case,case.base,case.body);body=explicit(case.body,data)
        changed={**body,'receipts':[{**r,'confirm_new_trade':True}for r in body['receipts']]}
        response=case.client.post(case.base+'/preview',headers=case.headers,json=changed)
        assert response.status_code==409 and 'actualiza el orden'in response.text,response.text
        assert counts(case.vault)==before


@pytest.mark.parametrize('booked',[False,True])
def test_gas_choices_have_no_artificial_trade_and_existing_review_needs_no_new_rows(tmp_path,booked):
    with gas_case(tmp_path,booked=booked)as case:
        at=datetime.fromisoformat(json.loads(case.source.receipt_json)['block_at'])
        manual=contribution(case,case.target,at)
        body={'address':case.meta['owner'],'include_gas':True}
        if not booked:body['receipts']=[{'source_kind':case.kind,'source_id':case.id,'gas_only':True}]
        with case.vault.session()as db:before=list_original_events(db,case.target);snapshot=account_snapshot(db,case.target)
        data=get_options(case,case.url,body);group=next(g for g in data['groups']if 'event:'+manual in [n['id']for n in g['nodes']])
        node=next(n for n in group['nodes']if n['type']=='CHAIN')
        assert node['kind']=='FEE'and node['asset']=='BNB'and node['quantity']is None and node['amount']is None
        selected=explicit(body,data)
        review=case.client.post(case.url+'/preview',headers=case.headers,json=selected)
        assert review.status_code==200,review.text
        assert review.json()['review']['events_to_create']==(0 if booked else 1)
        saved=case.client.post(case.url+'/confirm',headers=case.headers,json=confirmation(selected,review.json()))
        assert saved.status_code==200,saved.text
        with case.vault.session()as db:
            after=list_original_events(db,case.target)
            assert all(row in after for row in before)
            assert len(after)==len(before)+(0 if booked else 1)
            assert account_snapshot(db,case.target).positions==snapshot.positions


def test_options_are_strict_private_and_lock_can_run_during_detached_calculation(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch)as case:
        assert case.client.post(case.base+'/order-options',json=case.body).status_code==401
        assert case.client.post(case.base+'/order-options',headers=case.headers,json={**case.body,'balances':{}}).status_code==422
        assert case.client.post(case.base+'/order-options',headers=case.headers,json={**case.body,'order_options_digest':'wrong'}).status_code==422
        from app.portfolio import economic_order_options as module
        original=module.order_options
        with ThreadPoolExecutor(max_workers=1)as pool:
            def protected(*args,**kwargs):
                result=original(*args,**kwargs)
                response=pool.submit(lambda:case.client.post('/real/vault/lock',headers=case.headers)).result(timeout=10)
                assert response.status_code==200
                return result
            monkeypatch.setattr(module,'order_options',protected)
            response=case.client.post(case.base+'/order-options',headers=case.headers,json=case.body)
        assert response.status_code==401,response.text
        assert not case.vault.unlocked


def test_existing_current_cycle_is_reviewable_without_a_raw_balance_fallback(tmp_path,monkeypatch):
    from sqlalchemy import select
    from app.portfolio import economic_acceptance as archive,models as m
    from app.portfolio.economic_projection import ProjectionError
    from app.portfolio.repository import create_account,transfer_cash_between_accounts
    with api_case(tmp_path,monkeypatch)as case:
        preview=case.client.post(case.base+'/preview',headers=case.headers,json=case.body)
        assert preview.status_code==200,preview.text
        first=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(case.body,preview.json()))
        assert first.status_code==200,first.text
        with case.vault.session()as db:
            original_prefix=archive.capture_acceptance(db,first.json()['id'])
            original_cash=account_snapshot(db,case.ledger).cash.copy()
            peer=create_account(db,'Otra cuenta propia','USDT').id;db.commit()
        source=case.client.post(case.base+'/sources',headers=case.headers,json={'address':case.meta['owner']}).json()['sources'][0]
        at=datetime.fromisoformat(source['occurred_at'])+timedelta(seconds=2)
        contribution(case,peer,at-timedelta(days=1))
        requests=[]
        for left,right in ((case.ledger,peer),(peer,case.ledger)):
            request=str(uuid4());requests.append(request)
            with case.vault.session()as db:
                transfer_cash_between_accounts(db,left,right,request,currency='USDT',amount=D('0.1'),occurred_at=at,note='Traslado propio')
        # Only this disposable restored-tail fixture changes registration
        # metadata. No public writer bypass and no accepted original changes.
        with case.vault.session()as db:
            stamp=datetime.now(timezone.utc)
            for row in db.scalars(select(m.RealEvent).where(m.RealEvent.request_id.in_(requests))):
                row.recorded_at=stamp+timedelta(microseconds=1 if row.kind=='CASH_TRANSFER_IN'else 2)
            db.commit()
        with case.vault.session()as db:
            with pytest.raises(ProjectionError,match='ciclo'):account_snapshot(db,case.ledger)
            originals=list_original_events(db,case.ledger)
        body={'address':case.meta['owner']};data=get_options(case,case.base,body)
        group=next(g for g in data['groups']if g['account_id']==case.ledger and g['at']==at.isoformat())
        outgoing=next(n['id']for n in group['nodes']if n['kind']=='CASH_TRANSFER_OUT')
        incoming=next(n['id']for n in group['nodes']if n['kind']=='CASH_TRANSFER_IN')
        selected={**body,'order_options_digest':data['revision'],'tie_orders':[{'account_id':case.ledger,'at':group['at'],'nodes':[outgoing,incoming]}]}
        review=case.client.post(case.base+'/preview',headers=case.headers,json=selected)
        assert review.status_code==200,review.text
        actual=next(item for item in review.json()['review']['accounts']if item['account_id']==case.ledger)
        assert actual['current']is None and 'ciclo'in actual['current_error']
        assert review.json()['review']['events_to_create']==0
        saved=case.client.post(case.base+'/confirm',headers=case.headers,json=confirmation(selected,review.json()))
        assert saved.status_code==200,saved.text
        with case.vault.session()as db:
            assert list_original_events(db,case.ledger)==originals
            assert account_snapshot(db,case.ledger).cash==original_cash
            assert account_snapshot(db,peer).cash['USDT']==D('1')
            assert archive.capture_acceptance(db,first.json()['id'])==original_prefix
