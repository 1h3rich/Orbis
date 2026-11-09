"""Gas order -> booked witness -> accepted/current book on owned EVM/SQLCipher."""
from contextlib import contextmanager
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from sqlalchemy import select

from app.portfolio import dex_gas_order as orders, dex_receipt_booking as books, wallet_swap_booking as wallet_books
from app.portfolio import economic_acceptance as archive, models as m
from app.portfolio.economic_gas_source import SCHEMA, capture_booked_gas_order
from app.portfolio.economic_projection import GasOrderClaim, GAS_ENGINE, ProjectionError
from app.portfolio.economic_projection_source import claim_from_witness, capture_booked_order
from app.portfolio.economic_projection_review import ALL_RECEIPTS, GAS_SCHEMA, capture_projection_inputs, review_projection
from app.portfolio.domain import EventInput, PortfolioError
from app.portfolio.repository import create_account, append_event, account_snapshot, list_original_events
from app.portfolio.wallet_funding_credit import _wallet_owner
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.vault import RealVault
from tests.gas_order_fixture import gas_case
from tests.test_wallet_swap import PASSWORD as WALLET_PASSWORD


def next_approval(case):
    from tests.test_dex_call_resolution import prepare
    from app.live_execution.dex_call_resolution import collect_call_resolution
    from app.live_execution.dex_gas_order import collect_order
    from app.portfolio import wallet_swap
    from app.portfolio.dex_gas_order_source import gas_source
    plan,call,_=prepare(case.vault,case.account,case.meta,case.rpc,action='APPROVE',token='WBNB')
    tx_hash=case.rpc('eth_sendTransaction',[plan['transaction']]);observed=collect_call_resolution(case.rpc,call,tx_hash)
    old={key:observed[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
    now=max(datetime.now(timezone.utc),case.scope['now'])+timedelta(seconds=1)
    with case.vault.session() as db:wallet_swap.report_swap(db,plan['id'],account_id=case.account,tx_hash=tx_hash,now=now)
    with case.vault.session() as db:wallet_swap.check_swap(db,plan['id'],account_id=case.account,evidence=old,now=now)
    with case.vault.session() as db:source=gas_source(db,'WALLET_SWAP',plan['id'],wallet_address=case.meta['owner'])
    return SimpleNamespace(**{**vars(case),'kind':'WALLET_SWAP','id':plan['id'],'source':source,
        'scope':{'context_account_id':case.account,'wallet_address':case.meta['owner'],
            'evidence':collect_order(case.rpc,tx_hash),'now':now}})


def register(case):
    with case.vault.session() as db:
        review=orders.preview_gas_order(db,case.kind,case.id,**case.scope)
    with case.vault.session() as db:
        return orders.confirm_gas_order(db,case.kind,case.id,preview_digest=review['preview_digest'],**case.scope)


def book_gas(case):
    args={'context_account_id':case.account,'wallet_address':case.meta['owner']}
    with case.vault.session() as db:
        proposal=(wallet_books.preview_booking(db,case.id,**args) if case.kind=='WALLET_SWAP'
            else books.preview_booking(db,case.kind,case.id,**args))
    assert proposal['available'],proposal
    with case.vault.session() as db:
        kwargs={**args,'expected_revision':proposal['revision'],'preview_digest':proposal['preview_digest'],'now':case.scope['now']}
        return (wallet_books.book_swap(db,case.id,**kwargs) if case.kind=='WALLET_SWAP'
            else books.book_receipt(db,case.kind,case.id,**kwargs))


@contextmanager
def ready(directory,kind='CALL_RESOLUTION',*,typed=False,position_gas=False,booked=True):
    with gas_case(directory,kind,typed=typed) as original:
        with original.vault.session() as db:
            owner=_wallet_owner(db,canonical_wallet_address(original.meta['owner']))
            if owner is None:
                target=create_account(db,'Owned gas wallet','BNB').id
                save_wallet_link(db,target,original.meta['owner'],'0x38','Owned');db.commit()
            else:target=owner.id
        at=datetime.fromisoformat(json.loads(original.source.receipt_json)['block_at'])-timedelta(days=1)
        with original.vault.session() as db:
            append_event(db,target,str(uuid4()),EventInput('CONTRIBUTION','USDT' if position_gas else 'BNB',
                at-timedelta(seconds=1),amount=Decimal('300') if position_gas else Decimal('1')))
        if position_gas:
            with original.vault.session() as db:
                append_event(db,target,str(uuid4()),EventInput('BUY','USDT',at,asset='BNB',quantity=Decimal('1'),unit_price=Decimal('300')))
        case=SimpleNamespace(**vars(original),target=target)
        saved=register(case)
        if not booked:
            yield case,saved
            return
        book_gas(case)
        with case.vault.session() as db:
            changes=db.connection().exec_driver_sql('SELECT total_changes()').scalar_one()
            witness=capture_booked_gas_order(db,saved['id'],wallet_address=case.meta['owner'])
            assert db.connection().exec_driver_sql('SELECT total_changes()').scalar_one()==changes
        yield case,witness


def propose(case,witnesses):
    return archive.preview_acceptance(case.vault,case.target,witnesses,wallet_addresses=[case.meta['owner']])


def accept(case,witnesses,proposal,**kwargs):
    at=max(case.scope['now'],datetime.now(timezone.utc))+timedelta(minutes=1)
    return archive.confirm_acceptance(case.vault,case.target,witnesses,wallet_addresses=[case.meta['owner']],
        preview_digest=proposal['preview_digest'],now=at,**kwargs)


@pytest.mark.parametrize('kind,typed',[(kind,False)for kind in m.GAS_ORDER_SOURCES]+[('DEPLOYMENT',True),('CALL_RESOLUTION',True)])
def test_all_nine_booked_sources_reopen_as_fee_only_claims_and_exact_encrypted_acceptances(tmp_path,kind,typed):
    with ready(tmp_path,kind,typed=typed) as (case,witness):
        assert witness['schema']==SCHEMA and all(event['role']=='GAS' for event in witness['events'])
        with case.vault.session() as db:
            claim=claim_from_witness(db,witness);original=list_original_events(db,case.target);before=account_snapshot(db,case.target)
            assert isinstance(claim,GasOrderClaim) and claim.trade_event_id is None
            inputs=capture_projection_inputs(db,case.target,[witness])
        reviewed=review_projection(inputs)
        assert inputs.source_policy==ALL_RECEIPTS and reviewed.body['schema']==GAS_SCHEMA
        assert reviewed.projection.engine==GAS_ENGINE and reviewed.projection.snapshots[case.target]==before
        proposal=propose(case,[witness]);first=accept(case,[witness],proposal);again=accept(case,[witness],proposal)
        assert first['created'] and not again['created'] and first['id']==again['id']
        with case.vault.session() as db:
            captured=archive.capture_acceptance(db,first['id'])
            assert account_snapshot(db,case.target)==before and list_original_events(db,case.target)==original
        assert archive.replay_acceptance(captured)==reviewed
        assert json.loads(captured.payload_json)['draft']['schema']==archive.GAS_DRAFT
        for private in (case.meta['owner'],case.meta['operator'],case.scope['evidence']['tx_hash']):
            assert private.lower() not in captured.payload_json.lower()
        backup=case.vault.backup(tmp_path/'gas-accepted.db');restored=RealVault(backup);restored.unlock(case.password)
        try:
            with restored.session() as db:
                reopened=archive.capture_acceptance(db,first['id'])
                assert claim_from_witness(db,witness)==claim and account_snapshot(db,case.target)==before
            assert archive.replay_acceptance(reopened)==reviewed
        finally:restored.lock()


def test_new_gas_bookings_require_their_proof_extend_without_wallet_and_preserve_old_prefix(tmp_path):
    from app.portfolio.accounting_view import capture_accounting_inputs, replay_accounting_view
    with ready(tmp_path) as (case,witness):
        first=accept(case,[witness],propose(case,[witness]))
        with case.vault.session() as db:
            accepted=archive.capture_acceptance(db,first['id']);before=account_snapshot(db,case.target)
        later=next_approval(case)
        with pytest.raises(ProjectionError):book_gas(later)
        stored=register(later);book_gas(later)
        with case.vault.session() as db:
            captured=capture_accounting_inputs(db,case.target)
            assert len(captured.extension_claims)==1 and isinstance(captured.extension_claims[0],GasOrderClaim)
            from app.portfolio.economic_receipt_source import existing_receipt_witnesses
            from app.portfolio.economic_projection import ProjectionLimits
            _,witnesses,claims=existing_receipt_witnesses(db,case.target,(case.target,),{},ProjectionLimits())
            assert len(witnesses)==len(claims)==2 and all(isinstance(claim,GasOrderClaim) for claim in claims)
            assert archive.capture_acceptance(db,first['id'])==accepted
            after=account_snapshot(db,case.target)
        assert replay_accounting_view(captured).snapshots[case.target]==after
        fee=Decimal(stored['proof']['gas_fee_wei'])/Decimal(10**18)
        assert before.cash['BNB']-after.cash['BNB']==fee
        assert archive.replay_acceptance(accepted).projection.snapshots[case.target]==before
        with case.vault.session() as db:
            row=db.get(m.RealDexGasOrderProof,stored['id']);db.delete(row);db.commit()
        with case.vault.session() as db:
            with pytest.raises(PortfolioError):account_snapshot(db,case.target)
        register(later)
        with case.vault.session() as db:assert account_snapshot(db,case.target)==after


def test_cold_verification_replays_every_revision_and_authenticates_distinct_receipts_once(tmp_path,monkeypatch):
    from app.portfolio.vault import SCHEMA_VERSION

    def receipt_fee(case):
        receipt = case.scope['evidence']['receipt']
        return Decimal(int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16))/Decimal(10**18)

    with ready(tmp_path) as (case, first_witness):
        witnesses = [first_witness]
        fees = [receipt_fee(case)]
        revisions = []
        stamp = max(case.scope['now'],datetime.now(timezone.utc)) + timedelta(minutes=1)
        for index in range(4):
            if index:
                later = next_approval(case)
                stored = register(later)
                book_gas(later)
                fees.append(receipt_fee(later))
                with case.vault.session() as db:
                    witnesses.append(capture_booked_gas_order(db,stored['id'],wallet_address=case.meta['owner']))
            proposal = propose(case,witnesses)
            accepted = archive.confirm_acceptance(case.vault,case.target,witnesses,
                wallet_addresses=(case.meta['owner'],),preview_digest=proposal['preview_digest'],
                now=stamp+timedelta(seconds=index))
            revisions.append(accepted['id'])
        assert len({item['source_id'] for item in witnesses}) == 4
        copied = case.vault.backup(tmp_path/'verified-distinct-gas.db')
        copied_bytes = copied.read_bytes()
        cold = RealVault(copied)
        cold.unlock(case.password)
        try:
            calls = Counter()
            for name in ('_read_header','claim_from_witness','replay_acceptance'):
                original = getattr(archive,name)
                def count(*args, _name=name, _original=original, **kwargs):
                    calls[_name] += 1
                    return _original(*args,**kwargs)
                monkeypatch.setattr(archive,name,count)
            with cold.session() as db:
                archive.verify_all_acceptances(db,version=SCHEMA_VERSION)
            assert calls == {'_read_header':4,'claim_from_witness':4,'replay_acceptance':4}
            with cold.session() as db:
                expected = Decimal('1.01')  # Both fixture contributions.
                for index, identifier in enumerate(revisions, start=1):
                    revision = archive.capture_acceptance(db,identifier)
                    reproduced = archive.replay_acceptance(revision)
                    expected -= fees[index-1]
                    assert reproduced.body['claimed_transactions'] == index
                    assert reproduced.projection.snapshots[case.target].cash['BNB'] == expected
                assert account_snapshot(db,case.target).cash['BNB'] == expected
        finally:
            cold.lock()
        assert copied.read_bytes() == copied_bytes


def test_explicit_gas_review_preserves_and_links_a_preexisting_trade_only_acceptance(tmp_path):
    from tests.test_economic_acceptance import ready as trade_ready
    from app.portfolio.economic_projection_review import TRADES
    with trade_ready(tmp_path) as (old,trade_witness):
        vault,context,target,meta,rpc,*_=old
        case=SimpleNamespace(vault=vault,account=context,target=target,meta=meta,rpc=rpc,
            scope={'now':datetime.now(timezone.utc)},password=WALLET_PASSWORD)
        gas=next_approval(case);stored=register(gas);book_gas(gas)
        legacy=accept(case,[trade_witness],propose(case,[trade_witness]))
        with vault.session() as db:
            original=archive.capture_acceptance(db,legacy['id'])
            gas_witness=capture_booked_gas_order(db,stored['id'],wallet_address=meta['owner'])
        assert original.inputs.source_policy==TRADES and archive.replay_acceptance(original).body['schema']=='orbis.portfolio.economic.review.v1'
        witnesses=[trade_witness,gas_witness];proposal=propose(gas,witnesses);current=accept(gas,witnesses,proposal)
        with vault.session() as db:
            assert archive.capture_acceptance(db,legacy['id'])==original
            updated=archive.capture_acceptance(db,current['id'])
            assert updated.inputs.source_policy==ALL_RECEIPTS
            assert json.loads(updated.payload_json)['draft']['parents'][target]['id']==legacy['id']
            with pytest.raises(ProjectionError):capture_projection_inputs(db,target,[trade_witness])
        assert archive.replay_acceptance(original).body['schema']=='orbis.portfolio.economic.review.v1'


def test_gas_from_a_bnb_position_keeps_its_original_usdt_cost_without_inventing_native_cash(tmp_path):
    with ready(tmp_path,'DEPLOYMENT',position_gas=True) as (case,witness):
        claim=accept(case,[witness],propose(case,[witness]))
        with case.vault.session() as db:
            snapshot=account_snapshot(db,case.target)
            events=list_original_events(db,case.target)
            archived=archive.capture_acceptance(db,claim['id'])
        receipt=case.scope['evidence']['receipt']
        fee=Decimal(int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16))/Decimal(10**18)
        assert snapshot.positions[('BNB','USDT')].quantity==Decimal('1')-fee
        assert snapshot.positions[('BNB','USDT')].cost_basis==Decimal('300')*(Decimal('1')-fee)
        assert not snapshot.cash.get('BNB') and not snapshot.contributions.get('BNB')
        assert sum(event.input.kind=='BUY' for event in events)==1
        assert archive.replay_acceptance(archived).projection.snapshots[case.target]==snapshot


