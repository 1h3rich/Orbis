"""Pure preparation of verified, not-yet-booked receipts for a joint review.

These plans are not authenticated acceptance inputs. The storage port must
revalidate sources, stage original rows, then capture sealed booking witnesses.
No database/RPC, authority, balance checkpoint or capital contribution here.
"""
from __future__ import annotations

from bisect import bisect_left,bisect_right
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from decimal import Context,ROUND_HALF_EVEN,localcontext
from typing import Literal,Sequence
from uuid import UUID,uuid5

from app.portfolio.domain import CALC_PRECISION,AccountSnapshot,EventInput,PortfolioError,PortfolioEvent,utc_datetime
from app.portfolio.economic_projection import (
    ChainOrderClaim,GasOrderClaim,ExternalOrderClaim,OrderClaim,ProjectionError,ProjectionLimits,ProjectionResult,TieOrder,_uuid,
    _apply_scheduled_event,project_events,schedule_events,
)
from app.portfolio.wallet_swap_booking import _amount,_gas_items,_gas_units,_matches_trade


@dataclass(frozen=True,slots=True)
class ReceiptOrder:
    source_kind: Literal['WALLET_SWAP','OPERATOR','CALL_RESOLUTION','DEPLOYMENT','CONTROL','GAS_FUNDING',
        'GAS_RETURN','DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION']
    source_id: str
    order_record_id: str
    order_record_digest: str
    context_account_id: str
    account_id: str
    block_number: int
    transaction_index: int
    block_at: datetime
    block_hash: str
    block_transactions_ref: str
    block_transaction_count: int
    signer_digest: str
    nonce: int

    def claim(self,trade: str|None,gas: tuple[str,...]) -> OrderClaim:
        coordinates=(self.source_kind,self.source_id,self.order_record_id,self.order_record_digest,
            self.context_account_id,self.account_id,self.block_number,self.transaction_index,self.block_at,
            self.block_hash,self.block_transactions_ref,self.block_transaction_count,self.signer_digest,
            self.nonce)
        return GasOrderClaim(*coordinates,gas)if trade is None else ChainOrderClaim(*coordinates,trade,gas)


