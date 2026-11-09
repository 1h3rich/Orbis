"""Local protected deployment journal. Wallet confirmation sends the CREATE."""
from datetime import datetime, timezone
import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.domain import PortfolioError
from app.portfolio import dex_deployments as journal
from app.portfolio.wallet_proof import canonical_wallet_address


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Prepare(Body):
    request_id: UUID
    address: str = Field(min_length=42, max_length=42)
    chain_id: Literal['0x38']
    fee: int = Field(strict=True)
    nonce: str = Field(pattern=r'^(0|[1-9][0-9]{0,19})$')
    gas: str = Field(pattern=r'^[1-9][0-9]{0,7}$')
    gas_price: str = Field(pattern=r'^[1-9][0-9]{0,12}$')
    max_gas_bnb: str = Field(min_length=1, max_length=100)

    @field_validator('fee')
    @classmethod
    def supported_fee(cls, value: int) -> int:
        if value not in (100, 500, 3000, 10000):
            raise ValueError('Pool DEX no compatible')
        return value


class Claim(Body):
    address: str = Field(min_length=42, max_length=42)
    plan_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmation: Literal['DESPLEGAR_EJECUTOR_DEX']


class Hash(Body):
    tx_hash: str = Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class Rejected(Body):
    confirmation: Literal['WALLET_RECHAZO_4001']


class Check(Body):
    evidence: dict


class Recovery(Check):
    address: str = Field(min_length=42, max_length=42)


def create_dex_deployment_router(access, *, authorized, authorized_passive) -> APIRouter:
    router = APIRouter(prefix='/accounts/{account_id}/dex-deployments', tags=['dex-deployments'])

    def run(response, token, action, *, passive=False, address=None, account=None):
        response.headers['Cache-Control'] = 'no-store'
        try:
            with access._lock:
                access.require(token, touch=not passive)
                if address is not None:
                    wallet = canonical_wallet_address(address)
                    digest = hmac.new(access._wallet_proof_key, wallet.encode('ascii'), hashlib.sha256).hexdigest()
                    if (str(account), '0x38', digest) not in access._wallet_verified:
                        raise DexExecutorError('Verifica el control de esta wallet en BNB Chain y esta sesión REAL')
                with access.vault.session() as db:
                    return action(db, datetime.now(timezone.utc))
        except (PortfolioError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={'Cache-Control': 'no-store'}) from None

    @router.get('', dependencies=[Depends(authorized_passive)])
    def history(account_id: UUID, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(response, token, lambda db, now: journal.list_deployments(db, str(account_id), now=now), passive=True)

    @router.post('', dependencies=[Depends(authorized)])
    def prepare(account_id: UUID, body: Prepare, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        values = body.model_dump(exclude={'request_id', 'address', 'chain_id'})
        return run(response, token, lambda db, now: journal.prepare_deployment(db, identifier=str(body.request_id),
            account_id=str(account_id), wallet_address=body.address, now=now, **values), address=body.address, account=account_id)

    @router.post('/{identifier}/claim', dependencies=[Depends(authorized)])
    def claim(account_id: UUID, identifier: UUID, body: Claim, response: Response,
              token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(response, token, lambda db, now: journal.claim_deployment(db, str(identifier), account_id=str(account_id),
            wallet_address=body.address, plan_digest=body.plan_digest, now=now), address=body.address, account=account_id)

    @router.post('/{identifier}/hash', dependencies=[Depends(authorized)])
    def report(account_id: UUID, identifier: UUID, body: Hash, response: Response,
               token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(response, token, lambda db, now: journal.report_deployment(db, str(identifier),
            account_id=str(account_id), tx_hash=body.tx_hash, now=now))

    @router.post('/{identifier}/rejected', dependencies=[Depends(authorized)])
    def rejected(account_id: UUID, identifier: UUID, body: Rejected, response: Response,
                 token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(response, token, lambda db, now: journal.report_deployment(db, str(identifier),
            account_id=str(account_id), rejected=True, now=now))

    @router.post('/{identifier}/check', dependencies=[Depends(authorized)])
    def check(account_id: UUID, identifier: UUID, body: Check, response: Response,
              token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(response, token, lambda db, now: journal.check_deployment(db, str(identifier),
            account_id=str(account_id), evidence=body.evidence, now=now))

    @router.post('/{identifier}/recover', dependencies=[Depends(authorized)])
    def recover(account_id: UUID, identifier: UUID, body: Recovery, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(response, token, lambda db, now: journal.recover_deployment(db, str(identifier),
            account_id=str(account_id), wallet_address=body.address, evidence=body.evidence, now=now), address=body.address, account=account_id)

    return router