def test_gas_witness_detects_pending_same_wallet_in_another_context_without_stopping_or_sending(tmp_path):
    from tests.test_dex_call_resolution import prepare
    from app.portfolio.economic_operational_guard import AccountingOperationActive
    from app.portfolio import wallet_swap
    with ready(tmp_path) as (case,witness):
        proposal=propose(case,[witness])
        with case.vault.session() as db:other=create_account(db,'Another own context','USDT').id
        pending,_,_=prepare(case.vault,other,case.meta,case.rpc,action='APPROVE',token='WBNB')
        with pytest.raises(AccountingOperationActive) as caught:accept(case,[witness],proposal)
        assert any(item['kind']=='SWAP' and item['id']==pending['id'] for item in caught.value.report['blockers'])
        with case.vault.session() as db:
            assert wallet_swap.swap_record(db,pending['id'])[0].status=='WALLET_REQUESTED'
            assert not list(db.scalars(select(m.RealEconomicAcceptance)))


def test_gas_acceptance_rolls_back_all_archive_rows_after_flush_without_changing_bookings(tmp_path,monkeypatch):
    with ready(tmp_path) as (case,witness):
        proposal=propose(case,[witness]);original=archive._lineage
        with case.vault.session() as db:before=list_original_events(db,case.target)
        def fail(db,identifier):raise RuntimeError('owned post-flush fault')
        monkeypatch.setattr(archive,'_lineage',fail)
        with pytest.raises(RuntimeError,match='post-flush'):accept(case,[witness],proposal)
        monkeypatch.setattr(archive,'_lineage',original)
        with case.vault.session() as db:
            assert not list(db.scalars(select(m.RealEconomicAcceptance)))
            assert not list(db.scalars(select(m.RealEconomicAcceptanceAccount)))
            assert list_original_events(db,case.target)==before
        assert accept(case,[witness],proposal)['created']


