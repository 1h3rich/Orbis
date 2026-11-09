"""Private, authenticated external effects preview; no acceptance or sends yet."""
from __future__ import annotations

from datetime import datetime, timedelta
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import Field, field_validator, model_validator

from app.portfolio.domain import AccountSnapshot, EventInput, PortfolioError
from app.portfolio.economic_external_capture import WalletParticipant, capture_external_effects
from app.portfolio.economic_external_effects import AssetScale, EffectAllocation, EffectAnnotation, ManualEffect, ResourceDelta
from app.portfolio.economic_external_plan import NewExternalEvent, event_id, prepare_external_effects
from app.portfolio.economic_projection import ExternalOrderMember, ProjectionError, TieOrder
from app.portfolio.economic_projection_review import _snapshot
from app.portfolio.economic_review_api import StrictModel, Address, Key, ManualOrder, WalletContext, current_snapshots
from app.portfolio.vault import VaultError
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_funding import _encoded
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio import economic_external_acceptance as acceptance, economic_external_booking as booking


class EventReference(StrictModel):
    existing_id: Key | None = None
    new_key: Key | None = None

    @model_validator(mode='after')
    def one(self):
        if (self.existing_id is None) == (self.new_key is None):
            raise ValueError('Selecciona un asiento original o una propuesta nueva')
        return self


class ProposedEvent(StrictModel):
    key: Key
    account_id: Key
    kind: Literal['CONTRIBUTION', 'WITHDRAWAL', 'BUY', 'SELL', 'TRANSFER', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT',
        'TRANSFER_IN', 'TRANSFER_OUT', 'CASH_TRANSFER_IN', 'CASH_TRANSFER_OUT', 'FEE']
    quote_currency: str = Field(min_length=2, max_length=12)
    asset: str | None = Field(default=None, min_length=2, max_length=12)
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None
    gross_amount: str | None = None
    fee: str = '0'
    fee_asset: str | None = Field(default=None, min_length=2, max_length=12)
    note: str | None = Field(default=None, max_length=1000)
    pair_key: Key | None = None
    allow_similar: bool = False

    @field_validator('quantity', 'unit_price', 'amount', 'gross_amount', 'fee')
    @classmethod
    def decimal(cls, value):
        if value is None: return value
        try:
            parsed = Decimal(value)
            if len(value) > 144 or not parsed.is_finite() or abs(parsed.as_tuple().exponent) > 100:
                raise ValueError
        except (ValueError, InvalidOperation):
            raise ValueError('Usa importes decimales finitos como texto') from None
        return value


class Member(StrictModel):
    event: EventReference
    role: Literal['EFFECT', 'GAS']


class Allocation(StrictModel):
    effect_refs: list[str] = Field(min_length=1, max_length=4096)
    events: list[EventReference] = Field(default_factory=list, max_length=8192)
    classification: Literal['TRADE', 'OWN_TRANSFER', 'COMPOSITE', 'EXTERNAL_FLOW', 'ADJUSTMENT', 'EXPENSE', 'NO_LEDGER_CHANGE']
    description: str = Field(min_length=1, max_length=1000)


class Annotation(StrictModel):
    reference: str = Field(min_length=1, max_length=100)
    disposition: Literal['PERMISSION_REVIEWED', 'NON_FINANCIAL', 'MANUAL_EFFECTS_DECLARED', 'MANUAL_COMPLETE', 'UNKNOWN']
    note: str = Field(min_length=1, max_length=1000)


class Delta(StrictModel):
    account_id: Key
    asset: str = Field(min_length=2, max_length=12)
    units: str = Field(pattern=r'^-?[1-9][0-9]{0,77}$')


class Manual(StrictModel):
    id: Key
    annotation_ref: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    deltas: list[Delta] = Field(min_length=1, max_length=4096)
    own_transfer: bool = False


class Scale(StrictModel):
    asset: str = Field(min_length=2, max_length=12)
    decimals: int = Field(ge=0, le=36)


