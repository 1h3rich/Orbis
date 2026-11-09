"""Private read-only owner recovery. No wallet sender, key or client RPC data."""
import hashlib
import hmac
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import Field

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.live_execution.dex_owner_observation import collect_owner_receipt, collect_owner_code
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_deployments as deployments, dex_controls as controls
from app.portfolio.dex_deployment_api import Body
from app.portfolio.dex_deployment_receipt import observe_receipt
from app.portfolio.dex_control_receipt import observe_control
from app.portfolio.domain import PortfolioError
from app.portfolio.wallet_proof import canonical_wallet_address


class Observation(Body):
    address: str = Field(min_length=42, max_length=42)
    tx_hash: str | None = Field(default=None, pattern=r'^0x[0-9a-fA-F]{64}$')


def create_dex_owner_observation_router(access, *, authorized, rpc_factory=None) -> APIRouter:
    router = APIRouter(prefix='/accounts/{account_id}', tags=['dex-owner-observation'])
    factory = rpc_factory or BnbOperatorReadClient
    clock = access._dex_bots.now

    def verified(token, account, wallet):
        access.require(token)
        digest = hmac.new(access._wallet_proof_key, wallet.encode('ascii'), hashlib.sha256).hexdigest()
        if (str(account), '0x38', digest) not in access._wallet_verified:
            raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión REAL')

    def observe(kind, account, identifier, body, response, token):
        response.headers['Cache-Control'] = 'no-store'
        journal = deployments if kind == 'deployment' else controls
        record = journal.deployment_record if kind == 'deployment' else journal.control_record
        try:
            wallet = canonical_wallet_address(body.address)
            def context(db, now):
                row, terms, state = record(db, str(identifier))
                if row.account_id != str(account) or journal._identity(db, 'wallet', wallet) != row.wallet_digest:
                    raise DexExecutorError('La operación pertenece a otra wallet o cuenta')
                if body.tx_hash and state['tx_ref'] is not None and journal._identity(db, 'tx', body.tx_hash) != state['tx_ref']:
                    raise DexExecutorError('Conserva el hash original de esta operación')
                public = journal._public(row, terms, state, now)
                return row, terms, state, public

            with access._lock:
                verified(token, account, wallet)
                generation = access.vault.session_generation
                with access.vault.session() as db:
                    row, terms, state, public = context(db, clock())
                    if row.status in {'FINALIZED', 'REVERTED'}:return public
                    if row.status not in {'WALLET_REQUESTED', 'HASH_REPORTED', 'CODE_OBSERVED', 'NONCE_OBSERVED'}:
                        raise DexExecutorError('Esta operación no tiene una solicitud de wallet pendiente')
                    if body.tx_hash is None and (state.get('code_observation') or state.get('nonce_observation')):return public
                    if kind == 'deployment':nonce, fee = int(terms['nonce']), terms['fee']
                    else:
                        parent, deployment, _ = deployments.deployment_record(db, row.deployment_id)
                        if parent.account_id != str(account) or deployments._identity(db, 'wallet', wallet) != parent.wallet_digest:
                            raise DexExecutorError('Despliegue de otra wallet o cuenta')
                        nonce, fee = int(deployment['nonce']), deployment['fee']
            # No REAL lock or database session survives these bounded public reads.
            with factory() as rpc:
                if body.tx_hash:
                    evidence = collect_owner_receipt(rpc, wallet, nonce, fee, body.tx_hash)
                elif kind == 'deployment':evidence = collect_owner_code(rpc, wallet, nonce, fee)
                else:
                    evidence = read_executor_state(rpc, wallet, nonce, fee, now=clock(), block_tag='finalized')
            with access._lock:
                verified(token, account, wallet)
                if access.vault.session_generation != generation:raise DexExecutorError('La sesión REAL cambió durante la lectura')
                with access.vault.session() as db:
                    now = clock()
                    row, terms, state, public = context(db, now)
                    if row.status in {'FINALIZED', 'REVERTED'}:return public
                    if evidence is None:return {**public, 'observation_status': 'PENDING'}
                    if body.tx_hash:
                        # A pasted hash must verify before it can bind an immutable original.
                        verifier = observe_receipt if kind == 'deployment' else observe_control
                        verifier(db, row, terms, {**state, 'tx_ref': journal._identity(db, 'tx', body.tx_hash)}, evidence, now)
                        report = journal.report_deployment if kind == 'deployment' else journal.report_control
                        report(db, str(identifier), account_id=str(account), tx_hash=body.tx_hash, now=now)
                        # Idempotent report paths leave their read transaction open.
                        db.commit()
                        check = journal.check_deployment if kind == 'deployment' else journal.check_control
                        return check(db, str(identifier), account_id=str(account), evidence=evidence, now=now)
                    if kind == 'deployment':
                        return deployments.recover_deployment(db, str(identifier), account_id=str(account), wallet_address=wallet, evidence=evidence, now=now)
                    return controls.recover_control(db, str(identifier), account_id=str(account), wallet_address=wallet, snapshot=evidence, now=now)
        except (PortfolioError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={'Cache-Control': 'no-store'}) from None

    @router.post('/dex-deployments/{identifier}/observe', dependencies=[Depends(authorized)])
    def deployment(account_id: UUID, identifier: UUID, body: Observation, response: Response,
                   token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return observe('deployment', account_id, identifier, body, response, token)

    @router.post('/dex-controls/{identifier}/observe', dependencies=[Depends(authorized)])
    def control(account_id: UUID, identifier: UUID, body: Observation, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return observe('control', account_id, identifier, body, response, token)

    return router