def test_same_block_trade_and_separate_gas_use_actual_indices_without_manual_tie(tmp_path,monkeypatch):
    from tests.gas_order_fixture import mixed_order_case
    from app.portfolio import dex_receipt_order as trades
    with mixed_order_case(tmp_path,monkeypatch) as mixed:
        with mixed.vault.session() as db:
            target=_wallet_owner(db,canonical_wallet_address(mixed.meta['owner'])).id
        gas=SimpleNamespace(vault=mixed.vault,account=mixed.account,target=target,meta=mixed.meta,
            kind='WALLET_SWAP',id=mixed.gas.id,scope=mixed.gas_scope)
        gas_record=register(gas)
        with mixed.vault.session() as db:preview=trades.preview_order(db,mixed.trade.kind,mixed.trade.id,**mixed.trade_scope)
        with mixed.vault.session() as db:
            trade_record=trades.confirm_order(db,mixed.trade.kind,mixed.trade.id,preview_digest=preview['preview_digest'],**mixed.trade_scope)
        args={'context_account_id':mixed.account,'wallet_address':mixed.meta['owner']}
        with mixed.vault.session() as db:proposal=books.preview_booking(db,mixed.trade.kind,mixed.trade.id,**args)
        with mixed.vault.session() as db:
            books.book_receipt(db,mixed.trade.kind,mixed.trade.id,expected_revision=proposal['revision'],
                preview_digest=proposal['preview_digest'],now=mixed.trade_scope['now'],**args)
        book_gas(gas)
        with mixed.vault.session() as db:
            witnesses=[capture_booked_order(db,trade_record['id'],wallet_address=mixed.meta['owner']),
                capture_booked_gas_order(db,gas_record['id'],wallet_address=mixed.meta['owner'])]
            inputs=capture_projection_inputs(db,target,witnesses)
        reviewed=review_projection(inputs)
        nodes=list(dict.fromkeys(event.node_id for event in reviewed.projection.events if event.node_id.startswith('chain:')))
        assert nodes==['chain:'+record['id'] for record in sorted([gas_record,trade_record],key=lambda r:int(r['transaction_index']))]
        result=accept(gas,witnesses,propose(gas,witnesses))
        with mixed.vault.session() as db:captured=archive.capture_acceptance(db,result['id'])
        assert archive.replay_acceptance(captured)==reviewed


