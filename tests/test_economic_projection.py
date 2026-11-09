"""Independent weighted-cost examples and transfer closure, never financial I/O."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D,Context,ROUND_FLOOR,localcontext
from hashlib import sha256
from uuid import UUID,uuid5

import pytest

from app.portfolio.domain import EventInput,PortfolioEvent,PortfolioError,fold_events,realized_pnl_by_currency,CALC_PRECISION
from app.portfolio.economic_projection import ChainOrderClaim,TieOrder,ProjectionLimits,ProjectionError,project_events,ENGINE
from app.portfolio.economic_projection import ExternalOrderClaim, ExternalOrderMember


AT=datetime(2026,1,2,12,tzinfo=timezone.utc)
REC=AT+timedelta(days=1)
NS=UUID('7f37454c-7cd3-4c2c-91b6-3a677a63c356')
def uid(name):return str(uuid5(NS,name))
A,B,C=map(uid,('a','b','c'))


def event(name,kind,*,account=A,at=AT,recorded=0,request=None,quote='USDT',**values):
    return PortfolioEvent(uid(name),account,uid(request or name),REC+timedelta(seconds=recorded),
        EventInput(kind,quote,at,**values))


def fixture(*,offsets=False,position_gas=False):
    before=AT-timedelta(hours=1)
    events=[event('cash','CONTRIBUTION',at=before,amount=D('1000')),
        event('holding','BUY',at=before+timedelta(seconds=1),asset='WBNB',quantity=D('1'),unit_price=D('100'))]
    if position_gas:
        events.extend([event('eur','CONTRIBUTION',at=before,quote='EUR',amount=D('100')),
            event('bnb','BUY',at=before+timedelta(seconds=1),quote='EUR',asset='BNB',quantity=D('1'),unit_price=D('100'))])
    else:events.append(event('bnb','CONTRIBUTION',at=before,quote='BNB',amount=D('1')))
    buy_at=AT+timedelta(minutes=1) if offsets else AT
    sell_at=AT-timedelta(minutes=1) if offsets else AT
    buy=event('buy','BUY',at=buy_at,recorded=20,asset='WBNB',quantity=D('1'),unit_price=D('300'))
    sell=event('sell','SELL',at=sell_at,recorded=10,asset='WBNB',quantity=D('1'),unit_price=D('250'))
    gas=[]
    for name,at,recorded,amount in [('gas-buy',buy_at,21,'0.01'),('gas-sell',sell_at,11,'0.02')]:
        gas.append(event(name,'FEE',at=at,recorded=recorded,note='Gas propio',quote='EUR' if position_gas else 'BNB',
            **({'asset':'BNB','quantity':D(amount)} if position_gas else {'amount':D(amount)})))
    claims=[]
    for index,trade,fee in [(0,buy,gas[0]),(1,sell,gas[1])]:
        claims.append(ChainOrderClaim('OPERATOR' if index==0 else 'WALLET_SWAP',uid('source'+str(index)),
            uid('proof'+str(index)),sha256(('proof'+str(index)).encode()).hexdigest(),uid('context'),A,100,index,AT,
            '0x'+'ab'*32,'b'*64,2,
            sha256(('signer'+str(index)).encode()).hexdigest(),1,trade.id,(fee.id,)))
    return [*events,sell,gas[1],buy,gas[0]],claims


def test_chain_order_changes_realized_cost_not_cash_units_or_original_facts():
    events,claims=fixture();unchanged=tuple(events);legacy=fold_events(A,events)
    old=legacy.positions['WBNB','USDT'];assert (old.quantity,old.cost_basis,old.realized_pnl)==(D('1'),D('300'),D('150'))
    result=project_events(list(reversed(events)),list(reversed(claims)))
    assert result.engine==ENGINE and result.claimed_transactions==2
    current=result.snapshots[A];position=current.positions['WBNB','USDT']
    assert (position.quantity,position.cost_basis,position.realized_pnl)==(D('1'),D('200'),D('50'))
    assert current.cash=={'USDT':D('850'),'BNB':D('0.97')}
    assert current.contributions=={'USDT':D('1000'),'BNB':D('1')} and current.withdrawals=={}
    assert realized_pnl_by_currency(current)=={'USDT':D('50'),'BNB':D('-0.03')}
    claimed=[row.original.id for row in result.events if row.order_record_id is not None]
    assert claimed==[uid('buy'),uid('gas-buy'),uid('sell'),uid('gas-sell')]
    assert tuple(events)==unchanged and set(row.original.id for row in result.events)=={event.id for event in events}
    assert all(row.original==next(event for event in events if event.id==row.original.id) for row in result.events)


def test_manual_clock_offsets_are_retained_and_network_time_is_separate():
    events,claims=fixture(offsets=True);result=project_events(events,claims)
    trades={row.original.id:row for row in result.events if row.original.id in (uid('buy'),uid('sell'))}
    assert trades[uid('buy')].original.input.occurred_at==AT+timedelta(minutes=1)
    assert trades[uid('sell')].original.input.occurred_at==AT-timedelta(minutes=1)
    assert all(row.accounting_at==AT for row in trades.values())
    assert result.snapshots[A].positions['WBNB','USDT'].realized_pnl==D('50')


def test_corrected_basis_propagates_through_two_paired_transfers_with_asset_fee():
    events,claims=fixture();at=AT+timedelta(seconds=1)
    events.extend([
        event('ab-out','TRANSFER_OUT',at=at,recorded=30,request='ab',asset='WBNB',quantity=D('0.5'),gross_override=D('150'),fee=D('0.1'),fee_asset='WBNB',note='Propio'),
        event('ab-in','TRANSFER_IN',account=B,at=at,recorded=30,request='ab',asset='WBNB',quantity=D('0.4'),gross_override=D('150'),note='Propio'),
        event('bc-out','TRANSFER_OUT',account=B,at=at+timedelta(seconds=1),recorded=31,request='bc',asset='WBNB',quantity=D('0.2'),gross_override=D('75'),note='Propio'),
        event('bc-in','TRANSFER_IN',account=C,at=at+timedelta(seconds=1),recorded=31,request='bc',asset='WBNB',quantity=D('0.2'),gross_override=D('75'),note='Propio'),
        event('b-sale','SELL',account=B,at=at+timedelta(seconds=2),recorded=32,asset='WBNB',quantity=D('0.1'),unit_price=D('600'))])
    result=project_events(events,claims);assert result.adjusted_transfer_legs==4
    expected={A:(D('0.5'),D('100'),D('50')),B:(D('0.1'),D('25'),D('35')),C:(D('0.2'),D('50'),D('0'))}
    for account,values in expected.items():
        position=result.snapshots[account].positions['WBNB','USDT'];assert (position.quantity,position.cost_basis,position.realized_pnl)==values
    transfers={row.original.id:row for row in result.events if row.transfer_basis_derived}
    assert transfers[uid('ab-out')].original.input.gross_amount==D('150')
    assert transfers[uid('ab-out')].accounting_input.gross_amount==transfers[uid('ab-in')].accounting_input.gross_amount==D('100')
    assert transfers[uid('bc-out')].accounting_input.gross_amount==transfers[uid('bc-in')].accounting_input.gross_amount==D('50')
    assert result.snapshots[A].positions['WBNB','USDT'].fees_by_asset=={'WBNB':D('0.1')}
    assert result.snapshots[B].contributions=={} and result.snapshots[C].contributions=={}


def test_cash_transfer_keeps_principal_internal_and_charges_only_actual_fee():
    events,claims=fixture();at=AT+timedelta(seconds=1)
    events.extend([event('cash-out','CASH_TRANSFER_OUT',at=at,request='cash-transfer',amount=D('800'),fee=D('10'),note='Propio'),
        event('cash-in','CASH_TRANSFER_IN',account=B,at=at,request='cash-transfer',amount=D('800'),note='Propio')])
    result=project_events(events,claims)
    assert result.snapshots[A].cash['USDT']==D('40') and result.snapshots[B].cash['USDT']==D('800')
    assert result.snapshots[A].cash_fees['USDT']==D('10') and realized_pnl_by_currency(result.snapshots[A])['USDT']==D('40')
    assert result.snapshots[B].contributions=={} and result.adjusted_transfer_legs==0


def test_bnb_fee_position_keeps_its_own_currency_and_never_invents_fx():
    events,claims=fixture(position_gas=True);result=project_events(events,claims)
    bnb=result.snapshots[A].positions['BNB','EUR']
    assert (bnb.quantity,bnb.cost_basis,bnb.realized_pnl)==(D('0.97'),D('97'),D('-3'))
    assert result.snapshots[A].positions['WBNB','USDT'].realized_pnl==D('50')
    assert realized_pnl_by_currency(result.snapshots[A])['EUR']==D('-3')


def test_explicit_tie_order_can_move_manual_items_but_cannot_reverse_chain():
    events,claims=fixture()
    events.extend([event('manual1','CONTRIBUTION',recorded=5,amount=D('10')),event('manual2','CONTRIBUTION',recorded=6,amount=D('20'))])
    nodes=('event:'+uid('manual2'),'event:'+uid('manual1'),claims[0].node_id,claims[1].node_id)
    result=project_events(events,claims,tie_orders=(TieOrder(A,AT,nodes),))
    at=[row.node_id for row in result.events if row.accounting_at==AT]
    assert list(dict.fromkeys(at))==list(nodes)
    with pytest.raises(ProjectionError,match='ciclo'):
        project_events(events,claims,tie_orders=(TieOrder(A,AT,(*nodes[:2],claims[1].node_id,claims[0].node_id)),))
    for order in (TieOrder(A,AT,nodes[:-1]),TieOrder(A,AT,(nodes[0],)*len(nodes)),TieOrder(B,AT,nodes)):
        with pytest.raises(ProjectionError):project_events(events,claims,tie_orders=(order,))


def test_cross_account_cycle_requires_an_explicit_consistent_manual_order():
    events,claims=fixture();before=AT-timedelta(minutes=1);at=AT+timedelta(seconds=1)
    for account,label in ((A,'a'),(B,'b')):
        events.append(event('eth-'+label,'ADJUSTMENT_IN',account=account,at=before,quote='EUR',asset='ETH',quantity=D('1'),unit_price=D('100'),note='Saldo previo'))
    for label,owner,target in [('ab',A,B),('ba',B,A)]:
        events.extend([event(label+'-out','TRANSFER_OUT',account=owner,at=at,recorded=40,request=label,quote='EUR',asset='ETH',quantity=D('0.2'),gross_override=D('20'),note='Propio'),
            event(label+'-in','TRANSFER_IN',account=target,at=at,recorded=30,request=label,quote='EUR',asset='ETH',quantity=D('0.2'),gross_override=D('20'),note='Propio')])
    with pytest.raises(ProjectionError,match='ciclo'):project_events(events,claims)
    chosen=TieOrder(A,at,('event:'+uid('ab-out'),'event:'+uid('ba-in')))
    result=project_events(events,claims,tie_orders=(chosen,))
    for account in (A,B):
        position=result.snapshots[account].positions['ETH','EUR'];assert (position.quantity,position.cost_basis)==(D('1'),D('100'))


@pytest.mark.parametrize('ordering', ['prefix_orders', 'tie_orders'])
def test_one_shared_chain_node_can_have_an_order_in_each_account(ordering):
    before = AT - timedelta(hours=1)
    cash = event('shared-cash', 'CONTRIBUTION', at=before, amount=D('1000'))
    outgoing = event('shared-out', 'CASH_TRANSFER_OUT', request='shared-transfer', amount=D('10'))
    incoming = event('shared-in', 'CASH_TRANSFER_IN', account=B, request='shared-transfer', amount=D('10'))
    claim = ExternalOrderClaim('EXTERNAL_OBSERVATION', uid('shared-source'), uid('shared-proof'), 'a' * 64,
        A, A, 10, 0, AT, '0x' + 'ab' * 32, 'b' * 64, 1, 'c' * 64, 1,
        (ExternalOrderMember(outgoing.id, A, 'EFFECT'), ExternalOrderMember(incoming.id, B, 'EFFECT')))
    events = (cash, outgoing, incoming)
    orders = (TieOrder(A, before, ('event:' + cash.id,)), TieOrder(A, AT, (claim.node_id,)),
        TieOrder(B, AT, (claim.node_id,)))
    expected = project_events(events, (claim,))
    actual = project_events(events, (claim,), **{ordering: orders})
    assert actual == expected
    assert actual.snapshots[A].cash['USDT'] == D('990')
    assert actual.snapshots[B].cash['USDT'] == D('10')
    assert tuple(item.original for item in actual.events) == events
    with pytest.raises(ProjectionError):
        project_events(events, (claim,), **{ordering: (*orders, orders[0])})


def test_decimal_context_is_fixed_even_for_repeating_average_cost():
    events,claims=fixture()
    changed=[]
    for item in events:
        values=({'unit_price':D('1')} if item.id==uid('holding') else
            {'quantity':D('2'),'unit_price':D('0.5')} if item.id==uid('buy') else
            {'unit_price':D('1')} if item.id==uid('sell') else {})
        changed.append(replace(item,input=replace(item.input,**values)) if values else item)
    expected=project_events(changed,claims).snapshots
    with localcontext() as context:
        context.prec=6;context.rounding=ROUND_FLOOR
        assert project_events(list(reversed(changed)),list(reversed(claims))).snapshots==expected
    with localcontext(Context(prec=CALC_PRECISION)):
        position=expected[A].positions['WBNB','USDT']
        assert position.realized_pnl==D('1')-D('2')/D('3')


def test_missing_dependencies_duplicate_groups_bad_members_and_budgets_fail_closed():
    events,claims=fixture();original=tuple(events)
    with pytest.raises(ProjectionError):project_events(events,[])
    with pytest.raises(ProjectionError):project_events(events+[events[0]],claims)
    with pytest.raises(ProjectionError):project_events(events,claims+claims[:1])
    with pytest.raises(ProjectionError):project_events(events,[replace(claims[0],account_id=B),claims[1]])
    with pytest.raises(ProjectionError):project_events(events,[replace(claims[0],block_at=AT+timedelta(minutes=3)),claims[1]])
    with pytest.raises(ProjectionError):project_events(events,claims,limits=ProjectionLimits(max_events=len(events)-1))
    with pytest.raises(ProjectionError):project_events(events,claims,limits=ProjectionLimits(max_claims=1))
    incomplete=events+[event('orphan','TRANSFER_OUT',at=AT+timedelta(seconds=1),request='missing',asset='WBNB',quantity=D('0.1'),gross_override=D('20'))]
    with pytest.raises(ProjectionError,match='Falta'):project_events(incomplete,claims)
    assert tuple(events)==original


def test_inconsistent_block_witnesses_and_signed_nonce_order_are_rejected():
    events,claims=fixture()
    for changed in ({'block_hash':'0x'+'ef'*32},{'block_transactions_ref':'d'*64},{'block_transaction_count':3},
                    {'block_at':AT+timedelta(seconds=1)},{'signer_digest':claims[0].signer_digest,'nonce':0}):
        with pytest.raises(ProjectionError):project_events(events,[claims[0],replace(claims[1],**changed)])


@pytest.mark.parametrize('field,value',[('block_number',True),('transaction_index',-1),('nonce',2**64),('gas_event_ids',()),('account_id','bad'),
    ('block_hash','bad'),('block_transactions_ref','bad'),('block_transaction_count',True),('block_transaction_count',0)])
def test_claim_descriptors_are_strict(field,value):
    _,claims=fixture()
    with pytest.raises(PortfolioError):replace(claims[0],**{field:value})
