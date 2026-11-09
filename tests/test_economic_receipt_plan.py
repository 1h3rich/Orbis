"""Independent allocation/cost examples; a prepared plan is not archive authority."""
from dataclasses import fields,replace
from datetime import timedelta
from decimal import Context,Decimal as D,ROUND_FLOOR,localcontext

import pytest
from app.portfolio.domain import PortfolioError,realized_pnl_by_currency
from app.portfolio.economic_projection import ProjectionError,TieOrder,schedule_events
from app.portfolio.economic_receipt_plan import ReceiptOrder,PendingReceipt,prepare_receipts
from tests.test_economic_projection import AT,REC,A,B,fixture,event,uid


STAMP=REC+timedelta(minutes=1)


def pending(events,claims,*,reuse=False):
    found={row.id:row for row in events};result=[]
    for claim in claims:
        order=ReceiptOrder(**{field.name:getattr(claim,field.name)for field in fields(ReceiptOrder)})
        trade=replace(found[claim.trade_event_id].input,occurred_at=claim.block_at)
        amount=sum((row.input.amount or row.input.quantity)for row in events if row.id in claim.gas_event_ids)
        result.append(PendingReceipt(order,trade,int(amount*10**18),'EUR',REC+timedelta(seconds=30),
            claim.trade_event_id if reuse else None,claim.gas_event_ids if reuse else ()))
    return result


def case(*,reuse=False,position_gas=False,offsets=False):
    originals,claims=fixture(position_gas=position_gas,offsets=offsets)
    receipts=pending(originals,claims,reuse=reuse)
    members={member for claim in claims for member in (claim.trade_event_id,*claim.gas_event_ids)}
    return [row for row in originals if reuse or row.id not in members],receipts,originals,claims


def plan(events,receipts,**kwargs):
    return prepare_receipts(events,[],receipts,batch_id=uid('batch'),recorded_at=STAMP,**kwargs)


def test_new_mixed_wallet_operator_batch_uses_block_order_and_exact_gas_without_capital():
    events,receipts,_,_=case();before=tuple(events)
    result=plan(events,list(reversed(receipts)))
    book=result.projection.snapshots[A];position=book.positions['WBNB','USDT']
    assert (position.quantity,position.cost_basis,position.realized_pnl)==(D('1'),D('200'),D('50'))
    assert book.cash=={'USDT':D('850'),'BNB':D('0.97')}
    assert book.contributions=={'USDT':D('1000'),'BNB':D('1')}
    assert len(result.created_events)==4 and len(result.receipts)==2
    assert all(row.input.kind in ('BUY','SELL','FEE')for row in result.created_events)
    assert tuple(events)==before
    assert plan(events,receipts)==result
    assert [item.claim.transaction_index for item in result.receipts]==[0,1]


def test_linked_original_trade_and_gas_offsets_are_preserved_without_new_rows():
    events,receipts,originals,_=case(reuse=True,offsets=True);result=plan(events,receipts)
    assert result.created_events==() and tuple(events)==tuple(originals)
    assert all(member.existing for item in result.receipts for member in item.members)
    current=result.projection.snapshots[A].positions['WBNB','USDT']
    assert (current.cost_basis,current.realized_pnl)==(D('200'),D('50'))
    for row in result.projection.events:
        assert row.original==next(original for original in originals if original.id==row.original.id)


def test_partial_existing_gas_then_cash_then_position_are_allocated_at_execution():
    events,receipts,_,_=case(position_gas=True)
    events.extend([event('native-cash','CONTRIBUTION',at=AT-timedelta(minutes=1),quote='BNB',amount=D('0.005')),
        event('partial-fee','FEE',recorded=12,quote='BNB',amount=D('0.004'),note='Gas ya registrado')])
    receipts[0]=replace(receipts[0],gas_event_ids=(uid('partial-fee'),))
    result=plan(events,receipts);book=result.projection.snapshots[A]
    first=[row.input for row in result.created_events if row.id in result.receipts[0].claim.gas_event_ids]
    assert [(row.quote_currency,row.asset,row.amount,row.quantity)for row in first]==[
        ('BNB',None,D('0.001'),None),('EUR','BNB',None,D('0.005'))]
    assert book.cash['BNB']==0 and book.positions['BNB','EUR'].quantity==D('0.975')
    assert book.positions['BNB','EUR'].cost_basis==D('97.5')
    assert realized_pnl_by_currency(book)=={'USDT':D('50'),'BNB':D('-0.005'),'EUR':D('-2.5')}


