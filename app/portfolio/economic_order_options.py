"""Detached chronology choices, never a balance, booking or execution authority.

The financial planner remains the authority for allocation and replay. These
options describe stable chain nodes before newly allocated event IDs exist,
so a user can resolve a financial replay failure without fabricating rows.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime,timedelta
from decimal import Context,ROUND_HALF_EVEN,localcontext
import heapq

from app.portfolio.domain import CALC_PRECISION,EventInput,PortfolioEvent
from app.portfolio.economic_projection import ChainOrderClaim,GasOrderClaim,ExternalOrderClaim,ProjectionError,TieOrder
from app.portfolio.economic_projection_review import ProjectionInputs
from app.portfolio.economic_receipt_source import ReceiptBatchInputs
from app.portfolio.wallet_funding import _encoded
from app.portfolio.wallet_swap_booking import _amount,_gas_units,_matches_trade


SCHEMA='orbis.portfolio.economic.order.options.v1'
MAX_GROUPS=10000
MAX_GROUP_NODES=10000


def revision(inputs: ProjectionInputs|ReceiptBatchInputs,*,names: dict[str,str],parents: dict) -> str:
    if not isinstance(inputs,(ProjectionInputs,ReceiptBatchInputs)) or set(names)!=set(inputs.accounts):
        raise ProjectionError('Faltan las cuentas autenticadas del orden')
    body={'schema':SCHEMA,'root_account_id':inputs.root_account_id,'accounts':list(inputs.accounts),
        'account_revisions':dict(inputs.account_revisions),'names':names,'parents':parents,
        'witnesses':list(inputs.witness_json)}
    if isinstance(inputs,ReceiptBatchInputs):
        body.update(sources=list(inputs.sources_json),book_revisions=dict(inputs.book_revisions),include_gas=inputs.include_gas)
    else:body['source_policy']=inputs.source_policy
    return _encoded(body)[1]


@dataclass(frozen=True,slots=True)
class _Node:
    id: str
    account: str
    at: datetime
    priority: tuple[datetime,str]
    kind: str
    trade: EventInput|None
    gas_wei: int
    event: PortfolioEvent|None=None
    chain_position: tuple[int,int]|None=None


def _ordered(nodes: list[_Node]) -> list[_Node]:
    """A local proposal only; cross-account dependencies are checked at replay."""
    by_id={node.id:node for node in nodes};edges={node.id:set()for node in nodes};degrees={node.id:0 for node in nodes}
    manual=sorted((node for node in nodes if node.chain_position is None),key=lambda node:node.priority)
    chain=sorted((node for node in nodes if node.chain_position is not None),key=lambda node:node.chain_position)
    for sequence in (manual,chain):
        for left,right in zip(sequence,sequence[1:]):edges[left.id].add(right.id);degrees[right.id]+=1
    pending=[(*node.priority,node.id)for node in nodes if degrees[node.id]==0];heapq.heapify(pending);ordered=[]
    while pending:
        _,_,key=heapq.heappop(pending);ordered.append(by_id[key])
        for target in edges[key]:
            degrees[target]-=1
            if degrees[target]==0:heapq.heappush(pending,(*by_id[target].priority,target))
    if len(ordered)!=len(nodes):raise ProjectionError('La propuesta local perdió nodos')
    return ordered


def _visible(node: _Node) -> dict:
    item=node.event.input if node.event is not None else node.trade
    base={'id':node.id,'type':'MANUAL'if node.chain_position is None else 'CHAIN','kind':node.kind,
        'asset':item.asset or item.quote_currency if item is not None else 'BNB',
        'quantity':str(item.quantity)if item is not None and item.quantity is not None else None,
        'amount':str(item.gross_amount)if item is not None and item.kind in ('BUY','SELL')else
            str(item.amount)if item is not None and item.amount is not None else None,
        'quote_currency':item.quote_currency if item is not None else 'BNB',
        'gas_amount':str(_amount(node.gas_wei))if node.chain_position is not None else None}
    if node.chain_position is not None:
        base.update(block_number=str(node.chain_position[0]),transaction_index=str(node.chain_position[1]))
    return base


def order_options(inputs: ProjectionInputs|ReceiptBatchInputs,*,names: dict[str,str],parents: dict,
                  accepted_orders: tuple[TieOrder,...]=()) -> dict:
    digest=revision(inputs,names=names,parents=parents)
    pending=inputs.receipts if isinstance(inputs,ReceiptBatchInputs)else ()
    if not 0<=len(inputs.events)<=inputs.limits.max_events or not 0<len(inputs.claims)+len(pending)<=inputs.limits.max_claims:
        raise ProjectionError('El historial de orden está vacío o supera sus límites')
    events={event.id:event for event in inputs.events}
    if len(events)!=len(inputs.events):raise ProjectionError('El orden repite un asiento original')
    nodes=[];claimed=set();proofs=set()
    if type(accepted_orders)is not tuple or any(not isinstance(item,TieOrder)for item in accepted_orders):
        raise ProjectionError('Faltan los órdenes vigentes autenticados')
    accepted={(item.account_id,item.at):item.nodes for item in accepted_orders}
    if len(accepted)!=len(accepted_orders):raise ProjectionError('El orden vigente repite un grupo')
    def claim_members(ids,account):
        for key in ids:
            if key in claimed or key not in events or events[key].account_id!=account:
                raise ProjectionError('El orden perdió o reutiliza asientos del recibo')
            claimed.add(key)
    with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
        for claim in inputs.claims:
            if isinstance(claim,ExternalOrderClaim):
                if claim.node_id in proofs:raise ProjectionError('El orden repite una prueba externa')
                proofs.add(claim.node_id)
                for member in claim.members:claim_members((member.event_id,),member.account_id)
                for account in sorted({claim.account_id,*(item.account_id for item in claim.members)}):
                    ids=[item.event_id for item in claim.members if item.account_id==account]
                    priority=min((events[key].recorded_at,key)for key in ids)if ids else (claim.block_at,claim.node_id)
                    nodes.append(_Node(claim.node_id,account,claim.block_at,priority,'EXTERNAL',None,
                        sum(_gas_units(events[key].input)for key in claim.gas_event_ids if events[key].account_id==account),
                        chain_position=(claim.block_number,claim.transaction_index)))
                continue
            if not isinstance(claim,(ChainOrderClaim,GasOrderClaim)) or claim.node_id in proofs:
                raise ProjectionError('El orden perdió una prueba de red')
            proofs.add(claim.node_id);claim_members(claim.event_ids,claim.account_id)
            trade=events[claim.trade_event_id].input if claim.trade_event_id else None
            nodes.append(_Node(claim.node_id,claim.account_id,claim.block_at,
                min((events[key].recorded_at,key)for key in claim.event_ids),trade.kind if trade else 'FEE',trade,
                sum(_gas_units(events[key].input)for key in claim.gas_event_ids),
                chain_position=(claim.block_number,claim.transaction_index)))
        if isinstance(inputs,ReceiptBatchInputs):
            for receipt in inputs.receipts:
                order=receipt.order;node_id='chain:'+order.order_record_id
                refs=((receipt.trade_event_id,)if receipt.trade_event_id else ())+receipt.gas_event_ids
                if node_id in proofs or any(key in inputs.unavailable_event_ids for key in refs):
                    raise ProjectionError('El orden reutiliza una prueba o un asiento vinculado')
                proofs.add(node_id);claim_members(refs,order.account_id)
                if receipt.trade_event_id and not _matches_trade(events[receipt.trade_event_id].input,receipt.trade):
                    raise ProjectionError('La operación seleccionada no coincide con su recibo')
                total=0
                for key in receipt.gas_event_ids:
                    if abs(events[key].occurred_at-order.block_at)>timedelta(minutes=2):
                        raise ProjectionError('El gas seleccionado no corresponde al instante del recibo')
                    total+=_gas_units(events[key].input)
                if total>receipt.gas_wei:raise ProjectionError('Los asientos seleccionados superan el gas del recibo')
                priority=min((events[key].recorded_at,key)for key in refs)if refs else (receipt.not_before,node_id)
                nodes.append(_Node(node_id,order.account_id,order.block_at,priority,
                    receipt.trade.kind if receipt.trade is not None else 'FEE',receipt.trade,receipt.gas_wei,
                    chain_position=(order.block_number,order.transaction_index)))
        for event in inputs.events:
            if event.id not in claimed:nodes.append(_Node('event:'+event.id,event.account_id,event.occurred_at,
                (event.recorded_at,event.id),event.input.kind,None,0,event=event))
        grouped=defaultdict(list)
        positions=set()
        for node in nodes:
            if node.chain_position is not None:
                if node.chain_position in positions:raise ProjectionError('El orden repite una posición de red')
                positions.add(node.chain_position)
        for node in nodes:grouped[node.account,node.at].append(node)
        groups=[]
        for (account,at),values in sorted(grouped.items(),key=lambda pair:(pair[0][1],pair[0][0])):
            if len(values)<2 or all(node.chain_position is not None for node in values):continue
            # Positive contributions commute. With no other node at this
            # instant, choosing their order cannot alter a balance or basis.
            if all(node.chain_position is None and node.kind=='CONTRIBUTION'for node in values):continue
            if len(values)>MAX_GROUP_NODES:raise ProjectionError('Demasiados movimientos simultáneos; el grupo no se trunca')
            prior=accepted.get((account,at))
            retained=prior is not None and set(prior)=={node.id for node in values} and len(prior)==len(values)
            by_id={node.id:node for node in values}
            ordered=[by_id[key]for key in prior]if retained else _ordered(values)
            chain_order=[node.id for node in sorted((node for node in values if node.chain_position is not None),key=lambda node:node.chain_position)]
            if [node.id for node in ordered if node.chain_position is not None]!=chain_order:
                raise ProjectionError('El orden vigente contradice los índices de red')
            groups.append({'account_id':account,'name':names[account],'at':at.isoformat(),
                'accepted':retained,
                'nodes':[_visible(node)for node in ordered],
                'chain_order':chain_order})
            if len(groups)>MAX_GROUPS:raise ProjectionError('Demasiados grupos simultáneos para una revisión')
    return {'schema':SCHEMA,'revision':digest,'root_account_id':inputs.root_account_id,'groups':groups,
        'moves_funds':False,'trading_authority':False,'saved':False}
