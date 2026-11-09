"""EOA-scoped account review; private data, no RPC/signing/trading authority."""
from __future__ import annotations

from contextlib import contextmanager
from bisect import bisect_left,bisect_right
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from decimal import Context,ROUND_HALF_EVEN,localcontext
import hashlib,hmac,json
from typing import Annotated,Literal
from uuid import UUID,uuid4

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from sqlalchemy import select

from app.portfolio import economic_acceptance as archive,economic_receipt_acceptance as batches,models as m
from app.portfolio.dex_deployments import _identity
from app.portfolio.domain import CALC_PRECISION,AccountSnapshot,PortfolioError,fold_events
from app.portfolio.economic_operational_guard import operational_report
from app.portfolio.economic_projection import ProjectionError,ProjectionLimits,TieOrder
from app.portfolio.economic_projection_review import original_account_closure,_snapshot
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.economic_receipt_source import ReceiptSelection,existing_receipt_witnesses,_unavailable
from app.portfolio.wallet_swap_booking import _trade,_amount,_gas_units,_matches_trade
from app.portfolio.vault import VaultError
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_funding_credit import _wallet_owner
from app.portfolio.wallet_proof import canonical_wallet_address


Key=Annotated[str,Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
Address=Annotated[str,Field(pattern=r'^0x[0-9a-fA-F]{40}$')]
Digest=Annotated[str,Field(pattern=r'^[0-9a-f]{64}$')]


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class WalletContext(StrictModel):
    context_account_id: Key
    address: Address


class Receipt(StrictModel):
    source_kind: Literal['WALLET_SWAP','OPERATOR','CALL_RESOLUTION','DEPLOYMENT','CONTROL','GAS_FUNDING',
        'GAS_RETURN','DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION']
    source_id: Key
    context_account_id: Key|None=None
    wallet_address: Address|None=None
    trade_event_id: Key|None=None
    gas_event_ids: list[Key]=Field(default_factory=list,max_length=100)
    confirm_new_trade: bool=False
    confirm_new_gas: bool=False
    gas_only: bool=False

    @model_validator(mode='after')
    def profile(self):
        if (self.gas_only and (self.trade_event_id is not None or self.confirm_new_trade)
                or not self.gas_only and self.source_kind not in ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION')):
            raise ValueError('Selecciona el perfil de gas sin una operación ficticia')
        return self


class ManualOrder(StrictModel):
    account_id: Key
    at: str=Field(min_length=20,max_length=40)
    nodes: list[Annotated[str,Field(pattern=r'^(event|chain):[0-9a-f-]{36}$')]]=Field(min_length=1,max_length=10000)

    @field_validator('at')
    @classmethod
    def time(cls,value):
        try:return _time(datetime.fromisoformat(value)).isoformat()
        except (ValueError,PortfolioError):raise ValueError('Fecha UTC inválida')from None


class Review(StrictModel):
    address: Address
    wallets: list[WalletContext]=Field(default_factory=list,max_length=10000)
    receipts: list[Receipt]=Field(default_factory=list,max_length=100000)
    tie_orders: list[ManualOrder]=Field(default_factory=list,max_length=10000)
    include_gas: bool=False
    order_options_digest: Digest|None=None


class Confirmation(Review):
    mode: Literal['EXISTING','BATCH']
    batch_id: Key|None=None
    recorded_at: str=Field(min_length=20,max_length=40)
    preview_digest: Digest
    confirmation: Literal['ACEPTAR_CRITERIO_CONTABLE']

    @field_validator('recorded_at')
    @classmethod
    def time(cls,value):
        try:return _time(datetime.fromisoformat(value)).isoformat()
        except (ValueError,PortfolioError):raise ValueError('Fecha UTC inválida')from None


class Result(StrictModel):
    address: Address
    preview_digest: Digest


class Sources(StrictModel):
    address: Address
    include_gas: bool=False
    include_wallets: bool=False
    wallets: list[WalletContext]=Field(default_factory=list,max_length=10000)

    @model_validator(mode='after')
    def scope(self):
        if self.wallets and not self.include_wallets:
            raise ValueError('Activa la revisión de wallets para incluir otros contextos')
        pairs=[(item.context_account_id,canonical_wallet_address(item.address))for item in self.wallets]
        if len(pairs)!=len(set(pairs)):
            raise ValueError('La revisión repite un contexto de wallet')
        return self


def current_snapshots(events,accounts,current):
    """Pure actual-book replay; proposed receipt rows never enter this baseline."""
    from app.portfolio.accounting_view import AccountingReadIssue,replay_accounting_view,validate_review_baseline
    with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
        if isinstance(current,AccountingReadIssue):
            validate_review_baseline(current)
            return {account:(None,current.error)for account in accounts}
        if current is not None:
            try:
                snapshots=replay_accounting_view(current).snapshots
                return {account:(_snapshot(snapshots.get(account,AccountSnapshot(account))),None)for account in accounts}
            except PortfolioError as exc:
                # A failed accepted method cannot fall back to registration order.
                return {account:(None,str(exc))for account in accounts}
        grouped=defaultdict(list)
        for event in events:grouped[event.account_id].append(event)
        result={}
        for account in accounts:
            try:result[account]=(_snapshot(fold_events(account,grouped[account])),None)
            except PortfolioError as exc:result[account]=(None,str(exc))
        return result


def create_economic_review_router(access,*,authorized,authorized_passive,clock=None):
    router=APIRouter(prefix='/accounts/{context_account_id}/economic-review',tags=['economic-review'])
    clock=clock or (lambda:datetime.now(timezone.utc));limits=ProjectionLimits()

    def normalized(context,body):
        wallet=canonical_wallet_address(body.address)
        receipts=tuple(ReceiptSelection(item.source_kind,item.source_id,item.context_account_id or context,
            item.wallet_address or wallet,item.trade_event_id,tuple(item.gas_event_ids),item.confirm_new_trade,item.confirm_new_gas,item.gas_only)
            for item in getattr(body,'receipts',()))
        proof_contexts={(context,wallet),*((item.context_account_id,item.wallet_address)for item in receipts),
            *((item.context_account_id,canonical_wallet_address(item.address))for item in getattr(body,'wallets',()))}
        wallets=tuple(sorted({value for _,value in proof_contexts}))
        ties=tuple(TieOrder(item.account_id,datetime.fromisoformat(item.at),tuple(item.nodes))for item in getattr(body,'tie_orders',()))
        return wallet,receipts,tuple(sorted(proof_contexts)),wallets,ties

    def authorized_contexts(token,contexts,generation=None):
        access.require(token,touch=False)
        if generation is not None and access.vault.session_generation!=generation:
            raise VaultError('The real vault session changed')
        for account,wallet in contexts:
            digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if (account,'0x38',digest)not in access._wallet_verified:
                raise ProjectionError('Verifica el control de cada wallet y contexto de la revisión')

    def root_for(db,wallet):
        _existing_salt(db);owner=_wallet_owner(db,wallet)
        if owner is None or db.scalar(select(m.RealMexcAccountLink.account_ref).where(m.RealMexcAccountLink.account_id==owner.id))is not None:
            raise ProjectionError('Vincula la wallet a su cartera propia antes de revisar el criterio')
        return owner.id

    def capture_scope(token,contexts,wallet):
        with access._lock:
            authorized_contexts(token,contexts);generation=access.vault.session_generation
            with access.vault.session()as db:root=root_for(db,wallet)
        return root,generation

    @contextmanager
    def commit_guard(token,contexts,wallet,root,generation):
        # The access lock is taken BEFORE the vault lease. No lock inversion
        # with idle protection, and revocation cannot race the final commit.
        with access._lock:
            authorized_contexts(token,contexts,generation)
            with access.vault.session()as db:
                if root_for(db,wallet)!=root:raise ProjectionError('El vínculo de la wallet cambió durante la revisión')
            yield

    def existing_witnesses(root,wallets,include_gas=False):
        with access.vault.session()as db:
            archive._snapshot(db);accounts,_=original_account_closure(db,root,limits=limits)
            addresses={_identity(db,'wallet',wallet):wallet for wallet in wallets}
            _,witnesses,_=existing_receipt_witnesses(db,root,accounts,addresses,limits,include_gas=include_gas)
            if not witnesses:raise ProjectionError('Todavía no hay recibos registrados que revisar')
            return witnesses

    def summary(db,prepared,baseline,mode,batch_id,stamp,wallets):
        draft=prepared.draft;review=draft['review']
        report=operational_report(db,tuple(draft['prefix']),review['witnesses'],now=_time(clock()),wallet_addresses=wallets)
        comparisons=[]
        for item in review['comparisons']:
            account=db.get(m.RealAccount,item['account_id'])
            actual,error=baseline[item['account_id']]
            comparisons.append({'account_id':item['account_id'],'name':account.name,'registration':item['before'],
                'registration_error':item['before_error'],'current':actual,'current_error':error,
                'network':item['after'],'changed':actual!=item['after']})
        batch=draft.get('receipt_batch');new_count=sum(not ref['existing']for row in batch['receipts']for ref in row['events'])if batch else 0
        return {'id':archive.identifier(prepared.digest),'preview_digest':prepared.digest,'mode':mode,'batch_id':batch_id,
            'recorded_at':stamp.isoformat(),'root_account_id':review['root_account_id'],
            'review':{'events_count':review['events_count'],'checked_receipts':review['claimed_transactions'],
                'receipts_to_register':len(batch['receipts'])if batch else 0,'events_to_create':new_count,
                'adjusted_transfer_legs':review['adjusted_transfer_legs'],'accounts':comparisons},
            'operating':report,'saved':False,'moves_funds':False,'trading_authority':False}

    def fail(exc,token):
        with access._lock:access.require(token,touch=False)
        raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})from None

    @router.post('/sources',dependencies=[Depends(authorized_passive)])
    def sources(context_account_id:UUID,body:Sources,response:Response,
                token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            from app.portfolio import dex_call_booking as calls,dex_receipt_booking as bookings,wallet_swap_booking as swaps
            from app.portfolio.dex_receipt_order import identifier as order_id,order_record
            from app.portfolio.dex_gas_order import identifier as gas_order_id,gas_order_record
            from app.portfolio.economic_review_sources import review_sources
            wallet,_,contexts,wallets,_=normalized(str(context_account_id),body);root,generation=capture_scope(token,contexts,wallet)
            with commit_guard(token,contexts,wallet,root,generation),access.vault.session()as db:
                archive._snapshot(db);accounts,events=original_account_closure(db,root,limits=limits)
                used=_unavailable(db,events)
                from app.portfolio.economic_review_wallets import ReviewWallet,review_wallets,scope_summary
                if body.include_wallets:
                    targets,descriptors=review_wallets(db,accounts,wallets,limits=limits)
                else:targets=(ReviewWallet('',root,wallet),)
                available_by_account=defaultdict(list)
                for event in events:
                    if event.id not in used:available_by_account[event.account_id].append(event)
                rows=[];seen=set()
                for target in targets:
                    available=sorted(available_by_account[target.account_id],key=lambda event:(event.occurred_at,event.id))
                    times=[event.occurred_at for event in available]
                    digest=hmac.new(access._wallet_proof_key,target.address.encode('ascii'),hashlib.sha256).hexdigest()
                    for source,gas_only in review_sources(db,target.address,include_gas=body.include_gas):
                        if (source.kind,source.id)in seen:raise ProjectionError('Un recibo aparece en más de una wallet')
                        seen.add((source.kind,source.id))
                        booked=swaps._status(db,source.id)if source.kind=='WALLET_SWAP'else bookings.status(db,source.kind,source.id)
                        if booked['booked']and booked['target_account_id']not in accounts:continue
                        at=datetime.fromisoformat(source.receipt['block_at']);trade=_trade(source.trade_terms,source.receipt,at)
                        trades=[];gas=[];gas_wei=int(source.receipt['gas_fee_wei'])
                        if not booked['booked']:
                            window=timedelta(minutes=2)
                            for event in available[bisect_left(times,at-window):bisect_right(times,at+window)]:
                                if trade is not None and _matches_trade(event.input,trade):
                                    trades.append({'id':event.id,'occurred_at':event.input.occurred_at.isoformat(),
                                        'quantity':str(event.input.quantity),'gross_amount':str(event.input.gross_amount)})
                                try:native=_gas_units(event.input)
                                except PortfolioError:continue
                                if 0<native<=gas_wei:gas.append({'id':event.id,'occurred_at':event.input.occurred_at.isoformat(),'amount':str(_amount(native))})
                        context=db.get(m.RealAccount,source.account_id)
                        rows.append({'source_kind':source.kind,'source_id':source.id,'context_account_id':source.account_id,
                            'context_name':context.name,'context_verified':(source.account_id,'0x38',digest)in access._wallet_verified,
                            'action':'GAS'if gas_only else trade.kind,'occurred_at':at.isoformat(),'quantity':None if gas_only else str(trade.quantity),
                            'gross_amount':None if gas_only else str(trade.gross_amount),'gas_amount':str(_amount(gas_wei)),
                            'booked':booked['booked'],'order_checked':(gas_order_record(db,gas_order_id(source.kind,source.id))if gas_only
                                else order_record(db,order_id(source.kind,source.id)))is not None,
                            'trade_candidates':trades,'gas_candidates':gas,
                            **({'gas_only':gas_only,'gas_action':source.action}if body.include_gas else {}),
                            **({'wallet_link_id':target.link_id,'wallet_account_id':target.account_id}if body.include_wallets else {})})
                        if len(rows)>limits.max_claims:raise ProjectionError('Demasiados recibos para una revisión; delimita el historial')
                rows.sort(key=lambda item:(item['occurred_at'],item['source_kind'],item['source_id']))
                result={'root_account_id':root,'root_account_name':db.get(m.RealAccount,root).name,'sources':rows}
                if body.include_wallets:
                    result['wallet_scope']=scope_summary(db,root,accounts,descriptors,
                        {(row['source_kind'],row['source_id'])for row in rows},include_gas=body.include_gas,limits=limits)
                return result
        except (PortfolioError,VaultError,ValueError)as exc:fail(exc,token)

    @router.post('/order-options',dependencies=[Depends(authorized)])
    def order_choices(context_account_id:UUID,body:Review,response:Response,
                      token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            from app.portfolio.accounting_view import validate_review_baseline
            from app.portfolio.economic_receipt_source import capture_receipt_batch
            from app.portfolio.economic_order_options import order_options
            wallet,receipts,contexts,wallets,_=normalized(str(context_account_id),body)
            root,generation=capture_scope(token,contexts,wallet)
            with commit_guard(token,contexts,wallet,root,generation),access.vault.session()as db:
                if receipts:
                    inputs=capture_receipt_batch(db,root,receipts,wallet_addresses=wallets,limits=limits,include_gas=body.include_gas)
                    current=inputs.current;parents=json.loads(inputs.parents_json)
                else:
                    archive._snapshot(db);accounts,_=original_account_closure(db,root,limits=limits)
                    addresses={_identity(db,'wallet',value):value for value in wallets}
                    current,witnesses,_=existing_receipt_witnesses(db,root,accounts,addresses,limits,include_gas=body.include_gas)
                    if not witnesses:raise ProjectionError('Todavía no hay recibos registrados que revisar')
                    inputs,parents,_=archive._live_capture(db,root,witnesses,wallets,limits)
                names={account:db.get(m.RealAccount,account).name for account in inputs.accounts}
            validate_review_baseline(current)
            accepted=[]
            for archived in current.accepted if current is not None else ():
                payload=json.loads(archived.payload_json)
                accepted.extend(item for item in archive._ties(payload['draft']['review'])
                    if parents.get(item.account_id) is not None and parents[item.account_id]['id']==payload['id'])
            result=order_options(inputs,names=names,parents=parents,accepted_orders=tuple(accepted))
            with commit_guard(token,contexts,wallet,root,generation):return result
        except (PortfolioError,VaultError,ValueError)as exc:fail(exc,token)

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(context_account_id: UUID,body: Review,response:Response,
                token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            context=str(context_account_id);wallet,receipts,contexts,wallets,ties=normalized(context,body)
            root,generation=capture_scope(token,contexts,wallet);stamp=_time(clock());batch_id=str(uuid4())if receipts else None
            if receipts:
                prepared=batches.prepare_receipt_acceptance_review(access.vault,root,receipts,batch_id=batch_id,
                    recorded_at=stamp,wallet_addresses=wallets,tie_orders=ties,limits=limits,include_gas=body.include_gas)
                current=prepared.inputs.current
            else:
                witnesses=existing_witnesses(root,wallets,body.include_gas)
                prepared=archive.prepare_acceptance_review(access.vault,root,witnesses,wallet_addresses=wallets,tie_orders=ties,limits=limits)
                current=prepared.current
            baseline=current_snapshots(prepared.inputs.events,prepared.inputs.accounts,current)
            with commit_guard(token,contexts,wallet,root,generation),access.vault.session()as db:
                archive._snapshot(db)
                if body.order_options_digest is not None:
                    from app.portfolio.economic_order_options import revision
                    names={account:db.get(m.RealAccount,account).name for account in prepared.inputs.accounts}
                    if revision(prepared.inputs,names=names,parents=prepared.draft['parents'])!=body.order_options_digest:
                        raise ProjectionError('Cambió el historial o la selección; actualiza el orden antes de revisar')
                return summary(db,prepared,baseline,'BATCH'if receipts else 'EXISTING',batch_id,stamp,wallets)
        except (PortfolioError,VaultError,ValueError)as exc:fail(exc,token)

    @router.post('/confirm',dependencies=[Depends(authorized)])
    def confirm(context_account_id:UUID,body:Confirmation,response:Response,
                token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet,receipts,contexts,wallets,ties=normalized(str(context_account_id),body)
            root,generation=capture_scope(token,contexts,wallet)
            guard=lambda:commit_guard(token,contexts,wallet,root,generation)
            if body.mode=='BATCH':
                if not receipts or body.batch_id is None:raise ProjectionError('Falta el lote revisado')
                saved=batches.confirm_receipt_acceptance(access.vault,root,receipts,batch_id=body.batch_id,
                    recorded_at=datetime.fromisoformat(body.recorded_at),preview_digest=body.preview_digest,
                    now=_time(clock()),wallet_addresses=wallets,tie_orders=ties,limits=limits,commit_guard=guard,include_gas=body.include_gas)
            else:
                if receipts or body.batch_id is not None:raise ProjectionError('La revisión no corresponde a un lote nuevo')
                with access.vault.session()as db:
                    archive._snapshot(db);record=db.get(m.RealEconomicAcceptance,archive.identifier(body.preview_digest))
                    payload=archive._lineage(db,record.id)[1]if record is not None else None
                if payload is not None:
                    if payload['draft']['schema']not in(archive.DRAFT,archive.GAS_DRAFT):raise ProjectionError('La respuesta pertenece a otro tipo de revisión')
                    witnesses=payload['draft']['review']['witnesses']
                else:witnesses=existing_witnesses(root,wallets,body.include_gas)
                saved=archive.confirm_acceptance(access.vault,root,witnesses,wallet_addresses=wallets,
                    preview_digest=body.preview_digest,now=_time(clock()),tie_orders=ties,limits=limits,commit_guard=guard)
            with guard(),access.vault.session()as db:
                head=archive._heads(db,(root,))[root]
                return {**saved,'is_current':head is not None and head['id']==saved['id']}
        except (PortfolioError,VaultError,ValueError)as exc:fail(exc,token)

    @router.post('/result',dependencies=[Depends(authorized_passive)])
    def result(context_account_id:UUID,body:Result,response:Response,
               token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet,_,contexts,_,_=normalized(str(context_account_id),body);root,generation=capture_scope(token,contexts,wallet)
            identifier=archive.identifier(body.preview_digest)
            with access.vault.session()as db:
                archive._snapshot(db);record=db.get(m.RealEconomicAcceptance,identifier)
                archived=archive.capture_acceptance(db,identifier)if record is not None else None
                if archived is not None and archived.inputs.root_account_id!=root:raise ProjectionError('La aceptación pertenece a otra cartera')
            if archived is None:
                with commit_guard(token,contexts,wallet,root,generation):return {'id':identifier,'saved':False}
            archive.replay_acceptance(archived)
            with commit_guard(token,contexts,wallet,root,generation),access.vault.session()as db:
                row,payload=archive._lineage(db,identifier);head=archive._heads(db,(root,))[root]
                return {**archive._result(row,created=False),'is_current':head is not None and head['id']==identifier,
                    'receipt_batch':payload['draft']['schema']in(archive.BATCH_DRAFT,archive.GAS_BATCH_DRAFT)}
        except (PortfolioError,VaultError,ValueError)as exc:fail(exc,token)
    return router