def test_cash_transfer_before_same_block_changes_gas_allocation_without_contribution():
    events,receipts,_,_=case(position_gas=True)
    events.extend([event('native-cash','CONTRIBUTION',at=AT-timedelta(minutes=1),quote='BNB',amount=D('1')),
        event('cash-out','CASH_TRANSFER_OUT',quote='BNB',amount=D('0.995'),request='native-pair'),
        event('cash-in','CASH_TRANSFER_IN',account=B,quote='BNB',amount=D('0.995'),request='native-pair')])
    tie=TieOrder(A,AT,('event:'+uid('cash-out'),*(item.order.claim(uid('dummy-trade'),(uid('dummy-gas'),)).node_id for item in receipts)))
    result=plan(events,receipts,tie_orders=[tie])
    source,target=result.projection.snapshots[A],result.projection.snapshots[B]
    assert source.cash['BNB']==0 and source.positions['BNB','EUR'].quantity==D('0.975')
    assert target.cash['BNB']==D('0.995') and target.contributions=={}


def test_new_earlier_receipts_propagate_basis_through_later_transfer_and_sale():
    events,receipts,_,_=case()
    events.extend([event('out','TRANSFER_OUT',at=AT+timedelta(seconds=1),asset='WBNB',quantity=D('1'),gross_override=D('300'),request='asset-pair'),
        event('in','TRANSFER_IN',account=B,at=AT+timedelta(seconds=1),asset='WBNB',quantity=D('1'),gross_override=D('300'),request='asset-pair'),
        event('later-sale','SELL',account=B,at=AT+timedelta(seconds=2),asset='WBNB',quantity=D('1'),unit_price=D('350'))])
    result=plan(events,receipts)
    assert result.projection.snapshots[A].positions['WBNB','USDT'].quantity==0
    assert result.projection.snapshots[B].positions['WBNB','USDT'].realized_pnl==D('150')
    assert result.projection.adjusted_transfer_legs==2
    incoming=next(row for row in result.projection.events if row.original.id==uid('in'))
    assert incoming.original.input.gross_amount==D('300') and incoming.accounting_input.gross_amount==D('200')


def test_gas_position_base_currency_priority_and_context_rounding_are_preserved():
    events,receipts,_,_=case(position_gas=True)
    events.extend([event('usdt-bnb','BUY',at=AT-timedelta(minutes=1),asset='BNB',quantity=D('0.1'),unit_price=D('20'))])
    receipts=[replace(item,base_currency='USDT')for item in receipts]
    expected=plan(events,receipts)
    with localcontext(Context(prec=6,rounding=ROUND_FLOOR)):
        assert plan(events,receipts)==expected
    assert expected.projection.snapshots[A].positions['BNB','USDT'].quantity==D('0.07')
    assert expected.projection.snapshots[A].positions['BNB','EUR'].quantity==D('1')


@pytest.mark.parametrize('fault',['missing-gas','selected-conflict','gas-excess','trade-mismatch','duplicate-source','old-clock','cross-account'])
def test_invalid_or_insufficient_batch_fails_without_mutating_originals(fault):
    events,receipts,_,_=case(reuse=True);kwargs={}
    if fault=='missing-gas':
        events=[row for row in events if row.id!=uid('bnb')]
    elif fault=='selected-conflict':kwargs['unavailable_event_ids']=frozenset([receipts[0].trade_event_id])
    elif fault=='gas-excess':
        events=[replace(row,input=replace(row.input,amount=D('0.011')))if row.id==uid('gas-buy')else row for row in events]
    elif fault=='trade-mismatch':
        events=[replace(row,input=replace(row.input,unit_price=D('301')))if row.id==uid('buy')else row for row in events]
    elif fault=='duplicate-source':receipts.append(receipts[0])
    elif fault=='old-clock':receipts[0]=replace(receipts[0],not_before=STAMP+timedelta(seconds=1))
    elif fault=='cross-account':events=[replace(row,account_id=B)if row.id==uid('buy')else row for row in events]
    before=tuple(events)
    with pytest.raises(PortfolioError):plan(events,receipts,**kwargs)
    assert tuple(events)==before


def test_schedule_is_read_only_and_does_not_require_a_valid_financial_balance():
    events,claims=fixture();events=[row for row in events if row.id!=uid('bnb')]
    scheduled=schedule_events(events,claims)
    with pytest.raises(TypeError):scheduled.originals[uid('not-allowed')]=events[0]
    assert sum(len(node.members)for node in scheduled.nodes)==len(events)
    assert [node.order_record_id for node in scheduled.nodes if node.order_record_id]==[uid('proof0'),uid('proof1')]


@pytest.mark.parametrize('which',['trade','gas'])
def test_nearby_originals_require_reuse_or_explicit_new_fact_before_creating_rows(which):
    events,receipts,originals,_=case();receipt=receipts[0]
    if which=='trade':events.append(next(row for row in originals if row.id==uid('buy')))
    else:events.append(event('other-gas','FEE',quote='BNB',amount=D('0.005'),note='Otra comisión'))
    with pytest.raises(ProjectionError,match='existente'):plan(events,[receipt])
    allowed=replace(receipt,**{'confirm_new_'+which:True})
    result=plan(events,[allowed])
    assert len(result.created_events)==2
    if which=='trade':assert result.projection.snapshots[A].positions['WBNB','USDT'].quantity==D('3')
    else:assert result.projection.snapshots[A].cash['BNB']==D('0.985')
