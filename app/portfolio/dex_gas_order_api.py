"""EOA-scoped gas-order observation; no ledger changes or execution authority.

The shared reader retains the trade route's lock, identity, privacy and lost
response semantics. This profile accepts the nine authenticated gas families
and records their actual native fees, separately from transferred principal.
"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response

from app.portfolio import dex_gas_order as orders
from app.portfolio.dex_gas_order_source import gas_source, gas_reference
from app.portfolio.dex_receipt_order_api import Wallet, OrderProofPorts, _order_handlers


GasKind = Literal['WALLET_SWAP', 'OPERATOR', 'CALL_RESOLUTION', 'DEPLOYMENT',
    'CONTROL', 'GAS_FUNDING', 'GAS_RETURN', 'DEPLOYMENT_RESOLUTION', 'NATIVE_RESOLUTION']


def create_gas_order_router(access, *, authorized_passive, rpc_factory=None):
    router = APIRouter(prefix='/accounts/{account_id}/dex-receipts/{kind}/{identifier}', tags=['dex-gas-order'])
    check, lookup = _order_handlers(access, ports=OrderProofPorts(gas_source, gas_reference,
        orders.gas_order_record, orders.identifier, orders.public_record,
        orders.preview_gas_order, orders.confirm_gas_order), rpc_factory=rpc_factory)

    @router.post('/gas-order-proof', dependencies=[Depends(authorized_passive)])
    def prove_gas_order(account_id: UUID, kind: GasKind, identifier: UUID, body: Wallet, response: Response,
                        token: str | None = Header(default=None, alias='X-Orbis-Vault-Token'),
                        passive: str | None = Header(default=None, alias='X-Orbis-Passive')):
        return check(account_id, kind, identifier, body, response, token, passive=passive=='1')

    @router.get('/gas-order-proof', dependencies=[Depends(authorized_passive)])
    def gas_order_status(account_id: UUID, kind: GasKind, identifier: UUID, response: Response,
                         wallet: str = Header(alias='X-Orbis-Wallet', pattern=r'^0x[0-9a-fA-F]{40}$'),
                         token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')):
        return lookup(account_id, kind, identifier, response, wallet, token)
    return router
