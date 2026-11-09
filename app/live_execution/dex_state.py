"""Fresh, read-only BNB executor observations; no signing or transaction sends.

Contract/token reads share one numbered block, whose hash is checked again.
Pending EOA nonce is separate metadata. These are public RPC observations,
not independent consensus proofs. Addresses remain transient, with no logs.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import time
from typing import Callable, Literal

from eth_abi import encode, decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak
import httpx

from app.live_execution.dex_artifact import DexExecutorError, _owner, _fee, _integer, executor_address, expected_runtime
from app.live_execution.dex_policy import PolicyLimits, POLICY_ABI
from app.portfolio.dex_deployment_receipt import quantity, word
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_swap import TOKENS


ZERO = '0x'+'00'*20
Rpc = Callable[[str, list], object]


class BnbReadClient:
    """One bounded public read session; endpoint comes only from local config."""
    METHODS = frozenset({'eth_chainId', 'eth_getBlockByNumber', 'eth_getCode', 'eth_call',
                         'eth_getTransactionCount', 'eth_getBalance'})
    MAX_QUERIES = 32

    def __init__(self, *, transport=None, monotonic=time.monotonic):
        self._url = os.environ.get('ORBIS_BSC_RPC_URL', 'https://bsc-rpc.publicnode.com')
        self._client = httpx.Client(timeout=4, trust_env=False, follow_redirects=False, transport=transport)
        self._clock = monotonic
        self._deadline = monotonic()+20
        self._sequence = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._client.close()

    def __call__(self, method: str, params: list) -> object:
        if method not in self.METHODS:
            raise DexExecutorError('El cliente BNB sólo admite lecturas públicas')
        self._sequence += 1
        remaining = self._deadline-self._clock()
        if remaining <= 0 or self._sequence > self.MAX_QUERIES:
            raise DexExecutorError('La lectura BNB superó el tiempo o número de consultas permitido')
        full_block = method == 'eth_getBlockByNumber' and len(params) == 2 and params[1] is True
        byte_limit = 4_000_000 if full_block else 500_000
        try:
            with self._client.stream('POST', self._url, json={'jsonrpc': '2.0', 'id': self._sequence,
                    'method': method, 'params': params}, timeout=min(4, remaining)) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > byte_limit or self._clock() > self._deadline:
                        raise ValueError('RPC response limit')
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get('jsonrpc') != '2.0'
                    or type(value.get('id')) is not int or value['id'] != self._sequence
                    or 'error' in value or 'result' not in value):
                raise ValueError('RPC envelope')
            return value['result']
        except (httpx.HTTPError, ValueError, TypeError):
            raise DexExecutorError('BNB no devolvió una lectura pública verificable') from None


@dataclass(frozen=True, slots=True)
class ExecutorState:
    owner: str = field(repr=False)
    executor_address: str = field(repr=False)
    pool: str = field(repr=False)
    operator: str = field(repr=False)
    candidate_operator: str | None = field(repr=False)
    fee: int
    finalized: bool
    block_number: int
    block_hash: str
    block_at: datetime
    observed_at: datetime
    owner_nonce: int
    pending_nonce: int
    native_balance: int
    balance_usdt: int
    balance_wbnb: int
    allowance_usdt: int
    allowance_wbnb: int
    policy_id: int
    policy_limits: PolicyLimits | None
    next_nonce: int
    buy_spent: int
    entries: int
    owned_wbnb: int
    cost_usdt: int
    realized_loss: int
    last_entry_at: int
    acquired_wbnb: int
    current_operator_code_empty: bool | None

    @property
    def position_covered(self) -> bool:
        return self.balance_wbnb >= self.owned_wbnb

    def require_fresh(self, now: datetime) -> None:
        now = _time(now)
        if (not -15 <= (now-self.block_at).total_seconds() <= 30
                or not 0 <= (now-self.observed_at).total_seconds() <= 30):
            raise DexExecutorError('La observación BNB ya no es actual; vuelve a revisar')


def read_executor_state(rpc: Rpc, wallet: str, creation_nonce: int, fee: int, *,
                        candidate_operator: str | None = None, now: datetime, block_tag: Literal['latest', 'finalized'] = 'latest') -> ExecutorState:
    """Check exact runtime/owner and return current state at a coherent block."""
    now = _time(now)
    if block_tag not in {'latest', 'finalized'}:raise DexExecutorError('Etiqueta de bloque no admitida')
    owner, fee = _owner(wallet), _fee(fee)
    executor = executor_address(owner, creation_nonce)
    candidate = _owner(candidate_operator) if candidate_operator is not None else None
    if candidate in {owner, executor}:
        raise DexExecutorError('El operador debe ser una cuenta separada')
    try:
        if quantity(rpc('eth_chainId', [])) != 56:
            raise ValueError('chain')
        block = rpc('eth_getBlockByNumber', [block_tag, False])
        height, block_hash = quantity(block['number']), word(block['hash'])
        at = datetime.fromtimestamp(quantity(block['timestamp']), timezone.utc)
        if height <= 0 or not -15 <= (now-at).total_seconds() <= 30:
            raise ValueError('stale block')
        tag = hex(height)

        def call(address, signature, inputs=(), values=(), outputs=('uint256',)):
            data = '0x'+(keccak(text=signature)[:4]+encode(inputs, values)).hex()
            raw = rpc('eth_call', [{'to': address, 'data': data}, tag])
            if not isinstance(raw, str) or not raw.startswith('0x') or len(raw) > 100_000:
                raise ValueError('call response')
            return decode(outputs, bytes.fromhex(raw[2:]), strict=True)

        pool = canonical_wallet_address(call(executor, 'pool()', outputs=('address',))[0])
        runtime = rpc('eth_getCode', [executor, tag])
        if not isinstance(runtime, str) or runtime.lower() != expected_runtime(owner, pool, fee):
            raise ValueError('program/owner/market')
        if candidate is not None and rpc('eth_getCode', [candidate, tag]) != '0x':
            raise ValueError('operator is not a code-free EOA')
        raw_policy = call(executor, 'policy()', outputs=(POLICY_ABI,))[0]
        operator = canonical_wallet_address(raw_policy[0])
        current_operator_code_empty = rpc('eth_getCode', [operator, tag]) == '0x' if operator != ZERO else None
        if all(value == 0 for value in raw_policy[1:]):
            if operator != ZERO:
                raise ValueError('empty policy operator')
            limits = None
        else:
            limits = PolicyLimits(*raw_policy[1:])
            if operator in {owner, executor}:
                raise ValueError('policy operator')
        counters = {}
        for name, signature in (('policy_id', 'policyId()'), ('next_nonce', 'nextNonce()'), ('buy_spent', 'buySpent()'),
                ('entries', 'entries()'), ('owned_wbnb', 'ownedWbnb()'), ('cost_usdt', 'costUsdt()'),
                ('realized_loss', 'realizedLoss()'), ('last_entry_at', 'lastEntryAt()'), ('acquired_wbnb', 'acquiredWbnb()')):
            counters[name] = call(executor, signature)[0]
        if ((counters['owned_wbnb'] == 0) != (counters['cost_usdt'] == 0)
                or counters['acquired_wbnb'] < counters['owned_wbnb']
                or limits is None and any(counters[key] for key in counters if key != 'policy_id')
                or limits is not None and counters['policy_id'] == 0):
            raise ValueError('executor counters')
        values = {}
        for token, address in TOKENS.items():
            if call(address, 'decimals()', outputs=('uint8',))[0] != 18:
                raise ValueError('token decimals')
            values['balance_'+token.lower()] = call(address, 'balanceOf(address)', ('address',), (owner,))[0]
            values['allowance_'+token.lower()] = call(address, 'allowance(address,address)', ('address', 'address'), (owner, executor))[0]
        owner_nonce = quantity(rpc('eth_getTransactionCount', [owner, tag]))
        pending_nonce = quantity(rpc('eth_getTransactionCount', [owner, 'pending']))
        native_balance = quantity(rpc('eth_getBalance', [owner, tag]))
        if not creation_nonce < owner_nonce <= pending_nonce <= 2**64-1:
            raise ValueError('owner nonce')
        repeated = rpc('eth_getBlockByNumber', [tag, False])
        if quantity(repeated['number']) != height or word(repeated['hash']) != block_hash or repeated['timestamp'] != block['timestamp']:
            raise ValueError('block changed during observation')
        return ExecutorState(owner, executor, pool, operator, candidate, fee, block_tag == 'finalized', height, block_hash, at, now,
            owner_nonce, pending_nonce, native_balance, **values, policy_limits=limits,
            current_operator_code_empty=current_operator_code_empty, **counters)
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, DecodingError, httpx.HTTPError):
        raise DexExecutorError('El estado actual del ejecutor BNB no es verificable; revisión necesaria') from None
