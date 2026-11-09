"""Reviewed owner controls. Public chain reads never hold the REAL access lock."""
import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import Field, model_validator

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_state import BnbReadClient, read_executor_state
from app.live_execution.dex_policy import PolicyLimits
from app.live_execution.dex_allowance import allowance_cap
from app.portfolio import dex_controls as controls
from app.portfolio.dex_deployments import deployment_record, _identity as deployment_identity
from app.portfolio.domain import PortfolioError
from app.portfolio.dex_deployment_api import Body, Hash, Rejected, Check
from app.portfolio.wallet_proof import canonical_wallet_address


class Limits(Body):
    expires_at: int = Field(strict=True, ge=1, le=2**64-1)
    cooldown_seconds: int = Field(strict=True, ge=0, le=86400)
    max_entries: int = Field(strict=True, ge=0, le=1000)
    twap_seconds: int = Field(strict=True, ge=300, le=86400)
    max_deviation_bps: int = Field(strict=True, ge=1, le=500)
    max_buy_per_order: str = Field(pattern=r'^(0|[1-9][0-9]{0,77})$')
    buy_budget: str = Field(pattern=r'^(0|[1-9][0-9]{0,77})$')
    max_exposure: str = Field(pattern=r'^(0|[1-9][0-9]{0,77})$')
    max_exit_notional: str = Field(pattern=r'^(0|[1-9][0-9]{0,77})$')
    max_realized_loss: str = Field(pattern=r'^(0|[1-9][0-9]{0,77})$')
    max_gas_price: str = Field(pattern=r'^[1-9][0-9]{0,12}$')
    max_wbnb_acquired: str = Field(pattern=r'^[1-9][0-9]{0,38}$')
    allow_buy: bool = Field(strict=True)
    allow_sell: bool = Field(strict=True)

    @model_validator(mode='after')
    def bounded(self):
        PolicyLimits.parse(self.model_dump())
        return self


class StateRequest(Body):
    deployment_id: UUID
    address: str = Field(min_length=42, max_length=42)
    operator: str | None = Field(default=None, min_length=42, max_length=42)


class Preparation(StateRequest):
    request_id: UUID
    action: Literal['CONFIGURE', 'STOP', 'APPROVE', 'REVOKE']
    limits: Limits | None = None
    token: Literal['USDT', 'WBNB'] | None = None
    allowance_units: str | None = Field(default=None, pattern=r'^(0|[1-9][0-9]{0,77})$')
    gas: str = Field(pattern=r'^[1-9][0-9]{0,6}$')
    gas_price: str = Field(pattern=r'^[1-9][0-9]{0,12}$')
    max_gas_bnb: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def coherent_action(self):
        if (self.action == 'CONFIGURE' and (self.operator is None or self.limits is None)
                or self.action != 'CONFIGURE' and (self.operator is not None or self.limits is not None)):
            raise ValueError('Sólo CONFIGURE admite operador y límites nuevos')
        if self.action in {'APPROVE', 'REVOKE'}:
            if (self.token is None or self.allowance_units is None or self.action == 'APPROVE' and self.allowance_units == '0'
                    or self.action == 'REVOKE' and self.allowance_units != '0'):
                raise ValueError('Indica token e importe: positivo para autorizar, cero para revocar')
        elif self.token is not None or self.allowance_units is not None:
            raise ValueError('Esta acción no modifica permisos ERC-20')
        return self


class Claim(Body):
    address: str = Field(min_length=42, max_length=42)
    operator: str | None = Field(default=None, min_length=42, max_length=42)
    plan_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmation: Literal['CONFIGURAR_EJECUTOR_DEX', 'DETENER_EJECUTOR_DEX', 'AUTORIZAR_TOKENS_DEX', 'REVOCAR_TOKENS_DEX']


class Recovery(Body):
    address: str = Field(min_length=42, max_length=42)