def test_existing_gas_method_is_reviewable_by_authenticated_http_without_rpc(tmp_path):
    from eth_account import Account
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.portfolio.api import create_real_router
    from tests.test_wallet_funding_api import prove
    with ready(tmp_path) as (case,witness):
        accept(case,[witness],propose(case,[witness]));case.vault.lock()
        def no_rpc():raise AssertionError('Existing review must not observe or send')
        app=FastAPI();app.include_router(create_real_router(case.vault.path,dex_rpc_factory=no_rpc))
        with TestClient(app) as client:
            result=client.post('/real/vault/unlock',json={'passphrase':case.password});assert result.status_code==200,result.text
            headers={'X-Orbis-Vault-Token':result.json()['token']}
            try:
                prove(client,headers,case.account,Account.from_key('0x'+'11'*32))
                result=client.post(f'/real/accounts/{case.account}/economic-review/preview',headers=headers,
                    json={'address':case.meta['owner']})
                assert result.status_code==200,result.text
                body=result.json();assert body['mode']=='EXISTING' and body['review']['checked_receipts']==1
                item=body['review']['accounts'][0]
                assert item['current']==item['network'] and item['current_error'] is None
                assert result.headers['cache-control']=='no-store' and not body['trading_authority']
            finally:client.post('/real/vault/lock',headers=headers)


