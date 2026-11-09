"""Rollback-only staging to authenticate a complete proposed receipt prefix.

The internal materializer never commits. Its eventual committing caller must
also insert the reviewed acceptance in the same transaction. There is no API
route to the materializer and no bypass flag on ordinary portfolio writes.
"""
from __future__ import annotations

from dataclasses import dataclass,replace
from decimal import Context,ROUND_HALF_EVEN,localcontext
import json
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio import models as m,dex_receipt_booking as bookings,wallet_swap_booking as swaps
from app.portfolio.domain import CALC_PRECISION
from app.portfolio.economic_acceptance import _heads
from app.portfolio.economic_projection import ProjectionError,ExternalOrderClaim
from app.portfolio.economic_projection_review import ALL_RECEIPTS,ProjectionInputs,capture_projection_inputs
from app.portfolio.economic_projection_source import capture_booked_order
from app.portfolio.economic_receipt_plan import ReceiptPlan
from app.portfolio.economic_receipt_source import ReceiptBatchInputs,ReceiptSelection,capture_receipt_batch,_financial
from app.portfolio.repository import _digest,_from_row
from app.portfolio.wallet_funding import _encoded


@dataclass(frozen=True)
class StagedReceiptInputs:
    projection: ProjectionInputs
    parents_json: str
    manifest_json: str


