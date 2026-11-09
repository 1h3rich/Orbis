"""Real signed source capture -> detached preparation, no writes or owner funds."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select,func
from app.portfolio import dex_receipt_order as orders
from app.portfolio.domain import PortfolioError
from app.portfolio.economic_receipt_source import ReceiptSelection,capture_receipt_batch,prepare_receipt_batch
from app.portfolio.models import RealEvent,RealDexReceiptBooking,RealWalletSwapBooking,RealEconomicAcceptance,RealWalletLink
from app.portfolio.wallet_funding import _encoded
from tests.dex_order_fixture import same_block_case
from tests.test_dex_call_booking import resolved_case
from tests.test_economic_projection_source import saved_order


def counts(vault):
    with vault.session() as db:
        return tuple(db.scalar(select(func.count()).select_from(cls))for cls in
            (RealEvent,RealDexReceiptBooking,RealWalletSwapBooking,RealEconomicAcceptance))


def capture(vault,owner,selections):
    with vault.session() as db:
        before=db.connection().connection.driver_connection.total_changes
        result=capture_receipt_batch(db,owner,selections)
        assert db.connection().connection.driver_connection.total_changes==before
        return result


def stamp(inputs):
    return max(datetime.now(timezone.utc),*(row.recorded_at for row in inputs.events),
        *(receipt.not_before for receipt in inputs.receipts))+timedelta(microseconds=1)


@pytest.mark.parametrize('mode',['BUY','SELL'])
def test_actual_resolved_call_capture_can_prepare_after_vault_lock_without_bookings(tmp_path,mode):
    with resolved_case(tmp_path,mode) as case:
        vault,context,owner,meta,_,_,_,resolved=case
        saved_order(case);before=counts(vault)
        selections=[ReceiptSelection('CALL_RESOLUTION',resolved['id'],context,meta['owner'])]
        inputs=capture(vault,owner,selections)
        assert inputs.current is None and inputs.claims==() and not inputs.witness_json
        assert counts(vault)==before and before[1:]==(0,0,0)
        private=(meta['owner'].lower(),meta['operator'].lower())
        assert all(value not in ''.join(inputs.sources_json).lower()for value in private)
        vault.lock()
        result=prepare_receipt_batch(inputs,batch_id=str(uuid4()),recorded_at=stamp(inputs))
        receipt=result.receipts[0];book=result.projection.snapshots[owner]
        quantity=D('0.01')  # the owned fixture swaps 0.01 at its 1:1 pool
        assert book.positions['WBNB','USDT'].quantity==(D('1')+quantity if mode=='BUY' else D('1')-quantity)
        assert book.contributions['USDT']==D('100') and len(result.created_events)==2
        assert receipt.claim.source_kind=='CALL_RESOLUTION'
        assert book.cash['BNB']==D('0.01')-D(resolved['receipt']['gas_fee_wei'])/10**18


def test_actual_two_emitters_in_one_block_must_capture_the_complete_known_prefix(tmp_path,monkeypatch):
    with same_block_case(tmp_path,monkeypatch) as case:
        for original in case.sources:
            scope=dict(context_account_id=original.account_id,wallet_address=case.meta['owner'],
                evidence=case.observations[original.key],now=max(case.at,datetime.now(timezone.utc)))
            with case.vault.session() as db:preview=orders.preview_order(db,original.kind,original.id,**scope)
            with case.vault.session() as db:orders.confirm_order(db,original.kind,original.id,preview_digest=preview['preview_digest'],**scope)
        selections=[ReceiptSelection(source.kind,source.id,source.account_id,case.meta['owner'])for source in case.sources]
        all_inputs=capture(case.vault,case.ledger_account,selections);before=counts(case.vault)
        ordered=sorted(all_inputs.receipts,key=lambda item:item.order.transaction_index)
        later=next(item for item in selections if item.source_id==ordered[-1].order.source_id)
        with pytest.raises(PortfolioError,match='anterior pendiente'):
            capture(case.vault,case.ledger_account,[later])
        earliest=next(item for item in selections if item.source_id==ordered[0].order.source_id)
        assert len(capture(case.vault,case.ledger_account,[earliest]).receipts)==1
        result=prepare_receipt_batch(all_inputs,batch_id=str(uuid4()),recorded_at=stamp(all_inputs))
        assert {item.claim.source_kind for item in result.receipts}=={'OPERATOR','WALLET_SWAP'}
        assert [item.claim.transaction_index for item in result.receipts]==[0,1]
        book=result.projection.snapshots[case.ledger_account]
        assert book.cash['USDT']==D('100')-sum(item.trade.gross_amount for item in all_inputs.receipts)
        assert book.cash['BNB']==D('0.01')-sum(D(item.gas_wei)/10**18 for item in all_inputs.receipts)
        assert counts(case.vault)==before


def test_missing_order_bad_owner_context_and_revoked_link_never_write_a_batch(tmp_path):
    with resolved_case(tmp_path,'BUY') as case:
        vault,context,owner,meta,_,_,_,resolved=case
        selection=ReceiptSelection('CALL_RESOLUTION',resolved['id'],context,meta['owner'])
        before=counts(vault)
        with pytest.raises(PortfolioError,match='bloque'):capture(vault,owner,[selection])
        assert counts(vault)==before
        saved_order(case)
        for invalid in (replace(selection,context_account_id=owner),replace(selection,wallet_address=meta['operator'])):
            with pytest.raises(PortfolioError):capture(vault,owner,[invalid])
            assert counts(vault)==before
        with pytest.raises(PortfolioError,match='repite'):capture(vault,owner,[selection,selection])
        with vault.session() as db:
            link=db.scalar(select(RealWalletLink).where(RealWalletLink.account_id==owner));db.delete(link);db.commit()
        with pytest.raises(PortfolioError,match='Vincula'):capture(vault,owner,[selection])
        assert counts(vault)==before