def test_gas_witness_rejects_trade_profile_forgery_and_changed_original_registration(tmp_path):
    with ready(tmp_path) as (case,witness):
        with case.vault.session() as db:
            claim=claim_from_witness(db,witness)
            with pytest.raises(PortfolioError):capture_booked_order(db,witness['order_record_id'],wallet_address=case.meta['owner'])
            with pytest.raises(PortfolioError):capture_booked_gas_order(db,witness['order_record_id'],wallet_address=case.meta['operator'])
        for key,value in [('schema','orbis.portfolio.booked.order.witness.v1'),('account_id',case.account),('owner_digest','f'*64),('booking_digest','0'*64)]:
            with case.vault.session() as db:
                with pytest.raises(PortfolioError):claim_from_witness(db,{**witness,key:value})
        forged=deepcopy(witness);forged['events'][0]['role']='TRADE'
        with case.vault.session() as db:
            with pytest.raises(PortfolioError):claim_from_witness(db,forged)
            row=db.get(m.RealEvent,claim.gas_event_ids[0]);stamp=row.recorded_at;row.recorded_at+=timedelta(seconds=1);db.commit()
        with case.vault.session() as db:
            with pytest.raises(PortfolioError):claim_from_witness(db,witness)
            db.get(m.RealEvent,claim.gas_event_ids[0]).recorded_at=stamp;db.commit()
        with case.vault.session() as db:assert claim_from_witness(db,witness)==claim
