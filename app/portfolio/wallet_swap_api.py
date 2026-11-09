"""Puerta local de planes DEX; el gasto sólo se solicita a la wallet elegida."""

from datetime import datetime, timezone
import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.portfolio.domain import PortfolioError
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_swap import WalletSwapError, prepare_swap, claim_swap, report_swap, list_swaps


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Prepare(Body):
    request_id: UUID
    address: str = Field(min_length=42, max_length=42)
    chain_id: Literal["0x38"]
    action: Literal["SWAP", "APPROVE", "REVOKE"]
    token_in: Literal["USDT", "WBNB"]
    amount: str = Field(min_length=1, max_length=100)
    quote_units: str = Field(pattern=r"^[1-9][0-9]{0,77}$")
    fee: int = Field(strict=True)
    slippage_bps: int = Field(strict=True, ge=1, le=500)
    nonce: str = Field(min_length=1, max_length=20)
    gas: str = Field(min_length=1, max_length=7)
    gas_price: str = Field(min_length=1, max_length=19)
    max_gas_bnb: str = Field(min_length=1, max_length=100)


class Claim(Body):
    address: str = Field(min_length=42, max_length=42)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["SOLICITAR_SWAP_WALLET"]


class Hash(Body):
    tx_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


class Rejected(Body):
    confirmation: Literal["WALLET_RECHAZO_4001"]


class BookingSelection(Body):
    address: str = Field(min_length=42, max_length=42)
    trade_event_id: UUID | None = None
    gas_event_ids: list[UUID] = Field(default_factory=list, max_length=100)


class BookingConfirm(BookingSelection):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["REGISTRAR_ACCION_DEX"]


def create_wallet_swap_router(access, *, authorized, authorized_passive) -> APIRouter:
    router = APIRouter(prefix="/accounts/{account_id}/wallet-swaps", tags=["wallet-swaps"])

    def run(response, token, action, *, passive=False, address=None, account=None):
        response.headers["Cache-Control"] = "no-store"
        try:
            with access._lock:
                access.require(token, touch=not passive)
                if address is not None:
                    wallet = canonical_wallet_address(address)
                    digest = hmac.new(access._wallet_proof_key, wallet.encode("ascii"), hashlib.sha256).hexdigest()
                    if (str(account), "0x38", digest) not in access._wallet_verified:
                        raise WalletSwapError("Verifica el control de esta wallet en BNB Chain y esta sesión REAL")
                with access.vault.session() as db:
                    return action(db, datetime.now(timezone.utc))
        except (PortfolioError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("", dependencies=[Depends(authorized_passive)])
    def history(account_id: UUID, response: Response,
                token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return run(response, token, lambda db, now: list_swaps(db, str(account_id), now=now), passive=True)

    @router.post("", dependencies=[Depends(authorized)])
    def prepare(account_id: UUID, body: Prepare, response: Response,
                token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        values = body.model_dump(exclude={"request_id", "address", "chain_id"})
        return run(response, token, lambda db, now: prepare_swap(db, identifier=str(body.request_id),
            account_id=str(account_id), wallet_address=body.address, now=now, **values), address=body.address, account=account_id)

    @router.post("/{identifier}/claim", dependencies=[Depends(authorized)])
    def claim(account_id: UUID, identifier: UUID, body: Claim, response: Response,
              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return run(response, token, lambda db, now: claim_swap(db, str(identifier), account_id=str(account_id),
            wallet_address=body.address, plan_digest=body.plan_digest, now=now), address=body.address, account=account_id)

    @router.post("/{identifier}/hash", dependencies=[Depends(authorized)])
    def report(account_id: UUID, identifier: UUID, body: Hash, response: Response,
               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return run(response, token, lambda db, now: report_swap(db, str(identifier), account_id=str(account_id), tx_hash=body.tx_hash, now=now))

    @router.post("/{identifier}/rejected", dependencies=[Depends(authorized)])
    def rejected(account_id: UUID, identifier: UUID, body: Rejected, response: Response,
                 token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return run(response, token, lambda db, now: report_swap(db, str(identifier), account_id=str(account_id), rejected=True, now=now))

    def selection(body):
        return {"wallet_address": body.address, "trade_event_id": str(body.trade_event_id) if body.trade_event_id else None,
                "gas_event_ids": [str(value) for value in body.gas_event_ids]}

    @router.post("/{identifier}/booking-preview", dependencies=[Depends(authorized)])
    def booking_preview(account_id: UUID, identifier: UUID, body: BookingSelection, response: Response,
                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        from app.portfolio.wallet_swap_booking import preview_booking
        return run(response, token, lambda db, now: preview_booking(db, str(identifier),
            context_account_id=str(account_id), **selection(body)), address=body.address, account=account_id)

    @router.post("/{identifier}/book", dependencies=[Depends(authorized)])
    def book(account_id: UUID, identifier: UUID, body: BookingConfirm, response: Response,
             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        from app.portfolio.wallet_swap_booking import book_swap
        return run(response, token, lambda db, now: book_swap(db, str(identifier), context_account_id=str(account_id),
            expected_revision=body.expected_revision, preview_digest=body.preview_digest, now=now, **selection(body)),
            address=body.address, account=account_id)

    return router
