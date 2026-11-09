"""Scoped, read-only admission for changing a portfolio accounting method.

No worker is stopped and no order is cancelled. Confirmation calls this inside
its BEGIN IMMEDIATE: pending submissions and bot activations serialize against
the method change. Existing replies need no admission because they change none.
GRVT execution uses its separate subaccount journals, not this portfolio book.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hmac
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio import models as m
from app.portfolio.dex_receipt_order_source import archived_order_source
from app.portfolio.economic_projection import ProjectionError,_uuid
from app.portfolio.economic_projection_source import _existing_salt,seal_witness,witness_record,is_gas_witness,is_external_witness
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_proof import canonical_wallet_address


class AccountingOperationActive(ProjectionError):
    def __init__(self,report):
        self.report=report
        super().__init__('Hay operativa pendiente en las cuentas revisadas; resuélvela antes de aceptar el criterio')


@dataclass(frozen=True)
class _Scope:
    accounts: frozenset[str]
    contexts: frozenset[str]
    owners: frozenset[str]
    swaps: frozenset[str]
    funding: frozenset[str]
    external: frozenset[tuple[str,str]]


def _scope(db,accounts,witnesses,wallet_addresses):
    from app.portfolio.dex_deployments import _identity
    from app.portfolio.wallet_swap import _identity as swap_identity
    from app.portfolio.wallet_funding import _identity as funding_identity
    salt=_existing_salt(db);accounts=frozenset(_uuid(value)for value in accounts)
    if not accounts or not witnesses:raise ProjectionError('Falta el alcance autenticado de la revisión')
    contexts=set(accounts);owners=set();swaps=set();funding=set();external=set();custody_refs=set()
    for witness in witnesses:
        body={key:value for key,value in witness.items()if key!='seal'}
        if not hmac.compare_digest(witness['seal'],seal_witness(db,body)):
            raise ProjectionError('La revisión perdió una prueba de wallet')
        record=witness_record(db,witness)
        if record is None:raise ProjectionError('La revisión perdió una prueba de orden')
        row,payload=record
        if (row.wallet_digest!=witness['owner_digest'] or row.payload_digest!=witness['order_record_digest']
                or row.account_id!=witness['context_account_id'] or witness['account_id']not in accounts
                or (row.source_kind,payload['source_id'])!=(witness['source_kind'],witness['source_id'])):
            raise ProjectionError('Una prueba de wallet pertenece a otra revisión')
        contexts.add(row.account_id);owners.add(row.wallet_digest)
        if is_external_witness(witness):
            from app.portfolio.dex_external_observation import observation_record
            from app.portfolio.dex_call_resolution import source_record
            from app.portfolio.economic_external_booking import booking_record
            observed=observation_record(db,row.observation_id)[1]
            # Custody and EOA proof context can differ from the ledger and the
            # source context. All attested participants remain in admission;
            # only selected observed source IDs receive the exemption below.
            _,book=booking_record(db,witness['booking_id'])
            for party in book['plan']['custody']:
                if party['basis']=='CURRENT_EOA_AND_LOCAL_WALLET_LINK':
                    if party['account_id']not in accounts:raise ProjectionError('Falta una cartera de la custodia revisada')
                    contexts.add(_uuid(party['context_account_id']))
                    custody_refs.add((observed['source_kind'],party['address_ref']))
            external.add((observed['source_kind'],observed['source_id']))
            _,terms,_,_=source_record(db,observed['source_kind'],observed['source_id'])
            if observed['source_kind']=='WALLET_SWAP':swaps.add(terms['wallet_digest'])
        elif is_gas_witness(witness):
            from app.portfolio.dex_gas_order_source import archived_gas_source
            original=archived_gas_source(db,row.source_kind,payload['source_id'],proof=payload['proof'])
            if original.parent_kind=='WALLET_SWAP':swaps.add(original.original_signer_digest)
        else:
            original=archived_order_source(db,row.source_kind,payload['source_id'],proof=payload['proof'])
            if original.call.kind=='WALLET_SWAP':swaps.add(original.call.terms['wallet_digest'])
    for value in wallet_addresses:
        wallet=canonical_wallet_address(value)
        from app.live_execution.dex_external_observation import source_reference,_ref
        owner=_identity(db,'wallet',wallet)
        if owner in owners or any(_ref(source_reference(kind,salt),'address',wallet)==ref for kind,ref in custody_refs):
            owners.add(owner)
            swaps.add(swap_identity(db,'wallet',wallet));funding.add(funding_identity(db,'wallet',wallet))
    return _Scope(accounts,frozenset(contexts),frozenset(owners),frozenset(swaps),frozenset(funding),frozenset(external))


def operational_report(db: Session,account_ids: Sequence[str],witnesses: Sequence[dict],*,
                       now: datetime,wallet_addresses: Sequence[str]=()) -> dict:
    """Review may remain visible while this report prevents a new acceptance."""
    from app.portfolio import dex_operator_journal as operator,dex_call_resolution as calls
    from app.portfolio import wallet_swap,wallet_funding,wallet_funding_credit
    from app.portfolio import dex_deployments,dex_controls,dex_gas_funding,dex_gas_return
    from app.portfolio import dex_native_resolution as native,dex_deployment_resolution as deployment
    from app.portfolio import dex_gas_cancellation,dex_wallet_cancellation
    from app.portfolio.mexc_withdrawal_journal import pending_withdrawals
    from app.live_execution.spot_bot_store import bot_record
    scope=_scope(db,account_ids,witnesses,wallet_addresses);now=_time(now);blocked=[]
    def add(code,kind,identifier):blocked.append({'code':code,'kind':kind,'id':identifier})
    def relevant(account,owner,aliases):return account in scope.contexts or owner in aliases
    # Durable ACTIVE survives a lost worker or process. A review must not pause
    # that bot (and its protective exits) implicitly to make itself admissible.
    for row in db.scalars(select(m.RealSpotBot).where(m.RealSpotBot.status=='ACTIVE')):
        if row.account_id in scope.accounts:
            bot_record(db,row.id);add('BOT_ACTIVE','MEXC',row.id)
    refs=set(db.scalars(select(m.RealMexcAccountLink.account_ref).where(m.RealMexcAccountLink.account_id.in_(scope.accounts))))
    for row in db.scalars(select(m.RealLiveSpotIntent).where(m.RealLiveSpotIntent.account_ref.in_(refs),
            m.RealLiveSpotIntent.status.in_(('RESERVED','SUBMISSION_UNKNOWN')))):
        add('ORDER_PENDING','MEXC',row.id)
    for ref in sorted(refs):
        if pending_withdrawals(db,ref,now):
            # Do not publish the account fingerprint/UID in the UI report.
            link=db.get(m.RealMexcAccountLink,ref);add('TRANSFER_PENDING','MEXC',link.account_id)
    for row in db.scalars(select(m.RealDexBotRun).where(m.RealDexBotRun.status=='ACTIVE')):
        _,grant,_=operator.grant_record(db,row.grant_id)
        if relevant(row.account_id,grant['wallet_digest'],scope.owners):add('BOT_ACTIVE','DEX',row.id)
    for row in db.scalars(select(m.RealDexOperatorIntent).where(m.RealDexOperatorIntent.status.in_(
            ('RESERVED','SIGNED','BROADCAST_ATTEMPTED','NONCE_OBSERVED')))):
        row,terms,_=operator.intent_record(db,row.id)
        if relevant(row.account_id,terms['owner_digest'],scope.owners)and calls.status(db,'OPERATOR',row.id)is None and ('OPERATOR',row.id)not in scope.external:
            add('ORDER_PENDING','DEX',row.id)
    for row in db.scalars(select(m.RealWalletSwapIntent).where(m.RealWalletSwapIntent.status.in_(('WALLET_REQUESTED','HASH_REPORTED')))):
        row,_,_=wallet_swap.swap_record(db,row.id)
        if relevant(row.account_id,row.wallet_digest,scope.swaps)and calls.status(db,'WALLET_SWAP',row.id)is None and ('WALLET_SWAP',row.id)not in scope.external:
            add('WALLET_PENDING','SWAP',row.id)
    for row in db.scalars(select(m.RealWalletFundingIntent).where(m.RealWalletFundingIntent.status.in_(('WALLET_REQUESTED','HASH_REPORTED')))):
        row,_,_=wallet_funding.funding_record(db,row.id)
        if relevant(row.account_id,row.wallet_digest,scope.funding)and wallet_funding_credit.credit_record(db,row.id)is None:
            add('TRANSFER_PENDING','WALLET',row.id)
    profiles=(
        (m.RealDexDeploymentIntent,('WALLET_REQUESTED','HASH_REPORTED'),dex_deployments.deployment_record,'DEPLOYMENT'),
        (m.RealDexControlIntent,('WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED'),dex_controls.control_record,'CONTROL'),
        (m.RealDexGasFundingIntent,('WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED'),dex_gas_funding.funding_record,'GAS_FUNDING'),
        (m.RealDexGasReturnIntent,('RESERVED','SIGNED','BROADCAST_ATTEMPTED'),dex_gas_return.return_record,'GAS_RETURN'),
        (m.RealDexGasCancellationIntent,('RESERVED','SIGNED','BROADCAST_ATTEMPTED'),dex_gas_cancellation.cancellation_record,'GAS_CANCEL'),
        (m.RealDexWalletCancellationIntent,('WALLET_REQUESTED','HASH_REPORTED'),dex_wallet_cancellation.cancellation_record,'WALLET_CANCEL'),
    )
    for model,statuses,loader,kind in profiles:
        for row in db.scalars(select(model).where(model.status.in_(statuses))):
            row,_,_=loader(db,row.id)
            if not relevant(row.account_id,row.wallet_digest,scope.owners):continue
            if kind=='DEPLOYMENT'and deployment.resolution_record(db,row.id)is not None:continue
            if kind in ('GAS_FUNDING','GAS_RETURN')and native.status(db,kind,row.id)is not None:continue
            add('WALLET_PENDING'if kind in ('DEPLOYMENT','CONTROL','GAS_FUNDING','WALLET_CANCEL')else 'ORDER_PENDING',kind,row.id)
    blocked.sort(key=lambda item:(item['code'],item['kind'],item['id']))
    return {'can_accept':not blocked,'blockers':blocked,'stops_workers':False,'cancels_orders':False,'moves_funds':False}


def require_quiescent(db: Session,account_ids: Sequence[str],witnesses: Sequence[dict],*,
                      now: datetime,wallet_addresses: Sequence[str]=()) -> None:
    report=operational_report(db,account_ids,witnesses,now=now,wallet_addresses=wallet_addresses)
    if not report['can_accept']:raise AccountingOperationActive(report)
