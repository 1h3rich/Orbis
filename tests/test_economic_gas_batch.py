"""Atomic fee-only/mixed receipt batches on actual owned Anvil/SQLCipher."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4
import json

import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.portfolio import economic_acceptance as archive,economic_receipt_acceptance as batches,models as m
from app.portfolio.accounting_view import AccountingReadIssue,capture_review_baseline,validate_review_baseline
from app.portfolio.domain import EventInput,PortfolioError
from app.portfolio.economic_projection import GasOrderClaim,ProjectionError
from app.portfolio.economic_receipt_source import ReceiptSelection,capture_receipt_batch
from app.portfolio.economic_review_api import current_snapshots
from app.portfolio.repository import append_event,account_snapshot,list_original_events
from app.portfolio.vault import RealVault
from tests.test_economic_gas_acceptance import ready,next_approval,register,book_gas,propose,accept


def test_mixed_same_block_receipts_commit_trade_and_both_fees_as_one_review(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from tests.gas_order_fixture import mixed_order_case
    from app.portfolio import dex_receipt_order as trades
    from app.portfolio.wallet_funding_credit import _wallet_owner
    from app.portfolio.wallet_proof import canonical_wallet_address
    with mixed_order_case(tmp_path,monkeypatch) as original:
        with original.vault.session() as db:target=_wallet_owner(db,canonical_wallet_address(original.meta['owner'])).id
        case=SimpleNamespace(vault=original.vault,target=target,account=original.account,meta=original.meta,
            kind=original.gas.kind,id=original.gas.id,scope=original.gas_scope)
        register(case)
        with original.vault.session() as db:p=trades.preview_order(db,original.trade.kind,original.trade.id,**original.trade_scope)
        with original.vault.session() as db:trades.confirm_order(db,original.trade.kind,original.trade.id,preview_digest=p['preview_digest'],**original.trade_scope)
        selected=[selection(case),ReceiptSelection(original.trade.kind,original.trade.id,original.account,original.meta['owner'])]
        options=scope(case,selected);before=counts(case.vault);proposal=preview(case,selected,options)
        assert counts(case.vault)==before and proposal['draft']['review']['claimed_transactions']==2
        flags={spec['source_kind']:spec['gas_only'] for spec in proposal['draft']['receipt_batch']['sources']}
        assert flags=={'WALLET_SWAP':True,'OPERATOR':False}
        result=commit(case,selected,options,proposal)
        assert result['receipt_count']==2 and result['events_created']==3
        with original.vault.session() as db:
            archived=archive.capture_acceptance(db,result['id']);snapshot=account_snapshot(db,target)
        assert archive.replay_acceptance(archived).projection.snapshots[target]==snapshot


@pytest.mark.parametrize('kind',['CALL_RESOLUTION','GAS_FUNDING'])
def test_http_gas_batch_preview_confirm_lost_reply_and_existing_method_use_no_rpc(tmp_path,kind):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from eth_account import Account
    from app.live_execution.dex_bot_supervisor import DexBotSupervisor
    from app.portfolio.api import create_real_router
    from tests.test_wallet_funding_api import prove
    from tests.test_economic_review_api import confirmation
    with ready(tmp_path,kind,booked=False) as (case,_):
        case.vault.lock();start=datetime.now(timezone.utc);base=max(start,case.scope['now'])+timedelta(seconds=3)
        def supervisor(vault,connection):return DexBotSupervisor(vault,connection,clock=lambda:base+(datetime.now(timezone.utc)-start))
        def no_rpc():raise AssertionError('Review must not observe, sign or send')
        app=FastAPI();app.include_router(create_real_router(case.vault.path,dex_rpc_factory=no_rpc,dex_bot_supervisor_factory=supervisor))
        with TestClient(app) as client:
            response=client.post('/real/vault/unlock',json={'passphrase':case.password});assert response.status_code==200,response.text
            headers={'X-Orbis-Vault-Token':response.json()['token']};url=f'/real/accounts/{case.account}/economic-review'
            try:
                prove(client,headers,case.account,Account.from_key('0x'+'11'*32))
                body={'address':case.meta['owner'],'receipts':[{'source_kind':kind,'source_id':case.id,'gas_only':True}]}
                invalid={**body,'receipts':[{**body['receipts'][0],'confirm_new_trade':True}]}
                assert client.post(url+'/preview',headers=headers,json=invalid).status_code==422
                response=client.post(url+'/preview',headers=headers,json=body);assert response.status_code==200,response.text
                data=response.json();assert data['mode']=='BATCH' and data['review']['events_to_create']==1
                response=client.post(url+'/confirm',headers=headers,json=confirmation(body,data));assert response.status_code==200,response.text
                assert response.json()['created'] and response.json()['receipt_count']==1
                response=client.post(url+'/result',headers=headers,json={'address':case.meta['owner'],'preview_digest':data['preview_digest']})
                assert response.status_code==200 and response.json()['saved'] and response.json()['receipt_batch']
                response=client.post(url+'/confirm',headers=headers,json=confirmation(body,data))
                assert response.status_code==200 and not response.json()['created']
                simple={'address':case.meta['owner']};response=client.post(url+'/preview',headers=headers,json=simple)
                assert response.status_code==200,response.text
                existing=response.json();assert existing['mode']=='EXISTING'
                response=client.post(url+'/confirm',headers=headers,json=confirmation(simple,existing))
                assert response.status_code==200,response.text
                assert response.headers['cache-control']=='no-store'
            finally:client.post('/real/vault/lock',headers=headers)


def counts(vault):
    with vault.session() as db:
        return tuple(db.scalar(select(func.count()).select_from(cls)) for cls in
            (m.RealEvent,m.RealWalletSwapBooking,m.RealDexReceiptBooking,m.RealEconomicAcceptance,m.RealEconomicAcceptanceAccount))


def selection(case,**kwargs):
    return ReceiptSelection(case.kind,case.id,case.account,case.meta['owner'],gas_only=True,**kwargs)


def scope(case,selections):
    with case.vault.session() as db:inputs=capture_receipt_batch(db,case.target,selections)
    parents=[datetime.fromisoformat(json.loads(item.payload_json)['recorded_at']) for item in inputs.current.accepted]if inputs.current else []
    stamp=max(datetime.now(timezone.utc),*(e.recorded_at for e in inputs.events),*(r.not_before for r in inputs.receipts),*parents)+timedelta(seconds=1)
    return {'batch_id':str(uuid4()),'recorded_at':stamp}


def preview(case,selections,options):
    return batches.preview_receipt_acceptance(case.vault,case.target,selections,**options)


def commit(case,selections,options,proposal,vault=None):
    return batches.confirm_receipt_acceptance(vault or case.vault,case.target,selections,**options,
        preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=1))


@pytest.mark.parametrize('kind,typed',[(kind,False)for kind in m.GAS_ORDER_SOURCES]+[('DEPLOYMENT',True),('CALL_RESOLUTION',True)])
def test_every_unbooked_gas_source_stages_without_writes_and_commits_original_fee_once(tmp_path,kind,typed):
    with ready(tmp_path,kind,typed=typed,booked=False) as (case,_):
        selected=[selection(case)];options=scope(case,selected);before=counts(case.vault)
        with case.vault.session() as db:original_ids={event.id for event in list_original_events(db,case.target)}
        proposal=preview(case,selected,options)
        assert counts(case.vault)==before
        draft=proposal['draft'];assert draft['schema']==archive.GAS_BATCH_DRAFT
        manifest=draft['receipt_batch'];assert manifest['schema']==batches.GAS_BATCH_SCHEMA
        assert manifest['sources'][0]['gas_only']
        assert all(ref['role']=='GAS' for receipt in manifest['receipts'] for ref in receipt['events'])
        first=commit(case,selected,options,proposal);again=commit(case,selected,options,proposal)
        assert first['created'] and not again['created'] and first['id']==again['id']
        assert first['events_created']==1 and again['events_created']==0
        with case.vault.session() as db:
            archived=archive.capture_acceptance(db,first['id']);events=list_original_events(db,case.target)
            assert isinstance(archived.inputs.claims[0],GasOrderClaim)
            assert all(event.input.kind=='FEE' for event in events if event.id not in original_ids)
            snapshot=account_snapshot(db,case.target)
        assert archive.replay_acceptance(archived).projection.snapshots[case.target]==snapshot
        backup=case.vault.backup(tmp_path/'atomic-gas.db');restored=RealVault(backup);restored.unlock(case.password)
        try:
            with restored.session() as db:
                assert archive.capture_acceptance(db,first['id'])==archived
                assert account_snapshot(db,case.target)==snapshot
        finally:restored.lock()
        for secret in (case.meta['owner'],case.meta['operator'],case.scope['evidence']['tx_hash']):
            assert secret.lower() not in archived.payload_json.lower()
        at=datetime.fromisoformat(json.loads(case.source.receipt_json)['block_at'])+timedelta(seconds=10)
        with case.vault.session() as db:append_event(db,case.target,str(uuid4()),EventInput('CONTRIBUTION','BNB',at,amount=Decimal('0.5')))
        assert not commit(case,selected,options,proposal)['created']


def test_legacy_financial_profile_cannot_be_retried_as_a_fee_or_create_an_artificial_trade(tmp_path):
    with ready(tmp_path,booked=False) as (case,_):
        with pytest.raises(PortfolioError):scope(case,[replace(selection(case),gas_only=False)])
        with pytest.raises(PortfolioError):replace(selection(case),trade_event_id=str(uuid4()))
        with pytest.raises(PortfolioError):replace(selection(case),confirm_new_trade=True)
        with pytest.raises(PortfolioError):ReceiptSelection('DEPLOYMENT',case.id,case.account,case.meta['owner'])


def test_two_independent_connections_commit_one_atomic_gas_batch(tmp_path,monkeypatch):
    with ready(tmp_path,booked=False) as (case,_):
        selected=[selection(case)];options=scope(case,selected);proposal=preview(case,selected,options)
        barrier=Barrier(2);original=archive._draft
        def meet(*args,**kwargs):
            result=original(*args,**kwargs);barrier.wait(timeout=10);return result
        monkeypatch.setattr(archive,'_draft',meet)
        class Independent:
            session_generation=1
            @contextmanager
            def guard_session_generation(self,generation):assert generation==1;yield
            @contextmanager
            def session(self):
                with Session(case.vault._engine) as db:yield db
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:commit(case,selected,options,proposal,Independent()),range(2)))
        assert sorted(item['created'] for item in results)==[False,True]
        assert len({item['id'] for item in results})==1
        assert counts(case.vault)[1:]==(0,1,1,1)


def test_post_materialization_failure_rolls_back_fee_bookings_and_acceptance_together(tmp_path,monkeypatch):
    with ready(tmp_path,booked=False) as (case,_):
        selected=[selection(case)];options=scope(case,selected);proposal=preview(case,selected,options);before=counts(case.vault)
        def fail(*args,**kwargs):raise RuntimeError('owned archive fault')
        monkeypatch.setattr(archive,'_store_reviewed_draft',fail)
        with pytest.raises(RuntimeError,match='owned archive fault'):commit(case,selected,options,proposal)
        assert counts(case.vault)==before


def test_partial_existing_gas_is_reused_exactly_and_versioned_manifest_rejects_profile_changes(tmp_path):
    from app.portfolio.economic_projection import ProjectionLimits
    with ready(tmp_path,booked=False) as (case,_):
        at=datetime.fromisoformat(json.loads(case.source.receipt_json)['block_at'])
        with case.vault.session() as db:
            existing=append_event(db,case.target,str(uuid4()),EventInput('FEE','BNB',at,
                amount=Decimal('0.000000000000000001'),note='Owned partial receipt gas'))
        selected=[selection(case,gas_event_ids=(existing.id,))];options=scope(case,selected);proposal=preview(case,selected,options)
        result=commit(case,selected,options,proposal)
        with case.vault.session() as db:
            archived=archive.capture_acceptance(db,result['id']);draft=json.loads(archived.payload_json)['draft']
            assert result['events_created']==1
            refs=draft['receipt_batch']['receipts'][0]['events']
            assert len(refs)==2 and any(ref['id']==existing.id and ref['existing'] for ref in refs)
            for change in ('wrong_profile','missing_flag','fake_trade','wrong_role'):
                batch=deepcopy(draft['receipt_batch'])
                if change=='wrong_profile':batch['sources'][0]['gas_only']=False
                elif change=='missing_flag':batch['sources'][0].pop('gas_only')
                elif change=='fake_trade':batch['sources'][0]['trade_event_id']=existing.id
                else:batch['receipts'][0]['events'][0]['role']='TRADE'
                with pytest.raises(PortfolioError):batches.validate_batch_manifest(db,batch,draft['review'],limits=ProjectionLimits())


def test_new_financial_batch_preserves_existing_gas_policy_and_its_immutable_prefix(tmp_path):
    from tests.dex_order_fixture import finalized_wallet_order
    from app.portfolio import dex_receipt_order as trades
    with ready(tmp_path) as (case,witness):
        first=accept(case,[witness],propose(case,[witness]))
        with case.vault.session() as db:previous=archive.capture_acceptance(db,first['id'])
        values=(case.vault,case.account,case.target,case.meta,case.rpc,None,None,None)
        source,evidence=finalized_wallet_order(values)
        args={'context_account_id':case.account,'wallet_address':case.meta['owner'],
            'evidence':evidence,'now':datetime.now(timezone.utc)+timedelta(seconds=1)}
        with case.vault.session() as db:p=trades.preview_order(db,source.kind,source.id,**args)
        with case.vault.session() as db:trades.confirm_order(db,source.kind,source.id,preview_digest=p['preview_digest'],**args)
        selected=[ReceiptSelection(source.kind,source.id,case.account,case.meta['owner'])]
        options=scope(case,selected);proposal=preview(case,selected,options)
        assert proposal['draft']['schema']==archive.GAS_BATCH_DRAFT
        assert proposal['draft']['receipt_batch']['sources'][0]['gas_only']is False
        result=commit(case,selected,options,proposal)
        assert result['events_created']==2
        with case.vault.session() as db:
            assert archive.capture_acceptance(db,first['id'])==previous
            assert archive.capture_acceptance(db,result['id']).inputs.source_policy=='dex-trades-and-gas-2'


def test_backdated_later_original_can_be_explicitly_reviewed_without_falling_back_to_raw_balance(tmp_path):
    with ready(tmp_path) as (case,witness):
        old=accept(case,[witness],propose(case,[witness]))
        block_at=datetime.fromisoformat(json.loads(case.source.receipt_json)['block_at'])
        item=EventInput('CONTRIBUTION','BNB',block_at-timedelta(seconds=1),amount=Decimal('2'))
        with case.vault.session() as db:
            with pytest.raises(PortfolioError):append_event(db,case.target,str(uuid4()),item)
        # Model a restored original from before the current append guard. The
        # production guard remains enforced; only this owned fixture inserts it.
        from app.portfolio.repository import _digest
        with case.vault.session() as db:
            db.add(m.RealEvent(id=str(uuid4()),account_id=case.target,request_id=str(uuid4()),payload_digest=_digest(item),
                kind=item.kind,asset=item.asset,quote_currency=item.quote_currency,quantity=item.quantity,
                unit_price=item.unit_price,gross_amount=item.gross_amount,fee=item.fee,fee_asset=item.fee_asset,
                occurred_at=item.occurred_at,recorded_at=datetime.now(timezone.utc),note=item.note));db.commit()
        with case.vault.session() as db:
            with pytest.raises(PortfolioError):account_snapshot(db,case.target)
            baseline=capture_review_baseline(db,case.target)
            actual=current_snapshots(list_original_events(db,case.target),(case.target,),baseline)
            assert actual[case.target][0] is None and actual[case.target][1]
            previous=archive.capture_acceptance(db,old['id'])
        later=next_approval(case);register(later)
        selected=[selection(later)];options=scope(later,selected);proposal=preview(later,selected,options)
        result=commit(later,selected,options,proposal)
        with case.vault.session() as db:
            assert archive.capture_acceptance(db,old['id'])==previous
            assert archive.capture_acceptance(db,result['id']).inputs.source_policy=='dex-trades-and-gas-2'
            assert account_snapshot(db,case.target).cash['BNB']>Decimal('2')


def test_missing_extension_proof_is_a_read_issue_but_corrupted_accepted_prefix_is_fatal(tmp_path):
    with ready(tmp_path) as (case,witness):
        accepted=accept(case,[witness],propose(case,[witness]));later=next_approval(case);stored=register(later);book_gas(later)
        with case.vault.session() as db:
            row=db.get(m.RealDexGasOrderProof,stored['id']);db.delete(row);db.commit()
        with case.vault.session() as db:
            issue=capture_review_baseline(db,case.target)
            assert isinstance(issue,AccountingReadIssue) and issue.accepted[0].payload_json
            events=list_original_events(db,case.target)
        validate_review_baseline(issue)
        assert current_snapshots(events,(case.target,),issue)[case.target]==(None,issue.error)
        with case.vault.session() as db:
            row=db.get(m.RealEconomicAcceptance,accepted['id']);row.payload_digest='f'*64;db.commit()
        with case.vault.session() as db:
            with pytest.raises(PortfolioError):capture_review_baseline(db,case.target)