def _materialize(db: Session,inputs: ReceiptBatchInputs,selections: Sequence[ReceiptSelection],
                 plan: ReceiptPlan) -> StagedReceiptInputs:
    """Private composable writer: BEGIN IMMEDIATE belongs to the caller."""
    if (not db.connection().connection.driver_connection.in_transaction or not isinstance(plan,ReceiptPlan)
            or tuple(item.source for item in plan.receipts)!=tuple(sorted(inputs.receipts,
                key=lambda item:(item.order.block_number,item.order.transaction_index)))):
        raise ProjectionError('La materialización necesita el plan completo del lote revisado')
    selected={(item.source_kind,item.source_id):item for item in selections}
    if len(selected)!=len(plan.receipts):raise ProjectionError('La selección no corresponde al plan revisado')
    for event in plan.created_events:
        if (event.accounting is not None or event.account_id not in inputs.accounts
                or db.get(m.RealEvent,event.id)is not None
                or db.scalar(select(m.RealEvent.id).where(m.RealEvent.account_id==event.account_id,
                    m.RealEvent.request_id==event.request_id))is not None):
            raise ProjectionError('Un asiento nuevo del lote ya existe o pertenece a otra cuenta')
        item=event.input
        db.add(m.RealEvent(id=event.id,account_id=event.account_id,request_id=event.request_id,payload_digest=_digest(item),
            kind=item.kind,asset=item.asset,quote_currency=item.quote_currency,quantity=item.quantity,
            unit_price=item.unit_price,gross_amount=item.gross_amount,fee=item.fee,fee_asset=item.fee_asset,
            occurred_at=item.occurred_at,recorded_at=event.recorded_at,note=item.note))
    db.flush();new_witnesses=[];manifest=[]
    for proposed in plan.receipts:
        order=proposed.claim;selection=selected.get((order.source_kind,order.source_id))
        if selection is None:raise ProjectionError('Falta la selección original del recibo')
        refs=[]
        for member in proposed.members:
            row=db.get(m.RealEvent,member.event_id)
            if row is None or row.account_id!=order.account_id:raise ProjectionError('Falta un asiento del lote')
            refs.append({'id':row.id,'digest':_digest(_from_row(row).input),'role':member.role,'existing':member.existing})
        if order.source_kind=='WALLET_SWAP':
            source,_,state=swaps._context(db,selection.source_id,selection.context_account_id,selection.wallet_address)
            payload={'contract':swaps.CONTRACT,'action_id':source.id,'context_account_id':source.account_id,
                'account_id':order.account_id,'wallet_digest':source.wallet_digest,'plan_digest':source.payload_digest,
                'receipt_digest':_encoded(state['receipt'])[1],'revision_before':dict(inputs.book_revisions)[order.account_id],
                'booked_at':plan.recorded_at.isoformat(),'events':refs}
            raw,digest=_encoded(payload)
            db.add(m.RealWalletSwapBooking(action_id=source.id,account_id=order.account_id,payload_json=raw,payload_digest=digest));db.flush()
            for ref in refs:db.add(m.RealWalletSwapBookingEvent(action_id=source.id,event_id=ref['id'],role=ref['role']))
        else:
            source=_financial(db,selection);identifier=bookings.booking_id(source.kind,source.id)
            payload={'contract':bookings._contract(source.kind),'source_kind':source.kind,'source_id':source.id,
                'context_account_id':source.account_id,'account_id':order.account_id,'wallet_digest':source.wallet_digest,
                'owner_ref':bookings._owner_ref(db,source,order.account_id),'plan_digest':source.plan_digest,
                'receipt_digest':source.receipt_digest,'revision_before':dict(inputs.book_revisions)[order.account_id],
                'booked_at':plan.recorded_at.isoformat(),'events':refs}
            raw,digest=_encoded(payload)
            db.add(m.RealDexReceiptBooking(id=identifier,source_kind=source.kind,source_id=source.id,account_id=order.account_id,
                payload_json=raw,payload_digest=digest));db.flush()
            for ref in refs:db.add(m.RealDexReceiptBookingEvent(booking_id=identifier,event_id=ref['id'],role=ref['role']))
        db.flush()
        if selection.gas_only:
            from app.portfolio.economic_gas_source import capture_booked_gas_order
            witness=capture_booked_gas_order(db,order.order_record_id,wallet_address=selection.wallet_address)
        else:
            witness=capture_booked_order(db,order.order_record_id,wallet_address=selection.wallet_address)
        new_witnesses.append(witness)
        manifest.append({'source_kind':order.source_kind,'source_id':order.source_id,'account_id':order.account_id,
            'order_record_id':order.order_record_id,'booking_digest':witness['booking_digest'],
            'events':[{'id':ref['id'],'role':ref['role'],'existing':ref['existing']}for ref in refs]})
    witnesses=[*map(json.loads,inputs.witness_json),*new_witnesses]
    projected=capture_projection_inputs(db,inputs.root_account_id,witnesses,limits=inputs.limits)
    # Authenticated original member IDs and the actual chain proof must be
    # identical to those used for pure preparation. Plans are not witnesses.
    def signature(claims):
        return tuple(sorted((item if isinstance(item,ExternalOrderClaim)else replace(item,gas_event_ids=tuple(sorted(item.gas_event_ids)))for item in claims),key=lambda item:item.order_record_id))
    expected=signature([*inputs.claims,*(item.claim for item in plan.receipts)])
    if signature(projected.claims)!=expected:
        raise ProjectionError('La prueba materializada no corresponde al plan calculado')
    body={'schema':'orbis.portfolio.receipt.batch.v2'if projected.source_policy!='dex-trades-1' else 'orbis.portfolio.receipt.batch.v1',
        'batch_id':plan.batch_id,'recorded_at':plan.recorded_at.isoformat(),
        'sources':list(map(json.loads,inputs.sources_json)),'receipts':manifest}
    return StagedReceiptInputs(projected,_encoded(_heads(db,inputs.accounts))[0],_encoded(body)[0])


def _materialize_receipt_plan(db: Session,inputs: ReceiptBatchInputs,selections: Sequence[ReceiptSelection],
                              plan: ReceiptPlan) -> StagedReceiptInputs:
    with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
        return _materialize(db,inputs,selections,plan)


def stage_receipt_preview(vault,inputs: ReceiptBatchInputs,selections: Sequence[ReceiptSelection],plan: ReceiptPlan,*,
                          generation: int,wallet_addresses: Sequence[str]=()) -> StagedReceiptInputs:
    """Revalidate, stage and ALWAYS roll back before returning detached inputs."""
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            fresh=capture_receipt_batch(db,inputs.root_account_id,selections,wallet_addresses=wallet_addresses,limits=inputs.limits,
                include_gas=inputs.include_gas)
            if fresh!=inputs:raise ProjectionError('El libro, los recibos o sus vínculos cambiaron; revisa de nuevo')
            return _materialize_receipt_plan(db,inputs,selections,plan)
        finally:db.rollback()
