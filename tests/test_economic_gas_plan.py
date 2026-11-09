"""Exact gas-only allocation in economic order; no synthetic trade or capital."""
from dataclasses import fields,replace
from datetime import timedelta
from decimal import Decimal as D,localcontext,ROUND_FLOOR

import pytest
from app.portfolio.domain import PortfolioError,realized_pnl_by_currency
from app.portfolio.economic_projection import GAS_ENGINE,GasOrderClaim,ProjectionError,TieOrder
from app.portfolio.economic_receipt_plan import PendingReceipt,ReceiptOrder,prepare_receipts
from tests.test_economic_projection import AT,REC,A,B,event,uid
from tests.test_economic_gas_projection import gas_claim
from tests.test_economic_receipt_plan import STAMP,case


def pending_gas(amount,*,kind='CONTROL',index=0,count=1,block=100,**kwargs):
    coordinate=gas_claim(uid('placeholder-original'),kind=kind,index=index,count=count,block=block)
    order=ReceiptOrder(**{field.name:getattr(coordinate,field.name)for field in fields(ReceiptOrder)})
    return PendingReceipt(order,None,int(D(amount)*10**18),'EUR',REC+timedelta(seconds=30),**kwargs)


def plan(events,receipts,claims=(),**kwargs):
    return prepare_receipts(events,claims,receipts,batch_id=uid('gas-batch'),recorded_at=STAMP,**kwargs)


def capital():return event('native-capital','CONTRIBUTION',quote='BNB',at=AT-timedelta(hours=1),amount=D('1'))


@pytest.mark.parametrize('kind',['DEPLOYMENT','CONTROL','GAS_FUNDING','GAS_RETURN'])
def test_new_gas_plan_creates_only_fee_and_preserves_principal(kind):
    originals=[capital()];receipt=pending_gas('0.01',kind=kind);result=plan(originals,[receipt])
    assert result.projection.engine==GAS_ENGINE and len(result.created_events)==1
    assert result.created_events[0].input.kind=='FEE'and result.created_events[0].input.amount==D('0.01')
    assert isinstance(result.receipts[0].claim,GasOrderClaim)and result.receipts[0].claim.trade_event_id is None
    assert {member.role for member in result.receipts[0].members}=={'GAS'}
    book=result.projection.snapshots[A]
    assert book.cash=={'BNB':D('0.99')}and book.contributions=={'BNB':D('1')}and not book.withdrawals
    assert plan(originals,[receipt])==result


def test_partial_existing_cash_then_position_gas_is_exact_without_a_trade():
    originals=[event('capital','CONTRIBUTION',quote='EUR',at=AT-timedelta(hours=1),amount=D('100')),
        event('holding','BUY',quote='EUR',at=AT-timedelta(minutes=1),asset='BNB',quantity=D('1'),unit_price=D('100')),
        event('native','CONTRIBUTION',quote='BNB',at=AT-timedelta(minutes=1),amount=D('0.003')),
        event('old-fee','FEE',quote='BNB',at=AT+timedelta(minutes=1),recorded=5,amount=D('0.002'),note='Gas manual')]
    receipt=pending_gas('0.01',gas_event_ids=(uid('old-fee'),))
    result=plan(originals,[receipt]);book=result.projection.snapshots[A]
    assert [(item.input.asset,item.input.amount,item.input.quantity)for item in result.created_events]==[
        (None,D('0.001'),None),('BNB',None,D('0.007'))]
    assert book.cash=={'EUR':D('0'),'BNB':D('0')}
    position=book.positions['BNB','EUR'];assert (position.quantity,position.cost_basis,position.realized_pnl)==(D('0.993'),D('99.3'),D('-0.7'))
    assert realized_pnl_by_currency(book)=={'BNB':D('-0.003'),'EUR':D('-0.7')}
    assert book.contributions=={'EUR':D('100'),'BNB':D('0.003')}
    assert next(item for item in result.projection.events if item.original.id==uid('old-fee')).original==originals[-1]


def test_mixed_same_block_plan_keeps_gas_and_trade_order_in_one_graph():
    originals,receipts,_,_=case()
    receipts=[replace(item,order=replace(item.order,transaction_index=item.order.transaction_index+1,block_transaction_count=3))for item in receipts]
    control=pending_gas('0.005',count=3,index=0)
    result=plan(originals,[receipts[1],control,receipts[0]])
    assert [item.claim.transaction_index for item in result.receipts]==[0,1,2]
    assert len(result.created_events)==5 and result.projection.engine==GAS_ENGINE
    book=result.projection.snapshots[A]
    assert book.cash=={'USDT':D('850'),'BNB':D('0.965')}
    assert book.positions['WBNB','USDT'].realized_pnl==D('50')
    assert not any(member.role=='TRADE'for member in result.receipts[0].members)


