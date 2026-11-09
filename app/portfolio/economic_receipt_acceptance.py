"""Joint verified-receipt booking and economic acceptance, with one commit.

HTTP admission additionally checks active workers and unresolved orders.
Capture and materialization use short leases; all financial replay is outside.
The v2 draft binds the batch to unchanged public receipt/event record formats.
"""
from __future__ import annotations

from dataclasses import asdict,dataclass
from contextlib import nullcontext
from datetime import datetime,timedelta
import json
from typing import Sequence
from uuid import UUID,uuid5

from sqlalchemy.orm import Session

from app.portfolio import economic_acceptance as archive,models as m
from app.portfolio.dex_receipt_order_source import archived_order_source
from app.portfolio.domain import PortfolioError,_symbol
from app.portfolio.economic_projection import GAS_SOURCE_KINDS,ProjectionError,ProjectionLimits,TieOrder,_uuid
from app.portfolio.economic_projection_review import ALL_RECEIPTS,EXTERNAL_RECEIPTS,review_policy
from app.portfolio.economic_projection_source import _body,_booking,witness_record,is_gas_witness
from app.portfolio.economic_receipt_source import ReceiptSelection,ReceiptBatchInputs,capture_receipt_batch,prepare_receipt_batch,selected_order_source
from app.portfolio.economic_receipt_stage import StagedReceiptInputs,_materialize_receipt_plan,stage_receipt_preview
from app.portfolio.economic_receipt_plan import ReceiptPlan
from app.portfolio.wallet_funding import _encoded,_time
from app.portfolio.wallet_funding_credit import _wallet_owner


BATCH_SCHEMA='orbis.portfolio.receipt.batch.v1'
GAS_BATCH_SCHEMA='orbis.portfolio.receipt.batch.v2'
SOURCE_KEYS={'source_kind','source_id','definition_digest','receipt_digest','updated_at','owner_digest',
    'order_record_id','order_record_digest','context_account_id','target_account_id','base_currency',
    'trade_event_id','gas_event_ids','confirm_new_trade','confirm_new_gas'}
GAS_SOURCE_KEYS=SOURCE_KEYS|{'gas_only'}


