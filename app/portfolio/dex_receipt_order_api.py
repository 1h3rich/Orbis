"""Owner-proven supplemental order observation; never a signing or order route.

Discover the transient transaction hash from its pinned block and original HMAC.
RPC is outside access/SQL leases; token, wallet, source and generation are checked
again before saving. No full address/hash/calldata is added to the journal.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Callable, Generic, Literal, Protocol, TypeVar
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.live_execution.dex_receipt_order import MAX_TRANSACTIONS, OrderSource, Reference, collect_order
from app.live_execution.dex_gas_order import GasSource
from app.live_execution.evm_transaction import quantity, word
from app.portfolio import dex_receipt_order as orders
from app.portfolio.dex_receipt_order_source import order_source, order_reference
from app.portfolio.domain import PortfolioError
from app.portfolio.wallet_proof import canonical_wallet_address


Kind = Literal['WALLET_SWAP', 'OPERATOR', 'CALL_RESOLUTION']


class Wallet(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    address: str = Field(pattern=r'^0x[0-9a-fA-F]{40}$')


def _block_candidates(rpc, source) -> list[str]:
    """Bounded discovery only. The full observation verifies finality/signature."""
    try:
        receipt = json.loads(source.receipt_json)
        if quantity(rpc('eth_chainId', [])) != 56:
            raise ValueError('chain')
        block = rpc('eth_getBlockByNumber', [hex(int(receipt['block_number'])), False])
        if (quantity(block['number']) != int(receipt['block_number'])
                or word(block['hash']) != receipt['block_hash']
                or type(block['transactions']) is not list
                or not 1 <= len(block['transactions']) <= MAX_TRANSACTIONS):
            raise ValueError('pinned block')
        hashes = [word(value) for value in block['transactions']]
        if len(set(hashes)) != len(hashes):
            raise ValueError('duplicate transactions')
        if source.transaction_index is not None:
            if not 0 <= source.transaction_index < len(hashes):
                raise ValueError('index')
            return [hashes[source.transaction_index]]
        return hashes
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('No se pudo localizar el recibo en su bloque original') from None


SourceT = TypeVar('SourceT', bound=OrderSource | GasSource)
SourceCo = TypeVar('SourceCo', bound=OrderSource | GasSource, covariant=True)
RecordT = TypeVar('RecordT')


class SourceReader(Protocol[SourceCo]):
    def __call__(self, db: Session, kind: str, identifier: str, /, *, wallet_address: str) -> SourceCo: ...


class PreviewOrder(Protocol):
    def __call__(self, db: Session, kind: str, source_id: str, /, *, context_account_id: str,
                 wallet_address: str, evidence: dict, now: datetime) -> dict: ...


class ConfirmOrder(Protocol):
    def __call__(self, db: Session, kind: str, source_id: str, /, *, context_account_id: str,
                 wallet_address: str, evidence: dict, preview_digest: str, now: datetime) -> dict: ...


@dataclass(frozen=True)
class OrderProofPorts(Generic[SourceT, RecordT]):
    source: SourceReader[SourceT]
    reference: Callable[[Session, SourceT], Reference]
    record: Callable[[Session, str], RecordT | None]
    identifier: Callable[[str, str], str]
    public_record: Callable[[RecordT], dict]
    preview: PreviewOrder
    confirm: ConfirmOrder


def _order_handlers(access, *, ports: OrderProofPorts[SourceT, RecordT], rpc_factory=None):
    factory = rpc_factory or BnbOperatorReadClient

    def verified(token, account, wallet, *, passive=False):
        access.require(token, touch=not passive)
        digest = hmac.new(access._wallet_proof_key, wallet.encode('ascii'), hashlib.sha256).hexdigest()
        if (str(account), '0x38', digest) not in access._wallet_verified:
            raise DexExecutorError('Verifica el control de esta wallet en BNB Chain')

    def source_for(db, kind, identifier, account, wallet):
        source = ports.source(db, kind, str(identifier), wallet_address=wallet)
        if source.account_id != str(account):
            raise DexExecutorError('El recibo pertenece a otro contexto')
        return source

    def existing(db, source):
        record = ports.record(db, ports.identifier(source.kind, source.id))
        return {**ports.public_record(record), 'created': False} if record is not None else None

    def observe(action, token):
        try:
            with factory() as rpc:
                return action(rpc)
        except PortfolioError:
            raise
        except Exception:
            # Transport exceptions can contain private endpoint URLs. Do not
            # echo them, and do not return diagnostics after a revoked session.
            with access._lock:
                access.require(token, touch=False)
            raise HTTPException(503, 'No se pudo consultar el bloque DEX',
                headers={'Cache-Control': 'no-store'}) from None

    def check(account, kind, identifier, body, response, token,passive=False):
        response.headers['Cache-Control'] = 'no-store'
        try:
            wallet = canonical_wallet_address(body.address)
            with access._lock:
                verified(token, account, wallet,passive=passive)
                generation = access.vault.session_generation
                with access.vault.session() as db:
                    source = source_for(db, kind, identifier, account, wallet)
                    prior = existing(db, source)
                    if prior is not None:
                        return prior
            candidates = observe(lambda rpc: _block_candidates(rpc, source), token)
            with access._lock:
                verified(token, account, wallet,passive=passive)
                if access.vault.session_generation != generation:
                    raise DexExecutorError('La sesión cambió durante la lectura DEX')
                with access.vault.session() as db:
                    current = source_for(db, kind, identifier, account, wallet)
                    if current != source:
                        raise DexExecutorError('El recibo cambió; comprueba de nuevo su orden')
                    prior = existing(db, current)
                    if prior is not None:
                        return prior
                    reference = ports.reference(db, current)
                    matches = [value for value in candidates
                        if hmac.compare_digest(reference('tx', value), source.transaction_ref)]
                    if len(matches) != 1:
                        raise DexExecutorError('El bloque no contiene la transacción del recibo original')
                    tx_hash = matches[0]
            evidence = observe(lambda rpc: collect_order(rpc, tx_hash), token)
            with access._lock:
                verified(token, account, wallet,passive=passive)
                if access.vault.session_generation != generation:
                    raise DexExecutorError('La sesión cambió durante la lectura DEX')
                with access.vault.session() as db:
                    current = source_for(db, kind, identifier, account, wallet)
                    if current != source:
                        raise DexExecutorError('El recibo cambió; comprueba de nuevo su orden')
                    prior = existing(db, current)
                    if prior is not None:
                        return prior
                    if evidence is None:
                        raise DexExecutorError('El recibo todavía no tiene un bloque finalizado')
                    scope = dict(context_account_id=str(account), wallet_address=wallet,
                        evidence=evidence, now=access._dex_bots.now())
                    preview = ports.preview(db, kind, str(identifier), **scope)
                    return ports.confirm(db, kind, str(identifier),
                        preview_digest=preview['preview_digest'], **scope)
        except (PortfolioError, ValueError) as exc:
            with access._lock:
                access.require(token, touch=False)
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from None

    def lookup(account_id, kind, identifier, response, wallet, token):
        response.headers['Cache-Control'] = 'no-store'
        try:
            wallet = canonical_wallet_address(wallet)
            with access._lock:
                verified(token, account_id, wallet, passive=True)
                with access.vault.session() as db:
                    source = source_for(db, kind, identifier, account_id, wallet)
                    return existing(db, source) or {'id': ports.identifier(kind, source.id),
                        'source_kind': kind, 'source_id': source.id, 'saved': False,
                        'applied_to_ledger': False, 'trading_authority': False}
        except (PortfolioError, ValueError) as exc:
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from None
    return check, lookup


def create_receipt_order_router(access, *, authorized, authorized_passive, rpc_factory=None):
    router = APIRouter(prefix='/accounts/{account_id}/dex-receipts/{kind}/{identifier}', tags=['dex-receipt-order'])
    check, lookup = _order_handlers(access, ports=OrderProofPorts(order_source, order_reference,
        orders.order_record, orders.identifier, orders.public_record, orders.preview_order, orders.confirm_order),
        rpc_factory=rpc_factory)

    @router.post('/order-proof', dependencies=[Depends(authorized_passive)])
    def prove_order(account_id: UUID, kind: Kind, identifier: UUID, body: Wallet, response: Response,
                    token: str | None = Header(default=None, alias='X-Orbis-Vault-Token'),
                    passive: str | None = Header(default=None, alias='X-Orbis-Passive')):
        return check(account_id, kind, identifier, body, response, token, passive=passive=='1')

    @router.get('/order-proof', dependencies=[Depends(authorized_passive)])
    def status(account_id: UUID, kind: Kind, identifier: UUID, response: Response,
               wallet: str = Header(alias='X-Orbis-Wallet', pattern=r'^0x[0-9a-fA-F]{40}$'),
               token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return lookup(account_id, kind, identifier, response, wallet, token)
    return router
