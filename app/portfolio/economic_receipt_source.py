"""One authenticated read snapshot for preparing a joint receipt acceptance.

Wallet addresses remain transient request inputs. Detached results contain
only original events, sealed witnesses, pseudonymous bindings and order facts.
There is no write, RPC or heavy financial replay under this capture's lease.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass,field
from datetime import datetime
import json
from typing import Literal,Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.accounting_view import AccountingInputs,AccountingReadIssue,capture_review_baseline,validate_review_baseline,_extension_witness
from app.portfolio import models as m,dex_receipt_booking as bookings,dex_call_booking as calls
from app.portfolio.domain import PortfolioEvent
from app.portfolio.dex_deployments import _identity
from app.portfolio.dex_receipt_order import identifier,order_record
from app.portfolio.dex_receipt_order_source import order_source
from app.portfolio.economic_acceptance import _heads,_snapshot
from app.portfolio.economic_projection import GAS_SOURCE_KINDS,GasOrderClaim,ExternalOrderClaim,OrderClaim,ProjectionError,ProjectionLimits,TieOrder,_uuid
from app.portfolio.economic_projection_review import ALL_RECEIPTS,_chunks,_required_sources,original_account_closure
from app.portfolio.economic_projection_source import _existing_salt,capture_booked_order,claim_from_witness
from app.portfolio.economic_receipt_plan import PendingReceipt,ReceiptOrder,ReceiptPlan,prepare_receipts
from app.portfolio.repository import original_book_revision,account_book_revision
from app.portfolio.wallet_funding import _encoded
from app.portfolio.wallet_funding_credit import _wallet_owner
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_swap_booking import _trade,_context as wallet_context


@dataclass(frozen=True,slots=True)
class ReceiptSelection:
    source_kind: Literal['WALLET_SWAP','OPERATOR','CALL_RESOLUTION','DEPLOYMENT','CONTROL','GAS_FUNDING',
        'GAS_RETURN','DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION']
    source_id: str
    context_account_id: str
    wallet_address: str=field(repr=False)
    trade_event_id: str|None=None
    gas_event_ids: tuple[str,...]=()
    confirm_new_trade: bool=False
    confirm_new_gas: bool=False
    gas_only: bool=False

    def __post_init__(self):
        if (self.source_kind not in GAS_SOURCE_KINDS or type(self.gas_only)is not bool
                or not self.gas_only and self.source_kind not in ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION')
                or self.gas_only and (self.trade_event_id is not None or self.confirm_new_trade)):
            raise ProjectionError('Origen de lote no compatible')
        _uuid(self.source_id);_uuid(self.context_account_id)
        object.__setattr__(self,'wallet_address',canonical_wallet_address(self.wallet_address))
        if self.trade_event_id is not None:_uuid(self.trade_event_id)
        if (type(self.gas_event_ids) is not tuple or len(self.gas_event_ids)>100
                or len(set(self.gas_event_ids))!=len(self.gas_event_ids)
                or type(self.confirm_new_trade)is not bool or type(self.confirm_new_gas)is not bool
                or self.confirm_new_trade and self.trade_event_id is not None):
            raise ProjectionError('Selección de gas inválida')
        for value in self.gas_event_ids:_uuid(value)


@dataclass(frozen=True)
class ReceiptBatchInputs:
    root_account_id: str
    accounts: tuple[str,...]
    events: tuple[PortfolioEvent,...]
    claims: tuple[OrderClaim,...]
    witness_json: tuple[str,...]
    receipts: tuple[PendingReceipt,...]
    unavailable_event_ids: frozenset[str]
    account_revisions: tuple[tuple[str,str],...]
    book_revisions: tuple[tuple[str,str],...]
    account_names: tuple[tuple[str,str],...]
    parents_json: str
    sources_json: tuple[str,...]
    current: AccountingInputs|AccountingReadIssue|None
    limits: ProjectionLimits
    include_gas: bool=False


def _unavailable(db,events):
    used=set()
    for batch in _chunks(event.id for event in events):
        for cls in (m.RealWalletSwapBookingEvent,m.RealDexReceiptBookingEvent,m.RealMexcFillBooking):
            used.update(db.scalars(select(cls.event_id).where(cls.event_id.in_(batch))))
        if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78:
            used.update(db.scalars(select(m.RealDexExternalBookingMember.event_id).where(m.RealDexExternalBookingMember.event_id.in_(batch))))
    return frozenset(used)


def _financial(db,selection):
    if selection.source_kind=='WALLET_SWAP':
        row,terms,state=wallet_context(db,selection.source_id,selection.context_account_id,selection.wallet_address)
        return calls._wallet_source(db,row,terms,state,selection.wallet_address)
    return bookings._context(db,selection.source_kind,selection.source_id,selection.context_account_id,selection.wallet_address)


def selected_order_source(db,selection):
    """Explicit profile selection; a failed financial proof is never retried as gas."""
    if selection.gas_only:
        from app.portfolio.dex_gas_order_source import gas_source
        return gas_source(db,selection.source_kind,selection.source_id,wallet_address=selection.wallet_address)
    return order_source(db,selection.source_kind,selection.source_id,wallet_address=selection.wallet_address)


def selected_order_record(db,selection):
    if selection.gas_only:
        from app.portfolio.dex_gas_order import gas_order_record,identifier as gas_identifier
        return gas_order_record(db,gas_identifier(selection.source_kind,selection.source_id))
    return order_record(db,identifier(selection.source_kind,selection.source_id))


def existing_receipt_witnesses(db,root,accounts,addresses,limits,*,include_gas=False):
    current=capture_review_baseline(db,root,limits=limits)
    existing={};owners=defaultdict(set)
    if current is not None:
        for archived in current.accepted:
            for raw in archived.inputs.witness_json:
                witness=json.loads(raw);existing[witness['source_kind'],witness['source_id']]=witness
                owners[witness['owner_digest']].add(witness['account_id'])
    gas_mode=include_gas or current is not None and any(item.inputs.source_policy!= 'dex-trades-1' for item in current.accepted)
    gas_accounts=frozenset(accounts)if gas_mode else frozenset()
    trades=_required_sources(db,accounts,external_accounts=frozenset(accounts))
    required=_required_sources(db,accounts,gas_accounts=gas_accounts,external_accounts=frozenset(accounts))if gas_accounts else trades
    witnesses=[];claims=[]
    for kind,source_id in sorted(required):
        gas_only=(kind,source_id)not in trades
        if kind=='EXTERNAL_OBSERVATION':
            from app.portfolio.dex_external_order import order_record as external_order,identifier as external_id
            from app.portfolio.economic_external_booking import capture_external_witness,archived_witness
            record=external_order(db,external_id(source_id));capture=capture_external_witness
        elif gas_only:
            from app.portfolio.dex_gas_order import gas_order_record,identifier as gas_identifier
            from app.portfolio.economic_gas_source import capture_booked_gas_order
            record=gas_order_record(db,gas_identifier(kind,source_id));capture=capture_booked_gas_order
        else:
            record=order_record(db,identifier(kind,source_id));capture=capture_booked_order
        if record is None:raise ProjectionError('Comprueba el bloque de cada recibo ya registrado')
        witness=existing.get((kind,source_id))
        if witness is None:
            owner=record[0].wallet_digest
            if owner in addresses:witness=capture(db,record[0].id,wallet_address=addresses[owner])
            elif kind=='EXTERNAL_OBSERVATION':witness=archived_witness(db,source_id)
            else:witness=_extension_witness(db,kind,source_id,owners,None,gas_only=gas_only)
        claims.append(claim_from_witness(db,witness));witnesses.append(witness)
    return current,tuple(witnesses),tuple(claims)


def capture_receipt_batch(db: Session,root_account_id: str,selections: Sequence[ReceiptSelection],*,
                          wallet_addresses: Sequence[str]=(),limits: ProjectionLimits=ProjectionLimits(),
                          include_gas: bool=False) -> ReceiptBatchInputs:
    """No new salt, autoflush, originals, booking or acceptance is written here."""
    _snapshot(db);_existing_salt(db);root=_uuid(root_account_id)
    if (type(include_gas)is not bool or not isinstance(limits,ProjectionLimits) or not isinstance(selections,Sequence)
            or not 0<len(selections)<=limits.max_claims or any(not isinstance(item,ReceiptSelection)for item in selections)
            or not isinstance(wallet_addresses,Sequence) or len(wallet_addresses)>limits.max_accounts):
        raise ProjectionError('Faltan los recibos y wallets del lote')
    selected={(item.source_kind,item.source_id)for item in selections}
    if len(selected)!=len(selections):raise ProjectionError('El lote repite un recibo')
    accounts,events=original_account_closure(db,root,limits=limits)
    addresses={}
    for value in [*wallet_addresses,*(item.wallet_address for item in selections)]:
        wallet=canonical_wallet_address(value);addresses[_identity(db,'wallet',wallet)]=wallet
    if len(addresses)>limits.max_accounts:raise ProjectionError('Demasiadas wallets en la revisión')
    current,witnesses,claims=existing_receipt_witnesses(db,root,accounts,addresses,limits,
        include_gas=include_gas or any(item.gas_only for item in selections))
    gas_mode=any(item.gas_only for item in selections)or any(isinstance(claim,(GasOrderClaim,ExternalOrderClaim))for claim in claims)
    pending=[];sources=[];by_owner=defaultdict(list)
    for selection in sorted(selections,key=lambda item:(item.source_kind,item.source_id)):
        source=selected_order_source(db,selection)
        if source.account_id!=selection.context_account_id:raise ProjectionError('El recibo pertenece a otro contexto')
        record=selected_order_record(db,selection)
        if record is None:raise ProjectionError('Comprueba el bloque de cada recibo nuevo')
        row,payload=record;owner=_wallet_owner(db,selection.wallet_address)
        if (owner is None or owner.id not in accounts or row.wallet_digest!=source.owner_digest
                or db.scalar(select(m.RealMexcAccountLink.account_ref).where(m.RealMexcAccountLink.account_id==owner.id))is not None):
            raise ProjectionError('Vincula la wallet a una cuenta propia del cierre revisado')
        financial=_financial(db,selection)
        if calls._booked(db,financial):raise ProjectionError('El recibo ya está registrado; revisa su aceptación existente')
        proof=payload['proof'];receipt=json.loads(source.receipt_json)
        at=datetime.fromisoformat(proof['block_at']);trade=_trade(financial.trade_terms,receipt,at)
        if (trade is None)!=selection.gas_only:raise ProjectionError('El recibo no corresponde al perfil de operación o gas seleccionado')
        anchor=ReceiptOrder(row.source_kind,payload['source_id'],row.id,row.payload_digest,row.account_id,owner.id,
            int(row.block_number),int(row.transaction_index),at,proof['block_hash'],proof['block_transactions_ref'],
            int(proof['block_transaction_count']),row.signer_digest,int(row.nonce))
        pending.append(PendingReceipt(anchor,trade,int(receipt['gas_fee_wei']),owner.base_currency,
            max(source.updated_at,row.created_at),selection.trade_event_id,selection.gas_event_ids,
            selection.confirm_new_trade,selection.confirm_new_gas))
        by_owner[source.owner_digest].append((selection,anchor))
        sources.append(_encoded({'source_kind':source.kind,'source_id':source.id,'definition_digest':source.definition_digest,
            'receipt_digest':source.receipt_digest,'updated_at':source.updated_at.isoformat(),'owner_digest':source.owner_digest,
            'order_record_id':row.id,'order_record_digest':row.payload_digest,'context_account_id':row.account_id,
            'target_account_id':owner.id,'base_currency':owner.base_currency,'trade_event_id':selection.trade_event_id,
            'gas_event_ids':list(selection.gas_event_ids),'confirm_new_trade':selection.confirm_new_trade,
            'confirm_new_gas':selection.confirm_new_gas,**({'gas_only':selection.gas_only}if gas_mode else {})})[0])
    # A joint review may insert earlier executions, but may not omit a known
    # earlier unbooked execution of the same wallet or guess a missing index.
    for owner,values in by_owner.items():
        last=max((item.block_number,item.transaction_index)for _,item in values)
        wallet=values[0][0].wallet_address
        for other in calls._sources(db,wallet,owner):
            if (other.kind,other.id)in selected or calls._booked(db,other):continue
            height=int(other.receipt['block_number'])
            if height>last[0]:continue
            if height<last[0]:raise ProjectionError('Incluye la ejecución anterior pendiente '+other.id[:8])
            order=order_record(db,identifier(other.kind,other.id))
            if order is None:raise ProjectionError('Comprueba el orden de la otra ejecución del bloque '+other.id[:8])
            if (int(order[0].block_number),int(order[0].transaction_index))<last:
                raise ProjectionError('Incluye la ejecución anterior pendiente '+other.id[:8])
    return ReceiptBatchInputs(root,accounts,events,tuple(claims),tuple(_encoded(item)[0]for item in witnesses),
        tuple(pending),_unavailable(db,events),tuple((account,original_book_revision(db,account))for account in accounts),
        tuple((account,_encoded({'schema':'orbis.portfolio.review.book.identity.v1','account_id':account,
            'unreadable_revision':current.revision})[1]if isinstance(current,AccountingReadIssue)
            else account_book_revision(db,account))for account in accounts),
        tuple((account,db.get(m.RealAccount,account).name)for account in accounts),_encoded(_heads(db,accounts))[0],
        tuple(sources),current,limits,include_gas)


def prepare_receipt_batch(inputs: ReceiptBatchInputs,*,batch_id: str,recorded_at: datetime,
                          tie_orders: Sequence[TieOrder]=()) -> ReceiptPlan:
    """Pure preparation after closing the capture session and its vault lease."""
    if not isinstance(inputs,ReceiptBatchInputs):raise ProjectionError('Faltan los originales autenticados del lote')
    validate_review_baseline(inputs.current)
    return prepare_receipts(inputs.events,inputs.claims,inputs.receipts,batch_id=batch_id,recorded_at=recorded_at,
        unavailable_event_ids=inputs.unavailable_event_ids,tie_orders=tie_orders,limits=inputs.limits)
