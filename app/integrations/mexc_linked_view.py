"""MEXC View transport bound in memory to the account already recorded in the vault."""

from dataclasses import dataclass, field
from hmac import compare_digest
from typing import Callable

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError


_READ_METHODS = frozenset((
    "balances", "bsc_withdrawal_route", "ethereum_erc20_deposit_destination",
    "ethereum_token_contracts", "ethereum_token_contracts_by_direction",
    "evm_token_contracts_by_direction", "evm_token_deposit_destination",
    "open_orders", "open_orders_unfiltered", "order_by_client_id", "trades",
    "trades_for_order", "trades_window", "transfers_recent", "verify_permissions",
))


@dataclass(frozen=True, slots=True, repr=False)
class MexcLinkedView:
    _client: MexcReadOnlyClient = field(repr=False)
    account_id: str
    _archive_ref: str
    _credential_ref: str
    _uid: str = field(repr=False)

    def account_reference(self) -> str:
        if self._client.account_reference() != self._credential_ref:
            raise MexcReadOnlyError("La referencia de la clave View renovada cambió")
        return self._archive_ref

    def account_uid(self) -> str:
        self.account_reference()
        uid = self._client.account_uid()
        if not isinstance(uid, str) or not compare_digest(uid, self._uid):
            raise MexcReadOnlyError("El UID de la clave View renovada cambió")
        self.account_reference()
        return uid

    def __getattr__(self, name: str) -> Callable[..., dict | tuple]:
        if name not in _READ_METHODS:
            raise AttributeError(name)
        read = getattr(self._client, name)

        def verified_read(*args, **kwargs):
            self.account_uid()
            result = read(*args, **kwargs)
            self.account_uid()
            return result

        return verified_read
