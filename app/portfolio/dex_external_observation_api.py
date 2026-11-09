"""Private external-call observation; crypto/RPC run outside vault leases."""
from __future__ import annotations

import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_observation import source_reference, verify_external_call
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.portfolio import dex_call_resolution as resolution
from app.portfolio import dex_external_observation as journal
from app.portfolio.dex_deployments import _identity
from app.portfolio.domain import PortfolioError
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_proof import canonical_wallet_address


class ExternalObservationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    address: str = Field(pattern=r'^0x[0-9a-fA-F]{40}$')
    tx_hash: str = Field(pattern=r'^0x[0-9a-fA-F]{64}$')


class SaveExternalObservationRequest(ExternalObservationRequest):
    preview_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_state_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmation: Literal['GUARDAR_OBSERVACION_EXTERNA']


def create_external_observation_router(access, *, authorized_passive, rpc_factory=None):
    router = APIRouter(prefix='/accounts/{account_id}/dex-call-resolutions/{kind}/{identifier}',
                       tags=['dex-external-observation'])
    factory = rpc_factory or BnbOperatorReadClient

    def verified(token, account, wallet, *, touch=False, generation=None):
        access.require(token, touch=touch)
        if generation is not None and access.vault.session_generation != generation:
            raise DexExecutorError('La sesión cambió durante la observación externa')
        digest = hmac.new(access._wallet_proof_key, wallet.encode('ascii'), hashlib.sha256).hexdigest()
        if (str(account), '0x38', digest) not in access._wallet_verified:
            raise DexExecutorError('Verifica el control de esta wallet en BNB Chain')

    def capture(db, kind, identifier, account, wallet):
        row, _, _, source = resolution._context(db, kind, identifier, str(account), wallet)
        if (row.status not in resolution.PENDING[kind]
                or resolution.resolution_record(db, resolution.identifier(kind, identifier)) is not None):
            raise DexExecutorError('Esta llamada ya no necesita observar una ejecución externa')
        return row, source

    def idle(db, kind, row):
        if kind == 'OPERATOR':
            access._dex_bots.require_idle()
            resolution._quiescence(db, row, _time(access._dex_bots.now()), verify_runs=True)

    def prior(db, kind, source_id, body):
        record = journal.observation_record(db, journal.identifier(kind, source_id))
        if record is None:
            return None
        if (record[0].tx_digest != _identity(db, 'tx', body.tx_hash)
                or record[1]['preview_digest'] != body.preview_digest
                or record[1]['source_state_digest'] != body.source_state_digest):
            raise DexExecutorError('La llamada ya tiene otra observación externa guardada')
        return journal.public_record(record)

    def run(account_id, kind, identifier, body, response, token, passive, *, save):
        response.headers['Cache-Control'] = 'no-store'
        wallet = canonical_wallet_address(body.address); source_id = str(identifier)
        try:
            with access._lock:
                verified(token, account_id, wallet, touch=passive is None)
                generation = access.vault.session_generation
                with access.vault.session() as db:
                    row, source = capture(db, kind, source_id, account_id, wallet)
                    if save:
                        existing = prior(db, kind, source_id, body)
                        if existing is not None:
                            return existing
                        idle(db, kind, row)
                    else:
                        journal.require_unobserved(db, kind, source_id)
                    pin = (row.payload_digest, row.state_digest, row.status)
                    reference = source_reference(kind, _existing_salt(db))
            try:
                with factory() as rpc:
                    evidence = collect_call_resolution(rpc, source, body.tx_hash)
            except Exception:
                # Provider exceptions can contain an authenticated endpoint.
                # Revocation wins over reporting any transport failure.
                with access._lock:
                    verified(token, account_id, wallet, generation=generation)
                raise HTTPException(503, 'No se pudo consultar el recibo externo; conserva el pendiente',
                                    headers={'Cache-Control': 'no-store'}) from None
            if evidence is None:
                raise DexExecutorError('Todavía no hay un recibo externo finalizado')
            observed_at = _time(access._dex_bots.now())
            proof = verify_external_call(source, evidence, reference=reference, now=observed_at)
            with access._lock:
                verified(token, account_id, wallet, generation=generation)
                with access.vault.session() as db:
                    row, _ = capture(db, kind, source_id, account_id, wallet)
                    if (row.payload_digest, row.state_digest, row.status) != pin:
                        raise DexExecutorError('La llamada cambió durante la observación; vuelve a revisar')
                    if save:
                        existing = prior(db, kind, source_id, body)
                        if existing is not None:
                            return existing
                        idle(db, kind, row)
                        return journal.save_observation(db, kind, source_id, account_id=str(account_id),
                            wallet_address=wallet, signer_address=evidence['transaction']['from'], tx_hash=body.tx_hash,
                            proof=proof, preview_digest=body.preview_digest, source_state_digest=body.source_state_digest,
                            now=_time(access._dex_bots.now()))
                    journal.require_unobserved(db, kind, source_id)
                    return {**journal.preview(row, kind, proof), 'observed_at': observed_at.isoformat()}
        except (PortfolioError, ValueError) as exc:
            with access._lock:
                access.require(token, touch=False)
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from None

    @router.post('/external-observation', dependencies=[Depends(authorized_passive)])
    def observe(account_id: UUID, kind: Literal['WALLET_SWAP', 'OPERATOR'], identifier: UUID,
                body: ExternalObservationRequest, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token'),
                passive: Literal['1'] | None = Header(default=None, alias='X-Orbis-Passive')):
        return run(account_id, kind, identifier, body, response, token, passive, save=False)

    @router.post('/external-observation/save', dependencies=[Depends(authorized_passive)])
    def save(account_id: UUID, kind: Literal['WALLET_SWAP', 'OPERATOR'], identifier: UUID,
             body: SaveExternalObservationRequest, response: Response,
             token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(account_id, kind, identifier, body, response, token, None, save=True)

    @router.get('/external-observation/saved', dependencies=[Depends(authorized_passive)])
    def saved(account_id: UUID, kind: Literal['WALLET_SWAP', 'OPERATOR'], identifier: UUID,
              response: Response, token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'
        try:
            with access._lock:
                access.require(token, touch=False)
                with access.vault.session() as db:
                    row, _, _, _ = resolution.source_record(db, kind, str(identifier))
                    if row.account_id != str(account_id):
                        raise DexExecutorError('Llamada de otra cuenta')
                    return journal.status(db, kind, str(identifier)) or {
                        'id': journal.identifier(kind, str(identifier)), 'saved': False, 'resolves_intent': False}
        except (PortfolioError, ValueError) as exc:
            with access._lock:
                access.require(token, touch=False)
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from None
    return router
