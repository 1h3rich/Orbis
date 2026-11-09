"""Rollback-only staging authenticates actual original rows, not prepared balances."""
from dataclasses import replace
from datetime import datetime,timedelta
from decimal import Context,Decimal as D,ROUND_FLOOR,localcontext
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from app.portfolio.domain import EventInput,PortfolioError
from app.portfolio.economic_receipt_source import ReceiptSelection,prepare_receipt_batch
from app.portfolio.economic_receipt_stage import stage_receipt_preview
from app.portfolio.economic_projection_review import review_projection
from app.portfolio.models import RealDexReceiptBooking
from app.portfolio.repository import append_event
from app.portfolio.vault import VaultError
from tests.test_dex_call_booking import resolved_case
from tests.test_economic_projection_source import saved_order
from tests.test_economic_receipt_source import capture,counts,stamp


def prepared(case,**selected):
    vault,context,owner,meta,_,_,_,resolved=case
    saved_order(case)
    selections=[ReceiptSelection('CALL_RESOLUTION',resolved['id'],context,meta['owner'],**selected)]
    inputs=capture(vault,owner,selections);generation=vault.session_generation
    plan=prepare_receipt_batch(inputs,batch_id=str(uuid4()),recorded_at=stamp(inputs))
    return inputs,selections,plan,generation


def test_real_staged_records_are_authenticated_then_rolled_back_before_replay(tmp_path):
    with resolved_case(tmp_path,'SELL') as case:
        inputs,selections,plan,generation=prepared(case);before=counts(case[0])
        staged=stage_receipt_preview(case[0],inputs,selections,plan,generation=generation)
        assert counts(case[0])==before
        assert len(staged.projection.events)==before[0]+2 and len(staged.projection.claims)==1
        assert case[3]['owner'].lower() not in staged.manifest_json.lower()
        case[0].lock()
        review=review_projection(staged.projection)
        assert review.projection.snapshots==plan.projection.snapshots
        assert review.body['applied_to_ledger'] is False and review.body['trading_authority'] is False
        assert review.projection.snapshots[case[2]].positions['WBNB','USDT'].quantity==D('0.99')


def test_staging_existing_manual_trade_and_split_native_fee_preserves_originals(tmp_path):
    with resolved_case(tmp_path,'BUY') as case:
        vault,_,owner,_,_,_,_,resolved=case
        at=datetime.fromisoformat(resolved['receipt']['block_at'])
        with vault.session() as db:
            append_event(db,owner,str(uuid4()),EventInput('WITHDRAWAL','BNB',at-timedelta(minutes=2),amount=D('0.009999999999999999')))
        with vault.session() as db:
            append_event(db,owner,str(uuid4()),EventInput('BUY','USDT',at-timedelta(minutes=2),asset='BNB',quantity=D('1'),unit_price=D('1')))
        with vault.session() as db:
            linked=append_event(db,owner,str(uuid4()),EventInput('BUY','USDT',at-timedelta(minutes=1),asset='WBNB',quantity=D('0.01'),unit_price=D('1'),note='Manual'))
        inputs,selections,plan,generation=prepared(case,trade_event_id=linked.id);before=counts(vault)
        assert len(plan.created_events)==2 and all(row.input.kind=='FEE'for row in plan.created_events)
        with localcontext(Context(prec=6,rounding=ROUND_FLOOR)):
            staged=stage_receipt_preview(vault,inputs,selections,plan,generation=generation)
        assert counts(vault)==before
        reused=next(row for row in staged.projection.events if row.id==linked.id)
        assert reused==linked
        assert review_projection(staged.projection).projection.snapshots==plan.projection.snapshots


def test_failure_after_booking_insert_rolls_back_events_and_receipt_together(tmp_path):
    with resolved_case(tmp_path) as case:
        inputs,selections,plan,generation=prepared(case);before=counts(case[0])
        def fail(*args):raise RuntimeError('owned-stage-fault')
        sql_event.listen(RealDexReceiptBooking,'after_insert',fail)
        try:
            with pytest.raises(RuntimeError,match='owned-stage-fault'):
                stage_receipt_preview(case[0],inputs,selections,plan,generation=generation)
        finally:sql_event.remove(RealDexReceiptBooking,'after_insert',fail)
        assert counts(case[0])==before


def test_changed_book_and_locked_generation_cancel_staging_without_new_receipt(tmp_path):
    with resolved_case(tmp_path) as case:
        inputs,selections,plan,generation=prepared(case)
        with case[0].session() as db:
            append_event(db,case[2],str(uuid4()),EventInput('CONTRIBUTION','USDT',inputs.receipts[0].order.block_at+timedelta(seconds=1),amount=D('1')))
        before=counts(case[0])
        with pytest.raises(PortfolioError,match='cambiaron'):
            stage_receipt_preview(case[0],inputs,selections,plan,generation=generation)
        assert counts(case[0])==before
        case[0].lock()
        with pytest.raises(VaultError):stage_receipt_preview(case[0],inputs,selections,plan,generation=generation)