def create_dex_control_router(access, *, authorized, authorized_passive, rpc_factory=None) -> APIRouter:
    router = APIRouter(prefix='/accounts/{account_id}/dex-controls', tags=['dex-controls'])
    factory = rpc_factory or BnbReadClient
    clock = access._dex_bots.now

    def verified(token, account, address=None):
        access.require(token)
        if address is not None:
            wallet = canonical_wallet_address(address)
            digest = hmac.new(access._wallet_proof_key, wallet.encode('ascii'), hashlib.sha256).hexdigest()
            if (str(account), '0x38', digest) not in access._wallet_verified:
                raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión REAL')

    def failure(exc):
        return HTTPException(status_code=409, detail=str(exc), headers={'Cache-Control': 'no-store'})

    def local(response, token, action, *, passive=False):
        response.headers['Cache-Control'] = 'no-store'
        try:
            with access._lock:
                access.require(token, touch=not passive)
                with access.vault.session() as db:return action(db, clock())
        except (PortfolioError, ValueError) as exc:raise failure(exc) from None

    def chain(response, token, account, address, operator, resolve, action, *, finalized=False):
        response.headers['Cache-Control'] = 'no-store'
        try:
            with access._lock:
                verified(token, account, address)
                wallet = canonical_wallet_address(address)
                generation = access.vault.session_generation
                with access.vault.session() as db:
                    deployment_id, replay = resolve(db)
                    parent, terms, state = deployment_record(db, deployment_id)
                    if (parent.account_id != str(account) or parent.wallet_digest != deployment_identity(db, 'wallet', wallet)
                            or not (parent.status == 'FINALIZED' or state['code_observation'] is not None)):
                        raise DexExecutorError('Despliegue no verificado para esta wallet y cuenta')
                    creation_nonce, fee = int(terms['nonce']), terms['fee']
                if replay:
                    with access.vault.session() as db:return action(db, None, clock())
            # No REAL lock, transaction, keys or signing permission during I/O.
            with factory() as rpc:
                snapshot = read_executor_state(rpc, wallet, creation_nonce, fee,
                    candidate_operator=operator, now=clock(), block_tag='finalized' if finalized else 'latest')
            with access._lock:
                verified(token, account, address)
                if access.vault.session_generation != generation:raise DexExecutorError('La sesión REAL cambió durante la lectura')
                with access.vault.session() as db:return action(db, snapshot, clock())
        except (PortfolioError, ValueError) as exc:raise failure(exc) from None

    @router.get('', dependencies=[Depends(authorized_passive)])
    def history(account_id: UUID, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return local(response, token, lambda db, now: controls.list_controls(db, str(account_id), now=now), passive=True)

    @router.post('/state', dependencies=[Depends(authorized)])
    def current(account_id: UUID, body: StateRequest, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        def view(db, snapshot, now):
            controls._context(db, str(body.deployment_id), str(account_id), canonical_wallet_address(body.address), snapshot, now)
            caps, reasons = {}, {}
            for token_name in ('USDT', 'WBNB'):
                try:caps[token_name] = str(allowance_cap(snapshot, token_name, now=now))
                except DexExecutorError as exc:
                    caps[token_name] = None; reasons[token_name] = str(exc)
            return {'source': 'PUBLIC_RPC_OBSERVED', 'block_number': str(snapshot.block_number), 'block_at': snapshot.block_at.isoformat(),
                # Ephemeral after EOA proof; never written to the local journal.
                'executor_address': snapshot.executor_address, 'pool': snapshot.pool,
                'policy_id': str(snapshot.policy_id), 'limits': snapshot.policy_limits.as_dict() if snapshot.policy_limits else None,
                'operator_stopped': int(snapshot.operator, 16) == 0, 'position_covered': snapshot.position_covered,
                'owned_wbnb': str(snapshot.owned_wbnb), 'cost_usdt': str(snapshot.cost_usdt),
                'allowance_usdt': str(snapshot.allowance_usdt), 'allowance_wbnb': str(snapshot.allowance_wbnb),
                'allowance_caps': caps, 'allowance_reasons': reasons,
                'operator_code_empty': snapshot.current_operator_code_empty,
                'native_balance_wei': str(snapshot.native_balance), 'automatic_execution': False}
        return chain(response, token, account_id, body.address, body.operator, lambda db: (str(body.deployment_id), False), view)

    @router.post('', dependencies=[Depends(authorized)])
    def prepare(account_id: UUID, body: Preparation, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return chain(response, token, account_id, body.address, body.operator, lambda db: (str(body.deployment_id), False),
            lambda db, snapshot, now: controls.prepare_control(db, identifier=str(body.request_id), account_id=str(account_id),
                deployment_id=str(body.deployment_id), wallet_address=body.address, action=body.action, operator=body.operator,
                limits=body.limits.model_dump() if body.limits else None, snapshot=snapshot, gas=body.gas, gas_price=body.gas_price,
                max_gas_bnb=body.max_gas_bnb, now=now, token=body.token, allowance_units=body.allowance_units))

    @router.post('/{identifier}/claim', dependencies=[Depends(authorized)])
    def claim(account_id: UUID, identifier: UUID, body: Claim, response: Response,
              token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        def resolve(db):
            row, terms, _ = controls.control_record(db, str(identifier))
            expected = {'CONFIGURE': 'CONFIGURAR_EJECUTOR_DEX', 'STOP': 'DETENER_EJECUTOR_DEX',
                'APPROVE': 'AUTORIZAR_TOKENS_DEX', 'REVOKE': 'REVOCAR_TOKENS_DEX'}[terms['action']]
            if row.account_id != str(account_id) or body.confirmation != expected:
                raise DexExecutorError('Cuenta o confirmación de control distinta')
            return row.deployment_id, row.status != 'PREPARED'
        return chain(response, token, account_id, body.address, body.operator, resolve,
            lambda db, snapshot, now: controls.claim_control(db, str(identifier), account_id=str(account_id), wallet_address=body.address,
                operator=body.operator, plan_digest=body.plan_digest, snapshot=snapshot, now=now))

    @router.post('/{identifier}/hash', dependencies=[Depends(authorized)])
    def report(account_id: UUID, identifier: UUID, body: Hash, response: Response,
               token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return local(response, token, lambda db, now: controls.report_control(db, str(identifier), account_id=str(account_id), tx_hash=body.tx_hash, now=now))

    @router.post('/{identifier}/recover', dependencies=[Depends(authorized)])
    def recover(account_id: UUID, identifier: UUID, body: Recovery, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        def resolve(db):
            row, _, state = controls.control_record(db, str(identifier))
            if row.account_id != str(account_id):raise DexExecutorError('Cuenta de control distinta')
            return row.deployment_id, state['nonce_observation'] is not None
        return chain(response, token, account_id, body.address, None, resolve,
            lambda db, snapshot, now: controls.recover_control(db, str(identifier), account_id=str(account_id),
                wallet_address=body.address, snapshot=snapshot, now=now), finalized=True)

    @router.post('/{identifier}/rejected', dependencies=[Depends(authorized)])
    def rejected(account_id: UUID, identifier: UUID, body: Rejected, response: Response,
                 token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return local(response, token, lambda db, now: controls.report_control(db, str(identifier), account_id=str(account_id), rejected=True, now=now))

    @router.post('/{identifier}/check', dependencies=[Depends(authorized)])
    def check(account_id: UUID, identifier: UUID, body: Check, response: Response,
              token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return local(response, token, lambda db, now: controls.check_control(db, str(identifier), account_id=str(account_id), evidence=body.evidence, now=now))

    return router