@dataclass(frozen=True,slots=True)
class PendingReceipt:
    order: ReceiptOrder
    trade: EventInput|None
    gas_wei: int
    base_currency: str
    not_before: datetime
    trade_event_id: str|None=None
    gas_event_ids: tuple[str,...]=()
    confirm_new_trade: bool=False
    confirm_new_gas: bool=False

    def __post_init__(self):
        valid_trade=(isinstance(self.trade,EventInput) and self.trade.kind in ('BUY','SELL') and self.trade.asset=='WBNB'
                and self.trade.quote_currency=='USDT' and self.trade.fee==0 and self.trade.fee_asset is None
                and self.trade.occurred_at==self.order.block_at and self.order.source_kind in ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION'))if isinstance(self.order,ReceiptOrder)else False
        if (not isinstance(self.order,ReceiptOrder) or self.trade is not None and not valid_trade
                or self.trade is None and (self.trade_event_id is not None or self.confirm_new_trade)
                or type(self.gas_wei) is not int
                or not 0<self.gas_wei<2**256 or not isinstance(self.base_currency,str)
                or not self.base_currency or type(self.gas_event_ids) is not tuple
                or len(self.gas_event_ids)>100 or len(set(self.gas_event_ids))!=len(self.gas_event_ids)
                or type(self.confirm_new_trade) is not bool or type(self.confirm_new_gas) is not bool
                or self.confirm_new_trade and self.trade_event_id is not None):
            raise ProjectionError('La preparación necesita un recibo verificado y su gas exacto')
        utc_datetime(self.not_before)
        if self.trade_event_id is not None:_uuid(self.trade_event_id)
        for event_id in self.gas_event_ids:_uuid(event_id)


@dataclass(frozen=True,slots=True)
class PlannedMember:
    event_id: str
    role: Literal['TRADE','GAS']
    existing: bool


@dataclass(frozen=True,slots=True)
class PlannedReceipt:
    source: PendingReceipt
    members: tuple[PlannedMember,...]
    claim: OrderClaim


@dataclass(frozen=True)
class ReceiptPlan:
    batch_id: str
    recorded_at: datetime
    created_events: tuple[PortfolioEvent,...]
    receipts: tuple[PlannedReceipt,...]
    projection: ProjectionResult


def _identity(batch_id: str,receipt: PendingReceipt,role: str) -> str:
    return str(uuid5(UUID(batch_id),receipt.order.source_kind+':'+receipt.order.source_id+':'+role))


def prepare_receipts(events: Sequence[PortfolioEvent],claims: Sequence[OrderClaim],
                     receipts: Sequence[PendingReceipt],*,batch_id: str,recorded_at: datetime,
                     unavailable_event_ids: frozenset[str]=frozenset(),tie_orders: Sequence[TieOrder]=(),
                     limits: ProjectionLimits=ProjectionLimits()) -> ReceiptPlan:
    """Resolve gas in the scheduled economic state, then replay the final inputs.

    A temporary cash-fee member participates only in the chronology, never in
    a financial calculation. It is replaced with exact cash/position gas rows
    at that node. Existing allocated rows retain their dates, IDs and inputs.
    """
    _uuid(batch_id);stamp=utc_datetime(recorded_at)
    if (not isinstance(limits,ProjectionLimits) or not isinstance(events,Sequence)
            or not isinstance(claims,Sequence) or not isinstance(receipts,Sequence)
            or type(unavailable_event_ids) is not frozenset or not 0<len(receipts)<=limits.max_claims-len(claims)
            or any(not isinstance(item,PendingReceipt) for item in receipts)
            or any(not isinstance(item,(ChainOrderClaim,GasOrderClaim,ExternalOrderClaim)) for item in claims)
            or any(not isinstance(item,PortfolioEvent) or item.accounting is not None for item in events)):
        raise ProjectionError('Lote de preparación contable inválido')
    original={item.id:item for item in events}
    if len(original)!=len(events):raise ProjectionError('El libro repite un asiento')
    accounts={item.account_id for item in events}
    claimed=set(unavailable_event_ids)|{member for claim in claims for member in claim.event_ids}
    references=[identifier for receipt in receipts for identifier in
        ((receipt.trade_event_id,) if receipt.trade_event_id is not None else ())+receipt.gas_event_ids]
    reserved=set(references)
    if len(reserved)!=len(references) or reserved&claimed:
        raise ProjectionError('El lote reutiliza un asiento o selecciona uno ya vinculado')
    candidates=sorted((row for row in events if row.id not in claimed and row.id not in reserved
        and row.input.kind in ('BUY','SELL','FEE')),key=lambda row:row.occurred_at)
    candidate_times=[row.occurred_at for row in candidates]
    def nearby(receipt):
        at=receipt.order.block_at
        for index in range(bisect_left(candidate_times,at-timedelta(minutes=2)),bisect_right(candidate_times,at+timedelta(minutes=2))):
            row=candidates[index]
            if row.account_id==receipt.order.account_id:yield row
    ordered=sorted(receipts,key=lambda item:(item.order.block_number,item.order.transaction_index))
    if any(stamp<item.recorded_at for item in events) or any(stamp<item.not_before for item in ordered):
        raise ProjectionError('El registro del lote es anterior a sus originales')
    proposed=[];provisional=[];placeholders={};members_by_order={};selected=set();created=[]
    def event(receipt,role,item,offset=0):
        identifier=_identity(batch_id,receipt,role)
        if identifier in original:raise ProjectionError('Una identidad del lote ya pertenece al libro')
        return PortfolioEvent(identifier,receipt.order.account_id,_identity(batch_id,receipt,'request:'+role),
            stamp+timedelta(microseconds=offset),item)
    with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
        for receipt in ordered:
            order=receipt.order
            if order.account_id not in accounts:raise ProjectionError('Falta el historial original de la cuenta destino')
            members=[];trade=None
            if receipt.trade is None:pass
            elif receipt.trade_event_id is not None:
                trade=original.get(receipt.trade_event_id)
                if (trade is None or trade.account_id!=order.account_id or not _matches_trade(trade.input,receipt.trade)
                        or trade.id in claimed or trade.id in selected):
                    raise ProjectionError('La operación seleccionada no coincide o ya está vinculada')
                selected.add(trade.id);members.append(PlannedMember(trade.id,'TRADE',True))
            else:
                if not receipt.confirm_new_trade and any(_matches_trade(row.input,receipt.trade)for row in nearby(receipt)):
                    raise ProjectionError('Selecciona la operación existente o confirma crear otra')
                trade=event(receipt,'trade',receipt.trade);proposed.append(trade);created.append(trade)
                members.append(PlannedMember(trade.id,'TRADE',False))
            gas_ids=[];remaining=receipt.gas_wei
            for identifier in receipt.gas_event_ids:
                gas=original.get(identifier)
                if (gas is None or gas.account_id!=order.account_id or identifier in claimed or identifier in selected
                        or abs(gas.occurred_at-order.block_at)>timedelta(minutes=2)):
                    raise ProjectionError('El gas seleccionado cambió o ya está vinculado')
                remaining-=_gas_units(gas.input);selected.add(identifier);gas_ids.append(identifier)
                members.append(PlannedMember(identifier,'GAS',True))
            if remaining<0:raise ProjectionError('Los asientos seleccionados superan el gas del recibo')
            if not receipt.gas_event_ids and not receipt.confirm_new_gas:
                for candidate in nearby(receipt):
                    try:native=_gas_units(candidate.input)
                    except PortfolioError:continue
                    if 0<native<=receipt.gas_wei:
                        raise ProjectionError('Selecciona el gas existente o confirma registrar otro gasto')
            if remaining:
                placeholder=event(receipt,'pending-gas',EventInput('FEE','BNB',order.block_at,amount=_amount(remaining),note='Preparación temporal de gas'),1)
                proposed.append(placeholder);gas_ids.append(placeholder.id);placeholders[placeholder.id]=(receipt,remaining)
            provisional.append(order.claim(trade.id if trade is not None else None,tuple(gas_ids)))
            members_by_order[order.order_record_id]=members
        scheduled=schedule_events([*events,*proposed],[*claims,*provisional],tie_orders=tie_orders,limits=limits)
        states={account:AccountSnapshot(account) for account in scheduled.accounts};basis={}
        for node in scheduled.nodes:
            for member in node.members:
                if member in placeholders:
                    receipt,remaining=placeholders[member]
                    items=_gas_items(states[node.account_id],remaining,node.at,receipt.order.source_id,receipt.base_currency)
                    for index,item in enumerate(items):
                        gas=event(receipt,'gas:'+str(index),item,index+1);created.append(gas)
                        members_by_order[receipt.order.order_record_id].append(PlannedMember(gas.id,'GAS',False))
                        _apply_scheduled_event(gas,node,states,basis)
                else:_apply_scheduled_event(scheduled.originals[member],node,states,basis)
        planned=[];all_events={**original,**{item.id:item for item in created}}
        if len(all_events)!=len(original)+len(created):raise ProjectionError('El lote repite una identidad nueva')
        for receipt in ordered:
            members=tuple(members_by_order[receipt.order.order_record_id])
            if not (1 if receipt.trade is None else 2)<=len(members)<=100:raise ProjectionError('Demasiados asientos para el recibo')
            gas=tuple(member.event_id for member in members if member.role=='GAS')
            if sum(_gas_units(all_events[identifier].input)for identifier in gas)!=receipt.gas_wei:
                raise ProjectionError('El gas preparado no conserva el importe exacto del recibo')
            trade_ids=[member.event_id for member in members if member.role=='TRADE']
            if len(trade_ids)!=(0 if receipt.trade is None else 1):raise ProjectionError('El recibo no conserva sus miembros económicos')
            planned.append(PlannedReceipt(receipt,members,receipt.order.claim(trade_ids[0]if trade_ids else None,gas)))
        result=project_events(tuple(all_events.values()),[*claims,*(item.claim for item in planned)],tie_orders=tie_orders,limits=limits)
        if result.snapshots!=states:raise ProjectionError('El resultado preparado no reproduce el mismo inventario')
        return ReceiptPlan(batch_id,stamp,tuple(created),tuple(planned),result)
