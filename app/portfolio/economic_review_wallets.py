"""Private wallet scope for an accounting review; no address persistence or RPC.

Callers authorize every supplied EOA/context before entering this read snapshot.
Public descriptors use existing local link IDs, never address or owner digests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio import models as m
from app.portfolio.accounting_view import capture_review_baseline
from app.portfolio.economic_projection import ProjectionError, ProjectionLimits
from app.portfolio.economic_projection_review import ALL_RECEIPTS, _chunks, _required_sources
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.wallet_proof import canonical_wallet_address


@dataclass(frozen=True, slots=True)
class ReviewWallet:
    link_id: str
    account_id: str
    address: str = field(repr=False)


def review_wallets(db: Session, accounts: tuple[str, ...], addresses: Sequence[str], *,
                   limits: ProjectionLimits) -> tuple[tuple[ReviewWallet, ...], list[dict]]:
    """Match transient, already-proven addresses to links inside the exact closure."""
    _existing_salt(db)
    salt = bytes.fromhex(db.get(m.RealWalletIdentitySalt, 1).salt_hex)
    supplied = {}
    for value in addresses:
        address = canonical_wallet_address(value)
        digest = hmac.new(salt, f'0x38:{address}'.encode('ascii'), hashlib.sha256).hexdigest()
        supplied[digest] = address
    if len(supplied) > limits.max_accounts:
        raise ProjectionError('Demasiadas wallets para la revisión')
    targets = []
    descriptors = []
    matched = set()
    for batch in _chunks(accounts):
        for link in db.scalars(select(m.RealWalletLink).where(
                m.RealWalletLink.account_id.in_(batch), m.RealWalletLink.chain_id == '0x38')):
            if len(descriptors) >= limits.max_accounts:
                raise ProjectionError('Demasiados vínculos de wallet para la revisión')
            account = db.get(m.RealAccount, link.account_id)
            if account is None:
                raise ProjectionError('El vínculo wallet perdió su cuenta REAL')
            provided = link.address_digest in supplied
            descriptors.append({'id': link.id, 'account_id': link.account_id, 'account_name': account.name,
                                'alias': link.alias, 'provided': provided})
            if provided:
                if db.scalar(select(m.RealMexcAccountLink.account_ref).where(
                        m.RealMexcAccountLink.account_id == link.account_id)) is not None:
                    raise ProjectionError('La wallet necesita una cartera propia, separada de MEXC')
                matched.add(link.address_digest)
                targets.append(ReviewWallet(link.id, link.account_id, supplied[link.address_digest]))
    if matched != supplied.keys():
        raise ProjectionError('Vincula cada wallet a una cartera propia del cierre revisado')
    return (tuple(sorted(targets, key=lambda item: item.link_id)),
            sorted(descriptors, key=lambda item: (item['account_id'], item['id'])))


def scope_summary(db: Session, root: str, accounts: tuple[str, ...], descriptors: list[dict],
                  visible: set[tuple[str, str]], *, include_gas: bool, limits: ProjectionLimits) -> dict:
    """Identify booked receipts still requiring an address, without opening another wallet.

    Sealed historical witnesses remain usable without reconnecting their wallet,
    exactly as in existing_receipt_witnesses. This summary grants no authority to
    read a missing wallet's receipts or to accept a partial accounting closure.
    """
    baseline = capture_review_baseline(db, root, limits=limits)
    witnessed = set()
    if baseline is not None:
        for accepted in baseline.accepted:
            for raw in accepted.inputs.witness_json:
                witness = json.loads(raw)
                witnessed.add((witness['source_kind'], witness['source_id']))
    gas_mode = include_gas or baseline is not None and any(
        item.inputs.source_policy != 'dex-trades-1' for item in baseline.accepted)
    required = _required_sources(db, accounts, gas_accounts=frozenset(accounts) if gas_mode else frozenset(),external_accounts=frozenset(accounts))
    if len(required) > limits.max_claims:
        raise ProjectionError('Demasiados recibos para una revisión; delimita el historial')
    return {'schema': 'orbis.portfolio.economic.wallet.scope.v1', 'wallets': descriptors,
            'missing_receipts': [{'source_kind': kind, 'source_id': identifier}
                                 for kind, identifier in sorted(required - visible - witnessed)],
            'moves_funds': False, 'trading_authority': False}
