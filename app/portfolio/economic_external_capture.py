"""Authenticate external effects inputs in one read snapshot, then detach.

The HTTP caller proves every supplied EOA/context in the current vault session.
This port independently checks the original source, sealed order, actual wallet
links and the UNION of their transfer closures. No inferred ownership, RPC,
financial replay, new salt, event or acceptance is created by this capture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_external_observation import source_reference, _ref
from app.portfolio import dex_call_resolution as source, dex_external_observation as observations, dex_external_order as orders
from app.portfolio import models as m
from app.portfolio.economic_acceptance import _heads, _snapshot
from app.portfolio.accounting_view import AccountingInputs, AccountingReadIssue
from app.portfolio.economic_external_effects import PartyBinding
from app.portfolio.economic_projection import OrderClaim, ProjectionError, ProjectionLimits
from app.portfolio.economic_projection_review import original_account_closure
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.economic_receipt_source import existing_receipt_witnesses, _unavailable
from app.portfolio.domain import PortfolioEvent
from app.portfolio.repository import original_book_revision
from app.portfolio.wallet_funding import _encoded
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.dex_deployments import _identity


@dataclass(frozen=True, slots=True)
class WalletParticipant:
    context_account_id: str
    address: str = field(repr=False)


@dataclass(frozen=True)
class ExternalEffectsInputs:
    observation_id: str
    observation_digest: str
    observation_json: str
    order_id: str
    order_digest: str
    certificate_json: str
    root_account_id: str
    accounts: tuple[str, ...]
    events: tuple[PortfolioEvent, ...]
    claims: tuple[OrderClaim, ...]
    witness_json: tuple[str, ...]
    bindings: tuple[PartyBinding, ...]
    # Link IDs/attested delegation basis only; no transient addresses or salt.
    custody_json: str
    custody_seal: str
    currents: tuple[tuple[tuple[str, ...], AccountingInputs | AccountingReadIssue | None], ...]
    unavailable_event_ids: frozenset[str]
    account_names: tuple[tuple[str, str], ...]
    state_digest: str
    not_before: datetime
    limits: ProjectionLimits


def capture_external_effects(db: Session, kind: str, source_id: str, *, context_account_id: str,
        owner_address: str, wallets: Sequence[WalletParticipant] = (),
        limits: ProjectionLimits = ProjectionLimits()) -> ExternalEffectsInputs:
    _snapshot(db); salt = _existing_salt(db)
    owner = canonical_wallet_address(owner_address)
    source._context(db, kind, source_id, context_account_id, owner)
    observed = observations.observation_record(db, observations.identifier(kind, source_id))
    if observed is None:
        raise ProjectionError('Guarda primero la observación externa')
    row, observed_body = observed
    ordered = orders.order_record(db, orders.identifier(row.id))
    if ordered is None:
        raise ProjectionError('Comprueba y guarda primero el orden de la llamada externa')
    order, order_body = ordered; proof = observed_body['proof']
    if (not isinstance(wallets, Sequence) or len(wallets) >= limits.max_accounts
            or any(not isinstance(item, WalletParticipant) for item in wallets)):
        raise ProjectionError('Demasiadas wallets para la revisión externa')
    reference = source_reference(kind, salt)
    requested = [(context_account_id, owner)]
    requested.extend((item.context_account_id, canonical_wallet_address(item.address)) for item in wallets)
    if len(set(requested)) != len(requested):
        raise ProjectionError('La revisión externa repite un contexto de wallet')
    link_salt = bytes.fromhex(db.get(m.RealWalletIdentitySalt, 1).salt_hex)
    parties = {}; custody = []; addresses = {}
    for context, address in requested:
        if db.get(m.RealAccount, context) is None:
            raise ProjectionError('Falta el contexto de una wallet participante')
        digest = hmac.new(link_salt, f'0x38:{address}'.encode('ascii'), hashlib.sha256).hexdigest()
        link = db.scalar(select(m.RealWalletLink).where(m.RealWalletLink.chain_id == '0x38',
            m.RealWalletLink.address_digest == digest))
        if (link is None or db.get(m.RealAccount, link.account_id) is None
                or db.scalar(select(m.RealMexcAccountLink.account_ref).where(
                    m.RealMexcAccountLink.account_id == link.account_id)) is not None):
            raise ProjectionError('Vincula cada wallet a una cartera propia, separada de MEXC')
        ref = _ref(reference, 'address', address)
        role = 'OWNER' if ref == proof['owner_address_ref'] else 'SIGNER' if ref == proof['signer_address_ref'] else 'PROVEN_WALLET'
        binding = PartyBinding(ref, link.account_id, role)
        if ref in parties:
            # A second proof context for the same wallet adds no custody and is
            # ambiguous to retry. Each address has one canonical participant.
            raise ProjectionError('La revisión externa repite una wallet')
        parties[ref] = binding; addresses[_identity(db, 'wallet', address)] = address
        custody.append({'address_ref': ref, 'account_id': link.account_id, 'role': role,
            'basis': 'CURRENT_EOA_AND_LOCAL_WALLET_LINK', 'context_account_id': context,
            'link_id': link.id, 'link_verified_at': link.verified_at.isoformat()})
    owner_binding = parties.get(proof['owner_address_ref'])
    if owner_binding is None or owner_binding.role != 'OWNER':
        raise ProjectionError('Falta la custodia del propietario original')
    if not proof['signer_is_owner'] and proof['signer_address_ref'] not in parties:
        # The authenticated operator source/grant attests delegation. Without a
        # separately linked/proven EOA, its gas belongs to the owner's custody;
        # never let a caller nominate an arbitrary separate gas ledger.
        binding = PartyBinding(proof['signer_address_ref'], owner_binding.account_id, 'SIGNER')
        parties[binding.address_ref] = binding
        custody.append({'address_ref': binding.address_ref, 'account_id': binding.account_id,
            'role': binding.role, 'basis': 'SOURCE_DELEGATION_OWNER_CUSTODY',
            'source_kind': kind, 'source_id': source_id, 'observation_digest': row.payload_digest})
    # New pairs may join previously DISCONNECTED histories. Starting only at
    # the source owner silently loses the other wallet's existing inventory.
    accounts = set(); events = {}; claims = {}; witnesses = {}; currents = []
    for root in sorted({item.account_id for item in parties.values()}):
        if root in accounts:
            continue
        component, originals = original_account_closure(db, root, limits=limits)
        accounts.update(component); events.update((event.id, event) for event in originals)
        if len(accounts) > limits.max_accounts or len(events) > limits.max_events:
            raise ProjectionError('El historial conjunto supera los límites de revisión')
        current, captured, captured_claims = existing_receipt_witnesses(db, root, component, addresses, limits, include_gas=True)
        currents.append((component, current))
        for witness, claim in zip(captured, captured_claims, strict=True):
            raw = _encoded(witness)[0]
            if claim.order_record_id in witnesses and witnesses[claim.order_record_id] != raw:
                raise ProjectionError('Las cuentas discrepan sobre una prueba de orden')
            witnesses[claim.order_record_id] = raw; claims[claim.order_record_id] = claim
    if len(claims) >= limits.max_claims:
        raise ProjectionError('Demasiados recibos en el historial conjunto')
    accounts = tuple(sorted(accounts)); originals = tuple(events[key] for key in sorted(events))
    custody_json = _encoded(sorted(custody, key=lambda item: item['address_ref']))[0]
    attestation = {'observation_id': row.id, 'observation_digest': row.payload_digest,
        'order_id': order.id, 'order_digest': order.payload_digest, 'root_account_id': owner_binding.account_id,
        'custody': custody_json}
    custody_seal = hmac.new(salt, b'orbis-external-custody-v1:'+_encoded(attestation)[0].encode(), hashlib.sha256).hexdigest()
    names = tuple((account, db.get(m.RealAccount, account).name) for account in accounts)
    state = {'schema': 'orbis.portfolio.external.effects.inputs.v1',
        'observation_id': row.id, 'observation_digest': row.payload_digest,
        'order_id': order.id, 'order_digest': order.payload_digest, 'custody': custody_json,
        'accounts': [{'id': account, 'name': name, 'base_currency': db.get(m.RealAccount, account).base_currency,
            'revision': original_book_revision(db, account)} for account, name in names],
        'heads': _heads(db, accounts), 'witnesses': sorted(witnesses.values()),
        'unavailable': sorted(_unavailable(db, originals))}
    return ExternalEffectsInputs(row.id, row.payload_digest, _encoded(proof)[0], order.id, order.payload_digest,
        _encoded(order_body['proof'])[0], owner_binding.account_id, accounts, originals,
        tuple(claims[key] for key in sorted(claims)), tuple(witnesses[key]for key in sorted(witnesses)),
        tuple(parties[key] for key in sorted(parties)), custody_json, custody_seal,
        tuple(currents), _unavailable(db, originals), names, _encoded(state)[1], max(row.created_at, order.created_at), limits)
