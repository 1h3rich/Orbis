"""Private external chronology capture; network/signature recovery outside leases."""
from __future__ import annotations

import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import Field

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_observation import source_reference
from app.live_execution.dex_external_order import common_reference, verify_external_order
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.portfolio import dex_call_resolution as original, dex_external_observation as observation, dex_external_order as orders
from app.portfolio.dex_deployments import _identity
from app.portfolio.dex_external_observation_api import ExternalObservationRequest
from app.portfolio.domain import PortfolioError
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_proof import canonical_wallet_address

Kind = Literal['WALLET_SWAP', 'OPERATOR']


class SaveExternalOrderRequest(ExternalObservationRequest):
    preview_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmation: Literal['GUARDAR_ORDEN_EXTERNA']


def create_external_order_router(access, *, authorized_passive, rpc_factory=None):
    router = APIRouter(prefix='/accounts/{account_id}/dex-call-resolutions/{kind}/{identifier}/external-order',
                       tags=['dex-external-order'])
    factory = rpc_factory or BnbOperatorReadClient

    def verified(token, account, wallet, *, generation=None, touch=False):
        access.require(token, touch=touch)
        if generation is not None and access.vault.session_generation != generation:
            raise DexExecutorError('La sesión cambió durante la lectura del orden externo')
        digest = hmac.new(access._wallet_proof_key, wallet.encode('ascii'), hashlib.sha256).hexdigest()
        if (str(account), '0x38', digest) not in access._wallet_verified:
            raise DexExecutorError('Verifica el control de esta wallet en BNB Chain')

    def capture(db, account, kind, source_id, wallet, tx_hash):
        _, _, _, source = original._context(db, kind, source_id, str(account), wallet)
        record = observation.observation_record(db, observation.identifier(kind, source_id))
        if record is None:
            raise DexExecutorError('Guarda primero la observación externa verificada')
        row, body = record
        if row.tx_digest != _identity(db, 'tx', tx_hash):
            raise DexExecutorError('El hash no corresponde a la observación externa guardada')
        return row, body, source

    def prior(db, observation_id, body):
        record = orders.order_record(db, orders.identifier(observation_id))
        if record is None:
            return None
        result = orders.public_record(record)
        if result['preview_digest'] != body.preview_digest:
            raise DexExecutorError('La respuesta guardada pertenece a otra revisión de orden')
        return result

    def idle(kind):
        if kind == 'OPERATOR': access._dex_bots.require_idle()

    def run(account, kind, identifier, body, response, token, passive, *, save):
        response.headers['Cache-Control'] = 'no-store'
        wallet = canonical_wallet_address(body.address); source_id = str(identifier)
        try:
            with access._lock:
                verified(token, account, wallet, touch=passive is None)
                generation = access.vault.session_generation
                with access.vault.session() as db:
                    row, stored, source = capture(db, account, kind, source_id, wallet, body.tx_hash)
                    if save:
                        existing = prior(db, row.id, body)
                        if existing is not None: return existing
                        idle(kind)
                    pin = (row.id, row.payload_digest)
                    salt = _existing_salt(db); reference = source_reference(kind, salt); common = common_reference(salt)
            try:
                with factory() as rpc: evidence = collect_call_resolution(rpc, source, body.tx_hash)
            except Exception:
                with access._lock: verified(token, account, wallet, generation=generation)
                raise HTTPException(503, 'No se pudo consultar el orden externo; conserva la observación',
                                    headers={'Cache-Control': 'no-store'}) from None
            if evidence is None: raise DexExecutorError('Todavía no hay un recibo externo finalizado')
            proof = verify_external_order(source, pin[0], pin[1], stored['proof'], evidence,
                reference=reference, common=common, now=_time(access._dex_bots.now()))
            with access._lock:
                verified(token, account, wallet, generation=generation)
                with access.vault.session() as db:
                    row, _, _ = capture(db, account, kind, source_id, wallet, body.tx_hash)
                    if (row.id, row.payload_digest) != pin:
                        raise DexExecutorError('La observación cambió durante la consulta; revisa de nuevo')
                    if save:
                        existing = prior(db, row.id, body)
                        if existing is not None: return existing
                    else:
                        return orders.preview(row, proof)
                # No capture/read transaction is upgraded in place. The writer
                # reopens a short session and reauthenticates under BEGIN IMMEDIATE.
                idle(kind)
                with access.vault.session() as db:
                    return orders.save_order(db, pin[0], account_id=str(account), proof=proof,
                        preview_digest=body.preview_digest, now=_time(access._dex_bots.now()))
        except (PortfolioError, ValueError) as exc:
            with access._lock: access.require(token, touch=False)
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from None

    @router.post('/preview', dependencies=[Depends(authorized_passive)])
    def preview(account_id: UUID, kind: Kind, identifier: UUID, body: ExternalObservationRequest, response: Response,
                token: str | None = Header(default=None, alias='X-Orbis-Vault-Token'),
                passive: Literal['1'] | None = Header(default=None, alias='X-Orbis-Passive')):
        return run(account_id, kind, identifier, body, response, token, passive, save=False)

    @router.post('/save', dependencies=[Depends(authorized_passive)])
    def save(account_id: UUID, kind: Kind, identifier: UUID, body: SaveExternalOrderRequest, response: Response,
             token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return run(account_id, kind, identifier, body, response, token, None, save=True)

    @router.get('', dependencies=[Depends(authorized_passive)])
    def saved(account_id: UUID, kind: Kind, identifier: UUID, response: Response,
              token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control'] = 'no-store'
        try:
            with access._lock:
                access.require(token, touch=False)
                with access.vault.session() as db:
                    parent, _, _, _ = original.source_record(db, kind, str(identifier))
                    if parent.account_id != str(account_id): raise DexExecutorError('Llamada de otra cuenta')
                    record_id = orders.identifier(observation.identifier(kind, str(identifier)))
                    record = orders.order_record(db, record_id)
                    return orders.public_record(record) if record is not None else {
                        'id': record_id, 'saved': False, 'applied_to_ledger': False,
                        'resolves_intent': False, 'trading_authority': False}
        except (PortfolioError, ValueError) as exc:
            with access._lock: access.require(token, touch=False)
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from None
    return router
