"""Chronology proposals before allocation, with independent financial oracles."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest
from app.portfolio.economic_order_options import order_options,revision
from app.portfolio.economic_projection import ProjectionError,ProjectionLimits,TieOrder,project_events
from app.portfolio.economic_projection_review import ProjectionInputs
from app.portfolio.economic_receipt_source import ReceiptBatchInputs,prepare_receipt_batch
from tests.test_economic_projection import fixture,event,A,B,AT,uid
from tests.test_economic_receipt_plan import case,STAMP


def projection(events,claims):
    accounts=tuple(sorted({e.account_id for e in events}))
    return ProjectionInputs(A,accounts,tuple(events),tuple(claims),tuple((a,'a'*64)for a in accounts),(),ProjectionLimits())


def batch(events,receipts):
    accounts=tuple(sorted({e.account_id for e in events}))
    return ReceiptBatchInputs(A,accounts,tuple(events),(),(),tuple(receipts),frozenset(),
        tuple((a,'a'*64)for a in accounts),tuple((a,'b'*64)for a in accounts),
        tuple((a,'Cuenta')for a in accounts),json.dumps({a:None for a in accounts}),('authenticated selection',),None,ProjectionLimits())


def options(inputs):return order_options(inputs,names={a:'Cuenta'for a in inputs.accounts},parents={a:None for a in inputs.accounts})


def group_at(data,at=AT,account=A):
    return next(g for g in data['groups']if g['account_id']==account and g['at']==at.isoformat())


def test_existing_chain_nodes_are_indivisible_and_manual_order_has_exact_financial_effect():
    events,claims=fixture()
    events.append(event('manual-buy','BUY',recorded=0,asset='WBNB',quantity=D('1'),unit_price=D('500')))
    inputs=projection(events,claims);before=tuple(events);data=options(inputs);group=group_at(data)
    assert len(group['nodes'])==3 and len(group['chain_order'])==2
    manual='event:'+uid('manual-buy');chain=group['chain_order']
    first=project_events(events,claims,tie_orders=[TieOrder(A,AT,(manual,*chain))]).snapshots[A].positions['WBNB','USDT']
    last=project_events(events,claims,tie_orders=[TieOrder(A,AT,(*chain,manual))]).snapshots[A].positions['WBNB','USDT']
    assert (first.quantity,first.cost_basis,first.realized_pnl)==(D('2'),D('600'),D('-50'))
    assert (last.quantity,last.cost_basis,last.realized_pnl)==(D('2'),D('700'),D('50'))
    assert tuple(events)==before and not data['saved']and not data['moves_funds']and not data['trading_authority']
    with pytest.raises(ProjectionError,match='ciclo'):
        project_events(events,claims,tie_orders=[TieOrder(A,AT,(*reversed(chain),manual))])


def test_new_receipt_groups_exist_before_allocation_and_drive_cash_then_position_gas():
    events,receipts,_,_=case(position_gas=True)
    events.extend([event('native','CONTRIBUTION',at=AT-timedelta(minutes=1),quote='BNB',amount=D('1')),
        event('out','CASH_TRANSFER_OUT',quote='BNB',amount=D('0.995'),request='pair'),
        event('in','CASH_TRANSFER_IN',account=B,quote='BNB',amount=D('0.995'),request='pair')])
    inputs=batch(events,receipts);data=options(inputs);group=group_at(data)
    assert {node['id']for node in group['nodes']}=={'event:'+uid('out'),*group['chain_order']}
    tie=TieOrder(A,AT,('event:'+uid('out'),*group['chain_order']))
    plan=prepare_receipt_batch(inputs,batch_id=uid('options-batch'),recorded_at=STAMP,tie_orders=[tie])
    assert plan.projection.snapshots[A].cash['BNB']==0
    assert plan.projection.snapshots[A].positions['BNB','EUR'].quantity==D('0.975')
    assert plan.projection.snapshots[B].cash['BNB']==D('0.995')
    assert plan.projection.snapshots[B].contributions=={}


def test_selected_old_members_are_not_shown_as_duplicate_manual_nodes():
    events,receipts,_,_=case(reuse=True,offsets=True)
    events.append(event('extra-manual','CONTRIBUTION',amount=D('1')))
    data=options(batch(events,receipts));group=group_at(data)
    assert len(group['nodes'])==3
    assert [n['id']for n in group['nodes']if n['type']=='MANUAL']==['event:'+uid('extra-manual')]
    assert all('event:'+r.trade_event_id not in {n['id']for n in group['nodes']}for r in receipts)


def test_cross_account_cycle_can_be_presented_and_resolved_without_a_balance_fallback():
    events,claims=fixture();at=AT+timedelta(seconds=1)
    for account,label in ((A,'a'),(B,'b')):
        events.append(event('eth-'+label,'ADJUSTMENT_IN',account=account,at=AT-timedelta(minutes=1),quote='EUR',asset='ETH',quantity=D('1'),unit_price=D('100'),note='Inicial'))
    for label,owner,target in [('ab',A,B),('ba',B,A)]:
        events.extend([event(label+'-out','TRANSFER_OUT',account=owner,at=at,recorded=40,request=label,quote='EUR',asset='ETH',quantity=D('0.2'),gross_override=D('20'),note='Propio'),
            event(label+'-in','TRANSFER_IN',account=target,at=at,recorded=30,request=label,quote='EUR',asset='ETH',quantity=D('0.2'),gross_override=D('20'),note='Propio')])
    with pytest.raises(ProjectionError,match='ciclo'):project_events(events,claims)
    data=options(projection(events,claims));group=group_at(data,at)
    assert len(group['nodes'])==2 and group['chain_order']==[]
    fixed=project_events(events,claims,tie_orders=[TieOrder(A,at,('event:'+uid('ab-out'),'event:'+uid('ba-in')))])
    assert fixed.snapshots[A].positions['ETH','EUR'].quantity==fixed.snapshots[B].positions['ETH','EUR'].quantity==1


def test_revision_binds_book_parent_selection_profile_and_names_without_transient_wallets():
    events,receipts,_,_=case();inputs=batch(events,receipts);names={A:'Cuenta'};parents={A:None}
    base=revision(inputs,names=names,parents=parents)
    for changed in (replace(inputs,account_revisions=((A,'c'*64),)),replace(inputs,book_revisions=((A,'d'*64),)),
            replace(inputs,sources_json=('different selection',)),replace(inputs,include_gas=True)):
        assert revision(changed,names=names,parents=parents)!=base
    assert revision(inputs,names={A:'Otro alias'},parents=parents)!=base
    assert revision(inputs,names=names,parents={A:{'id':uid('parent'),'digest':'e'*64}})!=base


def test_exact_accepted_order_is_retained_but_new_group_members_require_another_choice():
    events,claims=fixture();events.append(event('manual-buy','BUY',asset='WBNB',quantity=D('0.1'),unit_price=D('2')))
    order=TieOrder(A,AT,(*(claim.node_id for claim in claims),'event:'+uid('manual-buy')))
    inputs=projection(events,claims)
    data=order_options(inputs,names={A:'Cuenta'},parents={A:None},accepted_orders=(order,))
    group=group_at(data)
    assert group['accepted']and [node['id']for node in group['nodes']]==list(order.nodes)
    assert all(not all(n['kind']=='CONTRIBUTION'for n in g['nodes'])for g in data['groups'])
    events.append(event('extra','CONTRIBUTION',amount=D('1')))
    changed=order_options(projection(events,claims),names={A:'Cuenta'},parents={A:None},accepted_orders=(order,))
    assert not group_at(changed)['accepted']


def test_duplicate_member_or_chain_position_and_oversized_group_fail_without_truncation(monkeypatch):
    events,receipts,_,_=case(reuse=True)
    with pytest.raises(ProjectionError):options(batch(events,[receipts[0],receipts[0]]))
    same=replace(receipts[1],order=replace(receipts[1].order,transaction_index=receipts[0].order.transaction_index))
    with pytest.raises(ProjectionError,match='posición'):options(batch(events,[receipts[0],same]))
    events.append(event('manual','CONTRIBUTION',amount=D('1')))
    monkeypatch.setattr('app.portfolio.economic_order_options.MAX_GROUP_NODES',2)
    with pytest.raises(ProjectionError,match='no se trunca'):options(batch(events,receipts))