class ExternalReview(StrictModel):
    address: Address
    wallets: list[WalletContext] = Field(default_factory=list, max_length=1023)
    review_id: Key
    recorded_at: str | None = Field(default=None, min_length=20, max_length=40)
    new_events: list[ProposedEvent] = Field(default_factory=list, max_length=8192)
    members: list[Member] = Field(default_factory=list, max_length=8192)
    allocations: list[Allocation] = Field(default_factory=list, max_length=4096)
    annotations: list[Annotation] = Field(max_length=4096)
    manual_effects: list[Manual] = Field(default_factory=list, max_length=4096)
    asset_scales: list[Scale] = Field(default_factory=list, max_length=512)
    tie_orders: list[ManualOrder] = Field(default_factory=list, max_length=10000)

    @field_validator('recorded_at')
    @classmethod
    def stamp(cls, value):
        if value is None: return value
        try: return _time(datetime.fromisoformat(value)).isoformat()
        except (ValueError, PortfolioError): raise ValueError('Fecha UTC inválida') from None


class ExternalConfirmation(ExternalReview):
    recorded_at: str = Field(min_length=20,max_length=40)
    preview_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmation: Literal['ACEPTAR_EFECTOS_EXTERNOS']


def _selection_digest(body,stamp,kind,salt):
    from app.live_execution.dex_external_observation import source_reference,_ref
    reference=source_reference(kind,salt)
    raw=body.model_dump(exclude={'address','wallets','preview_digest','confirmation'})
    raw['recorded_at']=stamp.isoformat()
    raw['owner_ref']=_ref(reference,'address',canonical_wallet_address(body.address))
    raw['wallets']=sorted([{'context_account_id':item.context_account_id,
        'address_ref':_ref(reference,'address',canonical_wallet_address(item.address))}for item in body.wallets],
        key=lambda item:(item['context_account_id'],item['address_ref']))
    return _encoded(raw)[1]


def _declaration(inputs, body):
    from app.portfolio.economic_external_batch import ExternalDeclaration
    at = _time(datetime.fromisoformat(json.loads(inputs.certificate_json)['block_at']))
    new_keys = {item.key for item in body.new_events}
    def resolve(ref):
        if ref.existing_id is not None: return ref.existing_id
        if ref.new_key not in new_keys: raise ProjectionError('Falta la propuesta del asiento seleccionado')
        return event_id(body.review_id, ref.new_key)
    new = []
    for item in body.new_events:
        decimals = {key: Decimal(getattr(item, key)) if getattr(item, key) is not None else None
            for key in ('quantity', 'unit_price', 'amount', 'fee')}
        gross = Decimal(item.gross_amount) if item.gross_amount is not None else None
        if item.kind in ('TRANSFER_IN', 'TRANSFER_OUT'):
            if gross is not None: raise ProjectionError('El coste del traslado nuevo se deriva del inventario original')
            gross = Decimal(0)
        new.append(NewExternalEvent(item.key, item.account_id, EventInput(item.kind, item.quote_currency, at,
            asset=item.asset, note=item.note, gross_override=gross, fee_asset=item.fee_asset, **decimals),
            item.pair_key, item.allow_similar))
    accounts = {event.id: event.account_id for event in inputs.events}
    accounts.update((event_id(body.review_id, item.key), item.account_id) for item in body.new_events)
    members = []
    for item in body.members:
        identifier = resolve(item.event)
        if identifier not in accounts: raise ProjectionError('El asiento no pertenece al historial revisado')
        members.append(ExternalOrderMember(identifier, accounts[identifier], item.role))
    return ExternalDeclaration(inputs, review_id=body.review_id, new_events=tuple(new),
        members=tuple(members), allocations=tuple(EffectAllocation(tuple(item.effect_refs),
            tuple(resolve(ref) for ref in item.events), item.classification, item.description) for item in body.allocations),
        annotations=tuple(EffectAnnotation(item.reference, item.disposition, item.note) for item in body.annotations),
        manual_effects=tuple(ManualEffect(item.id, item.annotation_ref, item.reason,
            tuple(ResourceDelta(value.account_id, value.asset, int(value.units)) for value in item.deltas), item.own_transfer)
            for item in body.manual_effects), asset_scales=tuple(AssetScale(item.asset, item.decimals) for item in body.asset_scales))


