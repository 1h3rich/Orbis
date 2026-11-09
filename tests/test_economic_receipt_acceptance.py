"""Atomic receipt facts + accepted method on owned SQLCipher/Anvil only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Context,Decimal as D,ROUND_HALF_EVEN,localcontext
import json
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from app.portfolio import economic_acceptance as archive,economic_receipt_acceptance as batches,wallet_swap_booking
from app.portfolio.domain import CALC_PRECISION,EventInput,PortfolioError
from app.portfolio.economic_receipt_source import ReceiptSelection
from app.portfolio.repository import account_snapshot,append_event,list_original_events,create_account,transfer_asset_between_accounts
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_links import save_wallet_link
from tests.test_dex_call_booking import resolved_case
from tests.test_economic_projection_source import saved_order
from tests.test_economic_receipt_source import capture,counts,stamp
from tests.test_economic_acceptance import ready,proposed,confirm
from tests.test_dex_operator import write
from tests.dex_order_fixture import finalized_wallet_order,same_block_case
from tests.test_dex_receipt_order_journal import arguments,review,save
from tests.test_wallet_swap import PASSWORD


def scope(case,*,selection=None,at=None):
    vault,context,owner,meta,_,_,_,resolved=case
    if selection is None:
        saved_order(case);selection=ReceiptSelection('CALL_RESOLUTION',resolved['id'],context,meta['owner'])
    inputs=capture(vault,owner,[selection])
    registered=max(stamp(inputs),at)if at is not None else stamp(inputs)
    return [selection],{'batch_id':str(uuid4()),'recorded_at':registered}


def preview(case,selections,options):
    return batches.preview_receipt_acceptance(case[0],case[2],selections,**options)


def commit(case,selections,options,proposal):
    return batches.confirm_receipt_acceptance(case[0],case[2],selections,**options,
        preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=1))


def test_real_joint_commit_is_one_method_with_original_receipts_and_recoverable_cipher_backup(tmp_path):
    with resolved_case(tmp_path) as case:
        selections,options=scope(case);before=counts(case[0]);proposal=preview(case,selections,options)
        assert counts(case[0])==before
        assert proposal['draft']['schema']==archive.BATCH_DRAFT
        assert case[3]['owner'].lower()not in json.dumps(proposal).lower()
        saved=commit(case,selections,options,proposal)
        assert saved['created'] and saved['accounting_method_active'] and saved['events_created']==2
        assert saved['receipt_count']==1 and not saved['applied_to_ledger'] and not saved['trading_authority']
        assert counts(case[0])==tuple(a+b for a,b in zip(before,(2,1,0,1)))
        with case[0].session() as db:
            book=account_snapshot(db,case[2]);originals=list_original_events(db,case[2])
            archived=archive.capture_acceptance(db,saved['id'])
        assert book.positions['WBNB','USDT'].quantity==D('1.01') and book.cash['USDT']==D('97.99')
        assert archive.replay_acceptance(archived).projection.snapshots[case[2]]==book
        backup=case[0].backup(tmp_path/'joint-copy.db');restored=RealVault(backup);restored.unlock(PASSWORD)
        try:
            with restored.session() as db:
                assert list_original_events(db,case[2])==originals and account_snapshot(db,case[2])==book
        finally:restored.lock()
        with case[0].session() as db:
            append_event(db,case[2],str(uuid4()),EventInput('CONTRIBUTION','USDT',
                options['recorded_at']+timedelta(seconds=20),amount=D('5')))
        repeated=commit(case,selections,options,proposal)
        assert not repeated['created'] and repeated['events_created']==0 and repeated['new_events_count']==2
        assert repeated['id']==saved['id'] and counts(case[0])[0]==before[0]+3


@pytest.mark.parametrize('new_wallet',[False,True])
def test_earlier_or_new_wallet_receipt_can_join_old_method_without_reproving_old_wallet(tmp_path,new_wallet):
    with ready(tmp_path) as (case,witness):
        vault,context,owner,meta,rpc,_,_,_=case
        alternate={**meta,'owner':meta['operator']}if new_wallet else meta
        if new_wallet:
            write(rpc,meta,meta['usdt'],'mint(address,uint256)',['address','uint256'],[alternate['owner'],10**18])
            write(rpc,alternate,meta['usdt'],'approve(address,uint256)',['address','uint256'],[meta['router'],10**18])
            with vault.session() as db:save_wallet_link(db,owner,alternate['owner'],'0x38','Segunda');db.commit()
        other_case=(*case[:3],alternate,*case[4:]);source,observed=finalized_wallet_order(other_case)
        args=arguments(other_case,source,observed);stored=save(other_case,source,args,review(other_case,source,args))
        # A known manual fact later than this receipt establishes the old
        # accepted economic boundary. Ordinary admission must request review.
        source_at=datetime.fromisoformat(json.loads(source.receipt_json)['block_at'])
        with vault.session() as db:
            append_event(db,owner,str(uuid4()),EventInput('CONTRIBUTION','USDT',source_at+timedelta(seconds=10),amount=D('1')))
        first=confirm(case,witness,proposed(case,witness))
        with pytest.raises(PortfolioError):
            with vault.session() as db:wallet_swap_booking.preview_booking(db,source.id,context_account_id=context,wallet_address=alternate['owner'])
        selection=ReceiptSelection('WALLET_SWAP',source.id,context,alternate['owner'])
        selections,options=scope(case,selection=selection,at=datetime.fromisoformat(first['recorded_at'])+timedelta(seconds=1))
        proposal=preview(case,selections,options)
        assert proposal['draft']['parents'][owner]['id']==first['id']
        saved=commit(case,selections,options,proposal)
        assert saved['created'] and saved['events_created']==2 and counts(vault)[1:]==(1,1,2)
        with vault.session() as db:
            current=account_snapshot(db,owner);archived=archive.capture_acceptance(db,saved['id'])
        assert current.positions['WBNB','USDT'].quantity==D('1.02')
        assert current.cash['USDT']==D('98.98') and current.contributions['USDT']==D('101')
        assert archive.replay_acceptance(archived).projection.snapshots[owner]==current
        assert not commit(case,selections,options,proposal)['created']


def test_post_acceptance_insert_fault_rolls_back_new_events_booking_and_method_together(tmp_path,monkeypatch):
    with resolved_case(tmp_path) as case:
        selections,options=scope(case);proposal=preview(case,selections,options);before=counts(case[0])
        original=archive._store_reviewed_draft
        def fail(*args,**kwargs):original(*args,**kwargs);raise RuntimeError('owned-final-commit-fault')
        with monkeypatch.context() as patch:
            patch.setattr(archive,'_store_reviewed_draft',fail)
            with pytest.raises(RuntimeError,match='owned-final-commit-fault'):commit(case,selections,options,proposal)
        assert counts(case[0])==before
        assert commit(case,selections,options,proposal)['created']


def test_lock_while_replay_is_outside_sql_cancels_the_whole_command(tmp_path,monkeypatch):
    with resolved_case(tmp_path) as case:
        selections,options=scope(case);proposal=preview(case,selections,options);before=counts(case[0]);original=archive._draft
        with ThreadPoolExecutor(max_workers=1) as pool:
            def lock(*args,**kwargs):
                result=original(*args,**kwargs);pool.submit(case[0].lock).result(timeout=5);return result
            monkeypatch.setattr(archive,'_draft',lock)
            with pytest.raises(VaultError):commit(case,selections,options,proposal)
        case[0].unlock(PASSWORD);assert counts(case[0])==before


def test_two_independent_sql_writers_book_and_accept_exactly_once(tmp_path,monkeypatch):
    with resolved_case(tmp_path) as case:
        selections,options=scope(case);proposal=preview(case,selections,options);barrier=Barrier(2);original=archive._draft
        def meet(*args,**kwargs):
            result=original(*args,**kwargs);barrier.wait(timeout=10);return result
        monkeypatch.setattr(archive,'_draft',meet)
        class Independent:
            session_generation=1
            @contextmanager
            def guard_session_generation(self,generation):assert generation==1;yield
            @contextmanager
            def session(self):
                with Session(case[0]._engine)as db:yield db
        def writer(_):
            return batches.confirm_receipt_acceptance(Independent(),case[2],selections,**options,
                preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=1))
        with ThreadPoolExecutor(max_workers=2)as pool:results=list(pool.map(writer,range(2)))
        assert sum(item['created']for item in results)==1 and len({item['id']for item in results})==1
        assert counts(case[0])==(5,1,0,1)


def test_manifest_is_checked_against_original_sources_and_selected_events(tmp_path):
    with resolved_case(tmp_path)as case:
        selections,options=scope(case);proposal=preview(case,selections,options);saved=commit(case,selections,options,proposal)
        with case[0].session()as db:
            archived=archive.capture_acceptance(db,saved['id']);draft=json.loads(archived.payload_json)['draft']
            for field in ('order_record_digest','target_account_id','trade_event_id'):
                bad=deepcopy(draft['receipt_batch']);bad['sources'][0][field]='0'*64 if field=='order_record_digest'else str(uuid4())
                with pytest.raises(PortfolioError):batches.validate_batch_manifest(db,bad,draft['review'],limits=archived.inputs.limits)


def test_actual_mixed_operator_wallet_block_is_committed_as_one_recoverable_batch(tmp_path,monkeypatch):
    with same_block_case(tmp_path,monkeypatch)as case:
        selections=[]
        from app.portfolio import dex_receipt_order as orders
        for source in case.sources:
            args=dict(context_account_id=source.account_id,wallet_address=case.meta['owner'],
                evidence=case.observations[source.key],now=max(case.at,datetime.now(timezone.utc)))
            with case.vault.session()as db:proposal=orders.preview_order(db,source.kind,source.id,**args)
            with case.vault.session()as db:orders.confirm_order(db,source.kind,source.id,preview_digest=proposal['preview_digest'],**args)
            selections.append(ReceiptSelection(source.kind,source.id,source.account_id,case.meta['owner']))
        inputs=capture(case.vault,case.ledger_account,selections)
        options={'batch_id':str(uuid4()),'recorded_at':stamp(inputs)}
        proposal=batches.preview_receipt_acceptance(case.vault,case.ledger_account,selections,**options)
        saved=batches.confirm_receipt_acceptance(case.vault,case.ledger_account,list(reversed(selections)),**options,
            preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=1))
        assert saved['events_created']==4 and saved['receipt_count']==2
        assert counts(case.vault)==(6,1,1,1)
        with case.vault.session()as db:
            book=account_snapshot(db,case.ledger_account);archived=archive.capture_acceptance(db,saved['id'])
        assert book.cash['USDT']+book.positions['WBNB','USDT'].cost_basis==D('100')
        assert book.contributions=={'USDT':D('100'),'BNB':D('0.01')}
        assert archive.replay_acceptance(archived).projection.snapshots[case.ledger_account]==book
        repeated=batches.confirm_receipt_acceptance(case.vault,case.ledger_account,selections,**options,
            preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=2))
        assert not repeated['created'] and repeated['events_created']==0


def test_joint_review_propagates_changed_basis_to_a_recipient_with_its_own_later_sale(tmp_path):
    with ready(tmp_path)as (case,witness):
        vault,context,owner,meta,*_=case
        source,observed=finalized_wallet_order(case);args=arguments(case,source,observed)
        save(case,source,args,review(case,source,args))
        first=confirm(case,witness,proposed(case,witness))
        source_at=datetime.fromisoformat(json.loads(source.receipt_json)['block_at'])
        # Record the old-cost transfer and sale before accepting the earlier
        # pending buy. Both legs must retain their original 300-digit basis.
        transfer_at=source_at+timedelta(seconds=10)
        with vault.session()as db:peer=create_account(db,'Receptora','USDT').id
        with vault.session()as db:
            transfer_asset_between_accounts(db,owner,peer,str(uuid4()),asset='WBNB',quote_currency='USDT',
                quantity=D('0.5'),occurred_at=transfer_at)
        with vault.session()as db:
            append_event(db,peer,str(uuid4()),EventInput('SELL','USDT',transfer_at+timedelta(seconds=1),
                asset='WBNB',quantity=D('0.5'),unit_price=D('3')))
            before=account_snapshot(db,peer);originals=list_original_events(db,peer)
        selection=ReceiptSelection('WALLET_SWAP',source.id,context,meta['owner'])
        selections,options=scope(case,selection=selection,at=datetime.fromisoformat(first['recorded_at'])+timedelta(seconds=1))
        proposal=preview(case,selections,options)
        assert proposal['draft']['parents'][peer]is None and proposal['draft']['parents'][owner]['id']==first['id']
        saved=commit(case,selections,options,proposal)
        with vault.session()as db:
            current=account_snapshot(db,peer);assert list_original_events(db,peer)==originals
            archived=archive.capture_acceptance(db,saved['id'])
        with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
            expected=D('1.5')-D('2.02')/D('1.02')*D('0.5')
        assert current.positions['WBNB','USDT'].realized_pnl==expected
        assert current.positions['WBNB','USDT'].realized_pnl!=before.positions['WBNB','USDT'].realized_pnl
        assert current.contributions=={} and archived.inputs.accounts==tuple(sorted([owner,peer]))
        assert archive.replay_acceptance(archived).projection.snapshots[peer]==current
        vault.backup(tmp_path/'dependent-joint.db')


def test_changed_selection_expired_review_and_changed_book_never_partially_confirm(tmp_path):
    with resolved_case(tmp_path)as case:
        selections,options=scope(case);proposal=preview(case,selections,options);before=counts(case[0])
        with pytest.raises(PortfolioError):commit(case,[replace(selections[0],confirm_new_trade=True)],options,proposal)
        with pytest.raises(PortfolioError,match='caducó'):
            batches.confirm_receipt_acceptance(case[0],case[2],selections,**options,
                preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(minutes=16))
        assert counts(case[0])==before
        with case[0].session()as db:
            append_event(db,case[2],str(uuid4()),EventInput('CONTRIBUTION','USDT',options['recorded_at']+timedelta(seconds=1),amount=D('1')))
        with pytest.raises(PortfolioError,match='cambió'):commit(case,selections,options,proposal)
        assert counts(case[0])==tuple(a+b for a,b in zip(before,(1,0,0,0)))
