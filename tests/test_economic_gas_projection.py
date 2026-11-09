"""Gas-only chronology, independent expected balances, no transaction authority."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D,localcontext,ROUND_FLOOR

import pytest
from app.portfolio.domain import PortfolioError,realized_pnl_by_currency
from app.portfolio.economic_projection import (
    ENGINE,GAS_ENGINE,GAS_SOURCE_KINDS,GasOrderClaim,ProjectionError,TieOrder,project_events,
)
from tests.test_economic_projection import AT,REC,A,B,event,fixture,uid


def gas_claim(*ids,kind='CONTROL',index=0,count=1,nonce=1,block=100,signer='c'*64,at=AT):
    return GasOrderClaim(kind,uid('source-'+kind),uid('order-'+kind),'a'*64,uid('context'),A,
        block,index,at,'0x'+'ab'*32,'b'*64,count,signer,nonce,tuple(ids))


@pytest.mark.parametrize('kind',sorted(GAS_SOURCE_KINDS))
def test_only_native_fee_for_every_supported_source_kind(kind):
    fee=event('fee','FEE',quote='BNB',amount=D('0.02'),note='Gas propio')
    originals=[event('capital','CONTRIBUTION',quote='BNB',at=AT-timedelta(hours=1),amount=D('1')),fee]
    claim=gas_claim(fee.id,kind=kind);before=tuple(originals)
    result=project_events(originals,[claim]);book=result.snapshots[A]
    assert result.engine==GAS_ENGINE and result.claimed_transactions==1
    assert claim.trade_event_id is None and claim.event_ids==(fee.id,)
    assert book.cash=={'BNB':D('0.98')}and book.contributions=={'BNB':D('1')}
    assert not book.positions and not book.withdrawals and book.cash_fees=={'BNB':D('0.02')}
    assert realized_pnl_by_currency(book)=={'BNB':D('-0.02')}
    assert tuple(originals)==before and [item.original for item in result.events]==originals


def test_same_block_gas_and_trades_share_coordinates_and_preserve_originals():
    originals,claims=fixture(offsets=True)
    claims=[replace(claim,block_transaction_count=3)for claim in claims]
    fee=event('control-gas','FEE',quote='BNB',at=AT-timedelta(minutes=1),recorded=4,amount=D('0.005'),note='Control')
    originals.append(fee);gas=gas_claim(fee.id,index=2,count=3)
    result=project_events(originals,[gas,*reversed(claims)]);book=result.snapshots[A]
    assert result.engine==GAS_ENGINE and book.cash=={'USDT':D('850'),'BNB':D('0.965')}
    assert book.positions['WBNB','USDT'].realized_pnl==D('50')
    assert book.contributions=={'USDT':D('1000'),'BNB':D('1')}
    assert realized_pnl_by_currency(book)=={'USDT':D('50'),'BNB':D('-0.035')}
    projected=next(item for item in result.events if item.original.id==fee.id)
    assert projected.original.input.occurred_at==AT-timedelta(minutes=1)and projected.accounting_at==AT
    assert [item.order_record_id for item in result.events if item.order_record_id is not None]==[
        claims[0].order_record_id,claims[0].order_record_id,claims[1].order_record_id,claims[1].order_record_id,gas.order_record_id]


def test_gas_from_position_keeps_quote_cost_and_never_creates_principal_events():
    fee=event('position-gas','FEE',quote='EUR',asset='BNB',quantity=D('0.1'),note='Gas de despliegue')
    originals=[event('capital','CONTRIBUTION',quote='EUR',at=AT-timedelta(hours=1),amount=D('100')),
        event('holding','BUY',quote='EUR',at=AT-timedelta(minutes=1),asset='BNB',quantity=D('1'),unit_price=D('100')),fee]
    result=project_events(originals,[gas_claim(fee.id,kind='GAS_FUNDING')]);book=result.snapshots[A]
    position=book.positions['BNB','EUR']
    assert (position.quantity,position.cost_basis,position.realized_pnl)==(D('0.9'),D('90'),D('-10'))
    assert position.fees_by_asset=={'BNB':D('0.1')}
    assert book.contributions=={'EUR':D('100')}and not book.withdrawals
    assert {item.original.id for item in result.events}=={item.id for item in originals}


@pytest.mark.parametrize('kind,values',[('BUY',{'asset':'WBNB','quantity':D('0.1'),'unit_price':D('1')}),
    ('CONTRIBUTION',{'amount':D('0.1')}),('WITHDRAWAL',{'amount':D('0.1')}),
    ('FEE',{'amount':D('0.1'),'note':'Comisión USDT'})])
def test_principal_trade_or_other_currency_cannot_be_disguised_as_gas(kind,values):
    item=event('not-gas',kind,**values)
    capital=event('capital','CONTRIBUTION',at=AT-timedelta(hours=1),amount=D('100'))
    with pytest.raises(ProjectionError,match='gas nativo'):project_events([capital,item],[gas_claim(item.id)])


def test_gas_and_trade_cannot_claim_the_same_fee_or_position():
    originals,claims=fixture()
    with pytest.raises(ProjectionError,match='reutiliza'):
        project_events(originals,[*claims,gas_claim(claims[0].gas_event_ids[0],block=11)])
    fee=event('control','FEE',quote='BNB',amount=D('0.01'),note='Control');originals.append(fee)
    with pytest.raises(ProjectionError,match='repite'):
        project_events(originals,[*claims,gas_claim(fee.id,index=0,count=2)])


def test_gas_order_rejects_repeated_nonce_and_false_shared_block():
    first=event('one','FEE',quote='BNB',amount=D('0.01'),note='Uno')
    second=event('two','FEE',quote='BNB',amount=D('0.01'),note='Dos')
    capital=event('capital','CONTRIBUTION',quote='BNB',at=AT-timedelta(hours=1),amount=D('1'))
    claims=[gas_claim(first.id,count=2),gas_claim(second.id,kind='DEPLOYMENT',index=1,count=2)]
    with pytest.raises(ProjectionError,match='nonce'):project_events([capital,first,second],claims)
    claims[1]=replace(claims[1],nonce=2,block_transactions_ref='d'*64)
    with pytest.raises(ProjectionError,match='mismo bloque'):project_events([capital,first,second],claims)


def test_explicit_manual_tie_positions_gas_before_internal_transfer_without_new_capital():
    fee=event('fee','FEE',quote='BNB',amount=D('0.2'),note='Gas')
    outgoing=event('out','CASH_TRANSFER_OUT',quote='BNB',request='pair',amount=D('0.3'),recorded=5,note='Propio')
    incoming=event('in','CASH_TRANSFER_IN',account=B,quote='BNB',request='pair',amount=D('0.3'),recorded=1,note='Propio')
    capital=event('capital','CONTRIBUTION',quote='BNB',at=AT-timedelta(hours=1),amount=D('1'))
    claim=gas_claim(fee.id)
    result=project_events([incoming,fee,capital,outgoing],[claim],tie_orders=(TieOrder(A,AT,(claim.node_id,'event:'+outgoing.id)),))
    assert result.snapshots[A].cash=={'BNB':D('0.5')}and result.snapshots[B].cash=={'BNB':D('0.3')}
    assert result.snapshots[B].contributions=={}and result.snapshots[A].withdrawals=={}
    assert next(item for item in result.events if item.original.id==fee.id).ordinal<next(item for item in result.events if item.original.id==outgoing.id).ordinal


def test_old_trade_only_projection_keeps_its_engine_and_decimal_context():
    originals,claims=fixture();expected=project_events(originals,claims)
    with localcontext()as ctx:
        ctx.prec=5;ctx.rounding=ROUND_FLOOR
        result=project_events(list(reversed(originals)),list(reversed(claims)))
    assert result==expected and result.engine==ENGINE


@pytest.mark.parametrize('change',[{'source_kind':'UNKNOWN'},{'gas_event_ids':()}, {'gas_event_ids':('bad',)},
    {'transaction_index':True},{'gas_event_ids':(uid('fee'),uid('fee'))}])
def test_invalid_gas_claims_fail_before_financial_replay(change):
    with pytest.raises(ProjectionError):replace(gas_claim(uid('fee')),**change)