def _prepare(inputs, body, stamp):
    declaration=_declaration(inputs,body)
    return prepare_external_effects(inputs,review_id=declaration.review_id,recorded_at=stamp,
        new_events=declaration.new_events,members=declaration.members,allocations=declaration.allocations,
        annotations=declaration.annotations,manual_effects=declaration.manual_effects,asset_scales=declaration.asset_scales,
        tie_orders=tuple(TieOrder(item.account_id, datetime.fromisoformat(item.at), tuple(item.nodes)) for item in body.tie_orders))


def create_external_effects_router(access, *, authorized):
    router = APIRouter(prefix='/accounts/{account_id}/dex-call-resolutions/{kind}/{identifier}/external-effects',
        tags=['external-effects'])

    def verified(token, contexts, *, generation=None):
        access.require(token, touch=False)
        if generation is not None and access.vault.session_generation != generation:
            raise VaultError('The real vault session changed')
        for context, wallet in contexts:
            digest = hmac.new(access._wallet_proof_key, wallet.encode('ascii'), hashlib.sha256).hexdigest()
            if (context, '0x38', digest) not in access._wallet_verified:
                raise ProjectionError('Verifica el control de cada wallet y su contexto en BNB Chain')

    def run(account_id,kind,identifier,body,response,token,*,save):
        response.headers['Cache-Control'] = 'no-store'
        owner = canonical_wallet_address(body.address)
        wallets = tuple(WalletParticipant(item.context_account_id, canonical_wallet_address(item.address)) for item in body.wallets)
        contexts = ((str(account_id), owner), *((item.context_account_id, item.address) for item in wallets))
        def capture(db):
            return capture_external_effects(db, kind, str(identifier), context_account_id=str(account_id),
                owner_address=owner, wallets=wallets)
        def idle():
            if kind == 'OPERATOR': access._dex_bots.require_idle()
        @contextmanager
        def commit_guard():
            with access._lock:
                verified(token,contexts,generation=generation);idle();yield
        try:
            now=_time(access._dex_bots.now())
            stamp=_time(datetime.fromisoformat(body.recorded_at))if body.recorded_at is not None else now
            with access._lock:
                verified(token, contexts); generation = access.vault.session_generation; idle()
                with access.vault.session() as db:
                    inputs=capture(db)
                    from app.portfolio.economic_projection_source import _existing_salt
                    selection=_selection_digest(body,stamp,kind,_existing_salt(db))
                    existing=booking.booking_record(db,booking.identifier(inputs.observation_id))
            if existing is not None:
                if not save:raise ProjectionError('Esta observación ya tiene una revisión aceptada; recupera el resultado guardado')
                if json.loads(inputs.custody_json)!=existing[1]['plan']['custody']:
                    raise ProjectionError('Cambió el vínculo de custodia de la revisión guardada')
                recovered=acceptance.recover_external_acceptance(access.vault,inputs.observation_id,
                    context_account_id=str(account_id),selection_digest=selection)
                if recovered['preview_digest']!=body.preview_digest:
                    raise ProjectionError('La respuesta pertenece a otra revisión externa')
                with commit_guard():return recovered
            # Pure unit reconciliation, full financial replay and cost derivation
            # run without the access lock, SQL transaction or vault lease.
            if stamp > now+timedelta(seconds=5): raise ProjectionError('La revisión no puede fecharse en el futuro')
            plan = _prepare(inputs, body, stamp)
            ties=tuple(TieOrder(item.account_id,datetime.fromisoformat(item.at),tuple(item.nodes))for item in body.tie_orders)
            prepared=acceptance.prepare_external_acceptance(access.vault,inputs,plan,capture=capture,
                generation=generation,selection_digest=selection,tie_orders=ties)if plan.review.body['manual_review_complete']else None
            if save:
                if prepared is None:raise ProjectionError('Revisa los efectos desconocidos antes de aceptar')
                return acceptance.confirm_external_acceptance(access.vault,prepared,capture=capture,
                    preview_digest=body.preview_digest,now=_time(access._dex_bots.now()),generation=generation,
                    commit_guard=commit_guard,wallet_addresses=(owner,*(item.address for item in wallets)))
            baseline = {}
            for accounts, current in inputs.currents:
                baseline.update(current_snapshots(tuple(event for event in inputs.events if event.account_id in accounts), accounts, current))
            snapshots = plan.review.projection.snapshots
            comparisons = [{'account_id': account, 'name': name, 'current': baseline[account][0],
                'current_error': baseline[account][1], 'proposed': _snapshot(snapshots.get(account, AccountSnapshot(account))),
                'changed': baseline[account][0] != _snapshot(snapshots.get(account, AccountSnapshot(account)))} for account, name in inputs.account_names]
            with access._lock:
                verified(token, contexts, generation=generation); idle()
                with access.vault.session() as db:
                    if capture(db).state_digest != inputs.state_digest:
                        raise ProjectionError('Cambió el historial o un vínculo de wallet; revisa de nuevo')
            operating=json.loads(prepared.staged.operating_json)if prepared is not None else {'can_accept':False,'blockers':[{'code':'EFFECTS_UNKNOWN'}]}
            return {'review_id': body.review_id, 'recorded_at': stamp.isoformat(), 'preview_digest': prepared.digest if prepared is not None else plan.digest,
                'state_digest': inputs.state_digest, 'root_account_id': inputs.root_account_id,
                'observation_id': inputs.observation_id, 'order_record_id': inputs.order_id,
                'manual_review_complete': plan.review.body['manual_review_complete'],
                'basis': 'USER_DECLARED_EFFECTS_WITH_OBSERVED_UNIT_CHECKS',
                'accounts': comparisons, 'events_count': len(inputs.events)+len(plan.created_events),
                'events_to_create': len(plan.created_events), 'checked_receipts': len(inputs.claims)+1,
                'created_events': [{'id': event.id, 'account_id': event.account_id, 'kind': event.input.kind,
                    'request_id': event.request_id, 'gross_amount': str(event.input.gross_amount)} for event in plan.created_events],
                'saved': False, 'acceptance_available': True, 'can_accept':operating['can_accept'],'operating':operating,'accounting_acceptance_active': False,
                'effects_proven_complete': False, 'strategy_executed': False,
                'resolves_intent': False, 'moves_funds': False, 'trading_authority': False}
        except (PortfolioError, VaultError, ValueError) as exc:
            with access._lock: access.require(token, touch=False)
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from None
    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(account_id:UUID,kind:Literal['WALLET_SWAP','OPERATOR'],identifier:UUID,body:ExternalReview,response:Response,
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,save=False)

    @router.post('/confirm',dependencies=[Depends(authorized)])
    def confirm(account_id:UUID,kind:Literal['WALLET_SWAP','OPERATOR'],identifier:UUID,body:ExternalConfirmation,response:Response,
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,kind,identifier,body,response,token,save=True)

    @router.get('',dependencies=[Depends(authorized)])
    def saved(account_id:UUID,kind:Literal['WALLET_SWAP','OPERATOR'],identifier:UUID,response:Response,
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.portfolio import dex_call_resolution as original,dex_external_observation as observations
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                access.require(token,touch=False);generation=access.vault.session_generation
                with access.vault.session()as db:
                    row,_,_,_=original.source_record(db,kind,str(identifier))
                    if row.account_id!=str(account_id):raise ProjectionError('Llamada de otro contexto')
            result=acceptance.recover_external_acceptance(access.vault,observations.identifier(kind,str(identifier)),context_account_id=str(account_id))
            with access._lock:
                access.require(token,touch=False)
                if access.vault.session_generation!=generation:raise VaultError('The real vault session changed')
            return result or {'saved':False,'accounting_method_active':False,'resolves_intent':False,'trading_authority':False}
        except (PortfolioError,VaultError,ValueError)as exc:
            with access._lock:access.require(token,touch=False)
            raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})from None
    return router
