"""Vínculos EOA optativos en la bóveda REAL; no conceden permisos de gasto."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError
from app.portfolio.models import RealAccount, RealWalletIdentitySalt, RealWalletLink
from app.portfolio.repository import RequestConflict
from app.portfolio.wallet_proof import canonical_chain, canonical_wallet_address


_ADDRESS_IN_TEXT = re.compile(r"0x[0-9a-f]{40}|0x[0-9a-f]{64}", re.IGNORECASE)


def _salt(db: Session) -> bytes:
    row = db.get(RealWalletIdentitySalt, 1)
    if row is None:
        row = RealWalletIdentitySalt(id=1, salt_hex=secrets.token_hex(32))
        db.add(row)
        db.flush()
    return bytes.fromhex(row.salt_hex)


def _payload(row: RealWalletLink) -> dict:
    return {"id": row.id, "account_id": row.account_id,
            "chain_id": row.chain_id, "alias": row.alias,
            "verified_at": row.verified_at.isoformat(),
            "spending_authorized": False, "balances_reconciled": False}


def list_wallet_links(db: Session, account_id: str) -> list[dict]:
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Real account not found")
    rows = db.scalars(select(RealWalletLink).where(
        RealWalletLink.account_id == account_id).order_by(
        RealWalletLink.verified_at, RealWalletLink.id)).all()
    return [_payload(row) for row in rows]


def inspect_wallet_link(db: Session, account_id: str, address: str,
                        chain_id: str) -> dict:
    """Clasifica una EOA recién probada sin persistir ni devolver su dirección."""
    address = canonical_wallet_address(address)
    chain_id = canonical_chain(chain_id)
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Real account not found")
    salt = db.get(RealWalletIdentitySalt, 1)
    row = None
    if salt is not None:
        digest = hmac.new(bytes.fromhex(salt.salt_hex),
                          f"{chain_id}:{address}".encode("ascii"), hashlib.sha256).hexdigest()
        row = db.scalar(select(RealWalletLink).where(
            RealWalletLink.chain_id == chain_id,
            RealWalletLink.address_digest == digest))
    result = {"status": "UNLINKED" if row is None else
              "CURRENT_ACCOUNT" if row.account_id == account_id else "OTHER_ACCOUNT",
              "chain_id": chain_id, "control_verified_this_session": True,
              "spending_authorized": False, "balances_reconciled": False}
    if row is not None:
        result["alias"] = row.alias
        if row.account_id != account_id:
            owner = db.get(RealAccount, row.account_id)
            if owner is None:
                raise PortfolioError("Wallet link references a missing REAL account")
            result["linked_account_name"] = owner.name
    return result


def save_wallet_link(db: Session, account_id: str, address: str,
                     chain_id: str, alias: str) -> dict:
    address = canonical_wallet_address(address)
    chain_id = canonical_chain(chain_id)
    if not isinstance(alias, str) or not alias.strip() or len(alias.strip()) > 80:
        raise PortfolioError("Wallet alias must contain 1–80 characters")
    alias = alias.strip()
    if _ADDRESS_IN_TEXT.search(alias) or any(ord(char) < 32 or ord(char) == 127 for char in alias):
        raise PortfolioError("Wallet alias cannot contain an address or control characters")
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Real account not found")
    digest = hmac.new(_salt(db), f"{chain_id}:{address}".encode("ascii"), hashlib.sha256).hexdigest()
    existing = db.scalar(select(RealWalletLink).where(
        RealWalletLink.chain_id == chain_id,
        RealWalletLink.address_digest == digest))
    if existing is not None:
        if existing.account_id != account_id:
            raise RequestConflict("Wallet already linked to another REAL account on this network")
        if existing.alias != alias:
            existing.alias = alias
            db.flush()
        return _payload(existing)
    row = RealWalletLink(id=str(uuid4()), account_id=account_id,
                         chain_id=chain_id, address_digest=digest,
                         alias=alias, verified_at=datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    return _payload(row)


def remove_wallet_link(db: Session, account_id: str, link_id: str) -> None:
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Real account not found")
    row = db.get(RealWalletLink, link_id)
    if row is None or row.account_id != account_id:
        raise PortfolioError("Wallet link not found")
    db.delete(row)
    db.flush()