def validate_batch_manifest(db: Session,batch: dict,review: dict,*,limits: ProjectionLimits) -> None:
    """Header-time source/booking checks; no balances, wallet addresses or RPC."""
    try:
        gas_mode=review_policy(review)in(ALL_RECEIPTS,EXTERNAL_RECEIPTS)
        if (set(batch)!={'schema','batch_id','recorded_at','sources','receipts'}
                or batch['schema']!=(GAS_BATCH_SCHEMA if gas_mode else BATCH_SCHEMA)
                or type(batch['sources'])is not list or type(batch['receipts'])is not list
                or not 0<len(batch['sources'])==len(batch['receipts'])<=limits.max_claims):raise ValueError('batch')
        namespace=UUID(_uuid(batch['batch_id']));stamp=_time(datetime.fromisoformat(batch['recorded_at']))
        if stamp.isoformat()!=batch['recorded_at']:raise ValueError('stamp')
        sources={(item['source_kind'],item['source_id']):item for item in batch['sources']}
        receipts={(item['source_kind'],item['source_id']):item for item in batch['receipts']}
        witnesses={(item['source_kind'],item['source_id']):item for item in review['witnesses']}
        if (len(sources)!=len(batch['sources']) or len(receipts)!=len(batch['receipts']) or sources.keys()!=receipts.keys()
                or len(witnesses)!=len(review['witnesses']) or not sources.keys()<=witnesses.keys()):raise ValueError('sources')
        for key,spec in sources.items():
            item=receipts[key]
            gas_only=spec.get('gas_only',False)
            if (set(spec)!=(GAS_SOURCE_KEYS if gas_mode else SOURCE_KEYS)
                    or set(item)!={'source_kind','source_id','account_id','order_record_id','booking_digest','events'}
                    or type(gas_only)is not bool or gas_only!=is_gas_witness(witnesses[key])
                    or spec['source_kind']not in (GAS_SOURCE_KINDS if gas_only else ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION'))
                    or spec['target_account_id']not in review['account_revisions']
                    or _symbol(spec['base_currency'],'base currency')!=spec['base_currency']
                    or type(spec['gas_event_ids'])is not list or len(set(spec['gas_event_ids']))!=len(spec['gas_event_ids'])
                    or type(spec['confirm_new_trade'])is not bool or type(spec['confirm_new_gas'])is not bool):raise ValueError('selection')
            record=witness_record(db,witnesses[key]);row,payload=record
            if row.id!=spec['order_record_id']:raise ValueError('proof')
            if gas_only:
                from app.portfolio.dex_gas_order_source import archived_gas_source
                from app.portfolio.economic_gas_source import _body as body_for
                source=archived_gas_source(db,*key,proof=payload['proof'])
            else:source=archived_order_source(db,*key,proof=payload['proof']);body_for=_body
            booked,booking=_booking(db,record)
            if (row.source_kind!=key[0] or payload['source_id']!=key[1] or row.account_id!=spec['context_account_id']
                    or row.payload_digest!=spec['order_record_digest'] or source.definition_digest!=spec['definition_digest']
                    or source.receipt_digest!=spec['receipt_digest'] or source.updated_at.isoformat()!=spec['updated_at']
                    or source.owner_digest!=spec['owner_digest'] or booked.account_id!=spec['target_account_id']
                    or item['account_id']!=booked.account_id or item['order_record_id']!=row.id
                    or item['booking_digest']!=booked.payload_digest or booking['booked_at']!=batch['recorded_at']
                    or body_for(db,record,(booked,booking))!= {k:v for k,v in witnesses[key].items()if k!='seal'}):raise ValueError('dependencies')
            expected=[{'id':ref['id'],'role':ref['role'],'existing':ref['existing']}for ref in booking['events']]
            if item['events']!=expected:raise ValueError('members')
            trades=[ref for ref in expected if ref['role']=='TRADE'];old_gas=[ref['id']for ref in expected if ref['role']=='GAS'and ref['existing']]
            if (len(trades)!=(0 if gas_only else 1)
                    or (trades[0]['id']if trades and trades[0]['existing']else None)!=spec['trade_event_id']
                    or gas_only and spec['confirm_new_trade']
                    or sorted(old_gas)!=sorted(spec['gas_event_ids']) or spec['trade_event_id']is not None and spec['confirm_new_trade']):raise ValueError('allocation')
            gas_index=0
            for ref in expected:
                if ref['existing']:continue
                role='trade'if ref['role']=='TRADE'else 'gas:'+str(gas_index)
                offset=0 if ref['role']=='TRADE'else gas_index+1
                if ref['role']=='GAS':gas_index+=1
                prefix=key[0]+':'+key[1]+':'
                event=db.get(m.RealEvent,ref['id'])
                if (event is None or event.id!=str(uuid5(namespace,prefix+role))
                        or event.request_id!=str(uuid5(namespace,prefix+'request:'+role))
                        or event.recorded_at!=stamp+timedelta(microseconds=offset)):raise ValueError('new originals')
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
        raise ProjectionError('El lote aceptado cambió o perdió sus recibos originales') from None


@dataclass(frozen=True)
class _Prepared:
    inputs: ReceiptBatchInputs
    plan: ReceiptPlan
    staged: StagedReceiptInputs
    draft: dict
    digest: str


def _prepare(vault,root,selections,*,batch_id,recorded_at,generation,wallet_addresses,tie_orders,limits,include_gas=False):
    with vault.guard_session_generation(generation),vault.session() as db:
        inputs=capture_receipt_batch(db,root,selections,wallet_addresses=wallet_addresses,limits=limits,include_gas=include_gas)
    plan=prepare_receipt_batch(inputs,batch_id=batch_id,recorded_at=recorded_at,tie_orders=tie_orders)
    staged=stage_receipt_preview(vault,inputs,selections,plan,generation=generation,wallet_addresses=wallet_addresses)
    draft,_=archive._draft(staged.projection,json.loads(staged.parents_json),tie_orders)
    draft={**draft,'schema':archive.EXTERNAL_BATCH_DRAFT if staged.projection.source_policy==EXTERNAL_RECEIPTS else archive.GAS_BATCH_DRAFT if staged.projection.source_policy==ALL_RECEIPTS else archive.BATCH_DRAFT,
        'receipt_batch':json.loads(staged.manifest_json)}
    raw,digest=_encoded(draft)
    with vault.guard_session_generation(generation):return _Prepared(inputs,plan,staged,json.loads(raw),digest)


def prepare_receipt_acceptance_review(vault,root_account_id: str,selections: Sequence[ReceiptSelection],*,batch_id: str,
                               recorded_at: datetime,wallet_addresses: Sequence[str]=(),
                               tie_orders: Sequence[TieOrder]=(),limits: ProjectionLimits=ProjectionLimits(),include_gas: bool=False) -> _Prepared:
    generation=vault.session_generation
    return _prepare(vault,root_account_id,selections,batch_id=batch_id,recorded_at=recorded_at,generation=generation,
        wallet_addresses=wallet_addresses,tie_orders=tie_orders,limits=limits,include_gas=include_gas)


def preview_receipt_acceptance(vault,root_account_id: str,selections: Sequence[ReceiptSelection],*,batch_id: str,
                               recorded_at: datetime,wallet_addresses: Sequence[str]=(),
                               tie_orders: Sequence[TieOrder]=(),limits: ProjectionLimits=ProjectionLimits(),include_gas: bool=False) -> dict:
    prepared=prepare_receipt_acceptance_review(vault,root_account_id,selections,batch_id=batch_id,recorded_at=recorded_at,
        wallet_addresses=wallet_addresses,tie_orders=tie_orders,limits=limits,include_gas=include_gas)
    return {'id':archive.identifier(prepared.digest),'preview_digest':prepared.digest,'draft':prepared.draft,
        'saved':False,'applied_to_ledger':False,'trading_authority':False}


def _match_selection(db,root,selections,batch_id,recorded_at,payload,tie_orders,limits):
    draft=payload['draft']
    if (draft['schema']not in(archive.BATCH_DRAFT,archive.GAS_BATCH_DRAFT,archive.EXTERNAL_BATCH_DRAFT)or draft['review']['root_account_id']!=root
            or draft['limits']!=asdict(limits)
            or archive._ties(draft['review'])!=tuple(sorted(tie_orders,key=lambda item:(item.at,item.account_id)))):
        raise ProjectionError('La respuesta pertenece a otra revisión')
    batch=draft['receipt_batch'];expected={(spec['source_kind'],spec['source_id']):spec for spec in batch['sources']}
    if (batch['batch_id']!=batch_id or batch['recorded_at']!=_time(recorded_at).isoformat()
            or len(selections)!=len(expected) or {(item.source_kind,item.source_id)for item in selections}!=expected.keys()):
        raise ProjectionError('La respuesta pertenece a otro lote')
    for item in selections:
        spec=expected[item.source_kind,item.source_id];owner=_wallet_owner(db,item.wallet_address)
        source=selected_order_source(db,item)
        if (owner is None or owner.id!=spec['target_account_id'] or source.owner_digest!=spec['owner_digest']
                or source.account_id!=item.context_account_id or item.context_account_id!=spec['context_account_id']
                or item.trade_event_id!=spec['trade_event_id'] or list(item.gas_event_ids)!=spec['gas_event_ids']
                or item.confirm_new_trade!=spec['confirm_new_trade'] or item.confirm_new_gas!=spec['confirm_new_gas']
                or item.gas_only!=spec.get('gas_only',False)):
            raise ProjectionError('La wallet, el vínculo o la selección del lote cambió')


def _batch_result(row,payload,*,created):
    batch=payload['draft']['receipt_batch']
    count=sum(not member['existing']for item in batch['receipts']for member in item['events'])
    return {**archive._result(row,created=created),'receipt_count':len(batch['receipts']),
        'new_events_count':count,'events_created':count if created else 0,'batch_id':batch['batch_id']}


def _recover(vault,root,selections,*,digest,batch_id,recorded_at,generation,tie_orders,limits):
    record_id=archive.identifier(digest)
    with vault.guard_session_generation(generation),vault.session() as db:
        archive._snapshot(db)
        if db.get(m.RealEconomicAcceptance,record_id)is None:return None
        archived=archive.capture_acceptance(db,record_id);payload=json.loads(archived.payload_json)
        _match_selection(db,root,selections,batch_id,recorded_at,payload,tie_orders,limits)
    archive.replay_acceptance(archived)
    with vault.guard_session_generation(generation),vault.session() as db:
        archive._snapshot(db);row,payload=archive._lineage(db,record_id)
        _match_selection(db,root,selections,batch_id,recorded_at,payload,tie_orders,limits)
        return _batch_result(row,payload,created=False)


def _not_before(db,inputs):
    at=max(event.recorded_at for event in inputs.events)
    for witness in map(json.loads,inputs.witness_json):
        record=witness_record(db,witness);_,booking=_booking(db,record)
        at=max(at,record[0].created_at,_time(datetime.fromisoformat(booking['booked_at'])))
    return at


def confirm_receipt_acceptance(vault,root_account_id: str,selections: Sequence[ReceiptSelection],*,batch_id: str,
                               recorded_at: datetime,preview_digest: str,now: datetime,wallet_addresses: Sequence[str]=(),
                               tie_orders: Sequence[TieOrder]=(),limits: ProjectionLimits=ProjectionLimits(),
                               commit_guard: archive.CommitGuard|None=None,include_gas: bool=False) -> dict:
    root=_uuid(root_account_id);_uuid(batch_id);stamp=_time(recorded_at);now=_time(now);generation=vault.session_generation
    options=dict(digest=preview_digest,batch_id=batch_id,recorded_at=stamp,generation=generation,tie_orders=tie_orders,limits=limits)
    recovered=_recover(vault,root,selections,**options)
    if recovered is not None:return recovered
    if now<stamp or now-stamp>timedelta(minutes=15):raise ProjectionError('La revisión del lote caducó; revisa de nuevo')
    try:
        prepared=_prepare(vault,root,selections,batch_id=batch_id,recorded_at=stamp,generation=generation,
            wallet_addresses=wallet_addresses,tie_orders=tie_orders,limits=limits,include_gas=include_gas)
    except PortfolioError:
        # Another confirmation may have won between the first read and its
        # short staging transaction. Recover that exact immutable prefix once.
        recovered=_recover(vault,root,selections,**options)
        if recovered is not None:return recovered
        raise
    if prepared.digest!=preview_digest:raise ProjectionError('El libro o el lote cambió; revisa de nuevo')
    with (commit_guard or nullcontext)(),vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            existing=db.get(m.RealEconomicAcceptance,archive.identifier(preview_digest))
            if existing is not None:
                row,payload=archive._lineage(db,existing.id)
                _match_selection(db,root,selections,batch_id,stamp,payload,tie_orders,limits)
                if payload['draft']!=prepared.draft:raise ProjectionError('Ya existe otra aceptación para el lote')
                result=_batch_result(row,payload,created=False);db.commit();return result
            fresh=capture_receipt_batch(db,root,selections,wallet_addresses=wallet_addresses,limits=limits,include_gas=include_gas)
            if fresh!=prepared.inputs:raise ProjectionError('El libro o los vínculos cambiaron durante la revisión')
            staged=_materialize_receipt_plan(db,fresh,selections,prepared.plan)
            if staged!=prepared.staged:raise ProjectionError('Los originales materializados cambiaron durante la revisión')
            archive._store_reviewed_draft(db,root,prepared.draft,preview_digest,now=now,not_before=_not_before(db,staged.projection),
                wallet_addresses=(*wallet_addresses,*(item.wallet_address for item in selections)))
            row,payload=archive._lineage(db,archive.identifier(preview_digest));result=_batch_result(row,payload,created=True)
            db.commit();return result
        except Exception:db.rollback();raise
