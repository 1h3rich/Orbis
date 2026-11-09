"""Finite ERC-20 authority to the pinned executor, never to its operator/router."""
from datetime import datetime
from eth_abi import encode
from eth_utils import keccak

from app.live_execution.dex_artifact import DexExecutorError, _integer, executor_address
from app.live_execution.dex_policy import _transaction
from app.live_execution.dex_state import ExecutorState, ZERO
from app.portfolio.wallet_swap import TOKENS


def cap_from_limits(limits, counters: dict, token: str) -> int:
    if token not in TOKENS:raise DexExecutorError('Token no admitido por el ejecutor')
    spent, owned, acquired = (counters[key] for key in ('buy_spent', 'owned_wbnb', 'acquired_wbnb'))
    for value in (spent, owned, acquired):_integer(value, 'Contador del ejecutor', 2**256-1, zero=True)
    if spent > limits.buy_budget or not owned <= acquired <= limits.max_wbnb_acquired:
        raise DexExecutorError('Los contadores no coinciden con los límites del ejecutor')
    if token == 'USDT':
        if not limits.allow_buy:raise DexExecutorError('Esta política no admite compras')
        return limits.buy_budget-spent
    if not limits.allow_sell:raise DexExecutorError('Esta política no admite ventas')
    # Future bought units need sale permission too. Sales never replenish quota.
    return owned+(limits.max_wbnb_acquired-acquired if limits.allow_buy else 0)


def allowance_cap(snapshot: ExecutorState, token: str, *, now: datetime) -> int:
    if type(snapshot) is not ExecutorState:raise DexExecutorError('Estado del ejecutor no verificado')
    snapshot.require_fresh(now)
    if (snapshot.operator == ZERO or snapshot.policy_limits is None or not snapshot.current_operator_code_empty
            or not snapshot.position_covered):raise DexExecutorError('Revisa la política, operador o posición antes de autorizar tokens')
    snapshot.policy_limits.validate_at(int(now.timestamp()), snapshot.owned_wbnb)
    return cap_from_limits(snapshot.policy_limits,
        {key: getattr(snapshot, key) for key in ('buy_spent', 'owned_wbnb', 'acquired_wbnb')}, token)


def approval_transaction(wallet: str, creation_nonce: int, token: str, units: int, *, cap: int,
                         nonce: int, gas: int, gas_price: int) -> dict:
    if token not in TOKENS:raise DexExecutorError('Token no admitido por el ejecutor')
    maximum = 10000*10**18 if token == 'USDT' else 2**128-1
    _integer(cap, 'Cuota de permiso', maximum, zero=True)
    _integer(units, 'Permiso ERC-20', cap, zero=True)
    spender = executor_address(wallet, creation_nonce)
    data = keccak(text='approve(address,uint256)')[:4]+encode(['address', 'uint256'], [spender, units])
    return {**_transaction(wallet, creation_nonce, data, nonce=nonce, gas=gas, gas_price=gas_price), 'to': TOKENS[token]}