def test_new_gas_after_existing_trade_claims_does_not_reuse_their_fee():
    from tests.test_economic_projection import fixture
    originals,claims=fixture();receipt=pending_gas('0.005',block=101)
    result=plan(originals,[receipt],claims)
    assert result.projection.snapshots[A].cash['BNB']==D('0.965')
    used={member for claim in claims for member in claim.event_ids}
    assert not used.intersection(result.receipts[0].claim.event_ids)


def test_existing_gas_claim_remains_bound_when_new_trades_are_planned():
    originals,receipts,_,_=case()
    fee=event('accepted-gas','FEE',quote='BNB',at=AT-timedelta(seconds=1),amount=D('0.005'),note='Gas previo')
    prior=gas_claim(fee.id,block=99,at=fee.occurred_at)
    result=plan([*originals,fee],receipts,[prior])
    assert result.projection.engine==GAS_ENGINE and len(result.created_events)==4
    assert result.projection.snapshots[A].cash['BNB']==D('0.965')
    assert next(row for row in result.projection.events if row.original.id==fee.id).order_record_id==prior.order_record_id


def test_internal_cash_transfer_changes_gas_allocation_without_becoming_an_external_flow():
    originals=[capital(),event('eur','CONTRIBUTION',quote='EUR',at=AT-timedelta(hours=1),amount=D('100')),
        event('holding','BUY',quote='EUR',at=AT-timedelta(minutes=1),asset='BNB',quantity=D('1'),unit_price=D('100')),
        event('out','CASH_TRANSFER_OUT',quote='BNB',request='pair',recorded=0,amount=D('0.3'),note='Propio'),
        event('in','CASH_TRANSFER_IN',account=B,quote='BNB',request='pair',recorded=0,amount=D('0.3'),note='Propio')]
    receipt=pending_gas('0.8')
    order=TieOrder(A,AT,('event:'+uid('out'),'chain:'+receipt.order.order_record_id))
    result=plan(originals,[receipt],tie_orders=(order,))
    fees=[item.input for item in result.created_events]
    assert [(fee.asset,fee.amount,fee.quantity)for fee in fees]==[(None,D('0.7'),None),('BNB',None,D('0.1'))]
    assert result.projection.snapshots[B].cash=={'BNB':D('0.3')}and result.projection.snapshots[B].contributions=={}
    assert result.projection.snapshots[A].withdrawals=={}


def test_existing_fee_requires_explicit_link_or_distinct_gas_confirmation():
    fee=event('old-fee','FEE',quote='BNB',amount=D('0.01'),note='Gas anterior')
    originals=[capital(),fee];receipt=pending_gas('0.01')
    with pytest.raises(ProjectionError,match='Selecciona el gas'):plan(originals,[receipt])
    linked=plan(originals,[replace(receipt,gas_event_ids=(fee.id,))])
    assert linked.created_events==()and linked.projection.snapshots[A].cash['BNB']==D('0.99')
    distinct=plan(originals,[replace(receipt,confirm_new_gas=True)])
    assert len(distinct.created_events)==1 and distinct.projection.snapshots[A].cash['BNB']==D('0.98')


def test_no_trade_selection_or_confirmation_is_allowed_on_gas_only_receipt():
    receipt=pending_gas('0.01')
    for change in ({'trade_event_id':uid('old-trade')},{'confirm_new_trade':True}):
        with pytest.raises(ProjectionError):replace(receipt,**change)
    _,trades,_,_=case()
    with pytest.raises(ProjectionError):replace(trades[0],order=receipt.order)


def test_hundred_existing_fee_members_are_supported_but_not_hundred_and_one():
    fees=[event('fee-'+str(i),'FEE',quote='BNB',amount=D('1E-18'),note='Gas parcial')for i in range(100)]
    receipt=pending_gas('0.000000000000000100',gas_event_ids=tuple(item.id for item in fees))
    result=plan([capital(),*fees],[receipt])
    assert result.created_events==()and len(result.receipts[0].claim.event_ids)==100
    assert result.projection.snapshots[A].cash['BNB']==D('0.999999999999999900')
    extra=event('fee-extra','FEE',quote='BNB',amount=D('1E-18'),note='Extra')
    with pytest.raises(ProjectionError):
        replace(receipt,gas_event_ids=(*receipt.gas_event_ids,extra.id))


def test_no_native_funds_and_no_bnb_position_never_invents_capital():
    originals=[event('other','CONTRIBUTION',at=AT-timedelta(hours=1),amount=D('100'))]
    with pytest.raises(PortfolioError):plan(originals,[pending_gas('0.01')])
    assert len(originals)==1 and originals[0].input.kind=='CONTRIBUTION'


def test_gas_allocation_has_canonical_precision_and_is_reproducible():
    originals=[capital()];receipts=[pending_gas('0.000000000000000003')]
    expected=plan(originals,receipts)
    with localcontext()as ctx:
        ctx.prec=5;ctx.rounding=ROUND_FLOOR
        assert plan(originals,receipts)==expected
