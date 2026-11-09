"""Read-only whole-executor quotes and exact operator calls on BNB Chain.

No private keys, sends or accounting. A quote simulates executeSwap against one
numbered block, including actual allowances, balances and contract limits.
Its output is a simulation, never a fill. An outbound durable journal is still
required before any worker may broadcast a signature compiled from these plans.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from eth_abi import encode, decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from app.live_execution.dex_artifact import DexExecutorError, _integer, executor_address, _owner, _fee
from app.live_execution.dex_state import BnbReadClient, ExecutorState, ZERO, read_executor_state
from app.live_execution.evm_transaction import quantity, word
from app.portfolio.wallet_funding import _time


SWAP_SIGNATURE = 'executeSwap(uint256,uint256,bool,uint256,uint256,uint256)'
MAX_GAS_BUDGET = 10**17
STOP_GAS_LIMIT = 100000


def _inputs(*, gas: int, gas_price: int, max_gas_wei: int) -> None:
    _integer(gas, 'Gas', 2_000_000)
    _integer(gas_price, 'Precio del gas', 10**12)
    _integer(max_gas_wei, 'Presupuesto de gas', MAX_GAS_BUDGET)
    if gas < 21000 or gas*gas_price > max_gas_wei:
        raise DexExecutorError('El presupuesto de gas no cubre la llamada DEX')


@dataclass(frozen=True, slots=True)
class OperatorPlan:
    snapshot: ExecutorState = field(repr=False)
    creation_nonce: int
    operator_nonce: int
    operator_pending_nonce: int
    operator_balance: int = field(repr=False)
    gas: int
    gas_price: int
    max_gas_wei: int
    observed_at: datetime

    def require_fresh(self, now: datetime) -> None:
        now = _time(now)
        state = self.snapshot
        if type(state) is not ExecutorState:
            raise DexExecutorError('El estado del ejecutor no está verificado')
        state.require_fresh(now)
        if (not 0 <= (now-self.observed_at).total_seconds() <= 15
                or self.observed_at != state.observed_at or state.operator == ZERO
                or state.current_operator_code_empty is not True or state.policy_limits is None
                or state.executor_address != executor_address(state.owner, self.creation_nonce)
                or state.operator in {state.owner, state.executor_address}):
            raise DexExecutorError('La revisión de la cuenta operadora ya no es válida')
        state.policy_limits.validate_at(int(now.timestamp()), state.owned_wbnb)
        _inputs(gas=self.gas, gas_price=self.gas_price, max_gas_wei=self.max_gas_wei)
        _integer(self.operator_nonce, 'Nonce operador', 2**64-1, zero=True)
        _integer(self.operator_pending_nonce, 'Nonce pendiente', 2**64-1, zero=True)
        _integer(self.operator_balance, 'Saldo de gas', 2**256-1, zero=True)
        if (self.operator_pending_nonce != self.operator_nonce or self.gas_price > state.policy_limits.max_gas_price
                or self.gas*self.gas_price > self.operator_balance):
            raise DexExecutorError('Revisa el nonce pendiente, saldo BNB o precio de gas del operador')

    def _transaction(self, data: bytes) -> dict:
        return {'from': self.snapshot.operator, 'to': self.snapshot.executor_address, 'chainId': '0x38',
                'type': '0x0', 'value': '0x0', 'data': '0x'+data.hex(), 'nonce': hex(self.operator_nonce),
                'gas': hex(self.gas), 'gasPrice': hex(self.gas_price)}


@dataclass(frozen=True, slots=True)
class DexTradePlan(OperatorPlan):
    buy: bool
    amount: int
    twap_out: int
    quote_out: int
    slippage_bps: int
    minimum: int
    deadline: int

    def validate(self, now: datetime) -> None:
        self.require_fresh(now)
        _trade_inputs(self.buy, self.amount, self.slippage_bps)
        for name in ('twap_out', 'quote_out', 'minimum'):
            _integer(getattr(self, name), name, 2**256-1)
        _integer(self.deadline, 'Plazo DEX', 2**64-1)
        state, limits = self.snapshot, self.snapshot.policy_limits
        fair_minimum = self.twap_out*(10000-limits.max_deviation_bps)//10000
        if (self.minimum != max(1, fair_minimum, self.quote_out*(10000-self.slippage_bps)//10000)
                or self.minimum > self.quote_out
                or not int(now.timestamp()) < self.deadline <= limits.expires_at
                or not self.observed_at.timestamp() < self.deadline <= int(self.observed_at.timestamp())+120
                or not state.position_covered):
            raise DexExecutorError('Cotización, mínimo, plazo o posición DEX no válidos')
        token = 'usdt' if self.buy else 'wbnb'
        if (getattr(state, 'balance_'+token) < self.amount or getattr(state, 'allowance_'+token) < self.amount
                or self.buy and not limits.allow_buy or not self.buy and not limits.allow_sell):
            raise DexExecutorError('La política, el saldo o el permiso ERC-20 no cubren esta operación')
        if self.buy:
            if (self.amount > limits.max_buy_per_order or state.buy_spent+self.amount > limits.buy_budget
                    or state.entries >= limits.max_entries or state.cost_usdt+self.amount > limits.max_exposure
                    or state.realized_loss >= limits.max_realized_loss
                    or state.acquired_wbnb+self.quote_out > limits.max_wbnb_acquired
                    or state.last_entry_at and int(now.timestamp()) < state.last_entry_at+limits.cooldown_seconds):
                raise DexExecutorError('La compra excede los límites vigentes del ejecutor')
        elif self.amount > state.owned_wbnb or self.twap_out > limits.max_exit_notional:
            raise DexExecutorError('La venta excede la posición propia o el límite de salida')

    def transaction(self) -> dict:
        return self._transaction(_swap_data(self.snapshot.policy_id, self.snapshot.next_nonce, self.buy,
                                           self.amount, self.minimum, self.deadline))


@dataclass(frozen=True, slots=True)
class DexStopPlan(OperatorPlan):
    def validate(self, now: datetime) -> None:
        self.require_fresh(now)
        if self.gas > STOP_GAS_LIMIT:
            raise DexExecutorError('La parada excede la reserva de gas prevista')

    def transaction(self) -> dict:
        return self._transaction(keccak(text='stop()')[:4])


def _trade_inputs(buy: bool, amount: int, slippage_bps: int) -> None:
    if type(buy) is not bool:
        raise DexExecutorError('La dirección del intercambio debe ser booleana')
    _integer(amount, 'Cantidad del intercambio', 2**128-1)
    _integer(slippage_bps, 'Deslizamiento', 500)


def _swap_data(policy: int, nonce: int, buy: bool, amount: int, minimum: int, deadline: int) -> bytes:
    return keccak(text=SWAP_SIGNATURE)[:4]+encode(['uint256','uint256','bool','uint256','uint256','uint256'],
                                               [policy, nonce, buy, amount, minimum, deadline])


def _operator_observation(rpc: Callable, wallet: str, creation_nonce: int, fee: int, *, now: datetime,
                          gas: int, gas_price: int, max_gas_wei: int,snapshot:ExecutorState|None=None) -> tuple[dict, str]:
    state = read_executor_state(rpc, wallet, creation_nonce, fee, now=now) if snapshot is None else snapshot
    if (type(state) is not ExecutorState or state.owner!=_owner(wallet) or state.fee!=_fee(fee)
            or state.executor_address!=executor_address(state.owner,creation_nonce)):
        raise DexExecutorError('La observación corresponde a otra wallet, mercado o ejecutor')
    state.require_fresh(now)
    tag = hex(state.block_number)
    if state.operator == ZERO:
        raise DexExecutorError('El ejecutor no tiene un operador activo')
    values = dict(snapshot=state, creation_nonce=creation_nonce,
        operator_nonce=quantity(rpc('eth_getTransactionCount', [state.operator, tag])),
        operator_pending_nonce=quantity(rpc('eth_getTransactionCount', [state.operator, 'pending'])),
        operator_balance=quantity(rpc('eth_getBalance', [state.operator, tag])),
        gas=gas, gas_price=gas_price, max_gas_wei=max_gas_wei, observed_at=state.observed_at)
    OperatorPlan(**values).require_fresh(now)
    return values, tag


def _same_block(rpc: Callable, state: ExecutorState) -> None:
    repeated = rpc('eth_getBlockByNumber', [hex(state.block_number), False])
    if (quantity(repeated['number']) != state.block_number or word(repeated['hash']) != state.block_hash
            or quantity(repeated['timestamp']) != int(state.block_at.timestamp())):
        raise DexExecutorError('El bloque cambió durante la revisión DEX')


def read_trade_plan(rpc: Callable, wallet: str, creation_nonce: int, fee: int, *, buy: bool, amount: int,
                    slippage_bps: int, gas: int, gas_price: int, max_gas_wei: int, now: datetime,
                    ttl_seconds: int = 60,snapshot:ExecutorState|None=None) -> DexTradePlan:
    now = _time(now)
    _trade_inputs(buy, amount, slippage_bps)
    _inputs(gas=gas, gas_price=gas_price, max_gas_wei=max_gas_wei)
    _integer(ttl_seconds, 'Plazo de simulación', 120)
    try:
        base, tag = _operator_observation(rpc, wallet, creation_nonce, fee, now=now,
                                         gas=gas, gas_price=gas_price, max_gas_wei=max_gas_wei,snapshot=snapshot)
        state = base['snapshot']
        deadline = min(int(now.timestamp())+ttl_seconds, state.policy_limits.expires_at)
        fair_data = keccak(text='quoteTwap(bool,uint128)')[:4]+encode(['bool','uint128'], [buy, amount])
        raw = rpc('eth_call', [{'to': state.executor_address, 'data': '0x'+fair_data.hex()}, tag])
        if not isinstance(raw, str) or not raw.startswith('0x') or len(raw) != 66:raise ValueError('TWAP ABI')
        twap = decode(['uint256'], bytes.fromhex(raw[2:]), strict=True)[0]
        _integer(twap, 'Salida TWAP', 2**256-1)
        floor = max(1, twap*(10000-state.policy_limits.max_deviation_bps)//10000)
        transaction = OperatorPlan(**base)._transaction(_swap_data(state.policy_id, state.next_nonce, buy, amount, floor, deadline))
        raw = rpc('eth_call', [transaction, tag])
        if not isinstance(raw, str) or not raw.startswith('0x') or len(raw) != 66:raise ValueError('swap ABI')
        quote = decode(['uint256'], bytes.fromhex(raw[2:]), strict=True)[0]
        minimum = max(floor, quote*(10000-slippage_bps)//10000)
        result = DexTradePlan(**base, buy=buy, amount=amount, twap_out=twap, quote_out=quote,
                              slippage_bps=slippage_bps, minimum=minimum, deadline=deadline)
        result.validate(now)
        _same_block(rpc, state)
        return result
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, DecodingError):
        raise DexExecutorError('No se ha podido simular el intercambio completo del ejecutor') from None


def read_exit_cap(rpc:Callable,snapshot:ExecutorState,*,now:datetime)->int:
    """Conservative base-unit cap for the contract's TWAP exit notional.

    _quote(false, amount) in the pinned contract is floor(k*amount). If
    floor(k*owned)>limit, floor(owned*limit/floor(k*owned)) cannot exceed that
    limit after the contract's floor. No floating point or price inversion.
    """
    if type(snapshot) is not ExecutorState:raise DexExecutorError('Revisa la posición del ejecutor')
    snapshot.require_fresh(_time(now))
    if snapshot.owned_wbnb==0:return 0
    try:
        limits=snapshot.policy_limits
        if limits is None or not limits.allow_sell:return 0
        data=keccak(text='quoteTwap(bool,uint128)')[:4]+encode(['bool','uint128'],[False,snapshot.owned_wbnb])
        raw=rpc('eth_call',[{'to':snapshot.executor_address,'data':'0x'+data.hex()},hex(snapshot.block_number)])
        if not isinstance(raw,str) or len(raw)!=66 or not raw.startswith('0x'):raise ValueError('TWAP')
        fair=decode(['uint256'],bytes.fromhex(raw[2:]),strict=True)[0]
        if fair<=0:raise ValueError('empty quote')
        _same_block(rpc,snapshot)
        return min(snapshot.owned_wbnb,snapshot.owned_wbnb*limits.max_exit_notional//fair)
    except DexExecutorError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,DecodingError):
        raise DexExecutorError('No se pudo comprobar la cuota de salida del DEX') from None


def read_stop_plan(rpc: Callable, wallet: str, creation_nonce: int, fee: int, *, gas: int, gas_price: int,
                   max_gas_wei: int, now: datetime) -> DexStopPlan:
    now = _time(now)
    _inputs(gas=gas, gas_price=gas_price, max_gas_wei=max_gas_wei)
    if gas > STOP_GAS_LIMIT:raise DexExecutorError('La parada excede la reserva de gas prevista')
    try:
        base, _ = _operator_observation(rpc, wallet, creation_nonce, fee, now=now,
                                       gas=gas, gas_price=gas_price, max_gas_wei=max_gas_wei)
        result = DexStopPlan(**base)
        result.validate(now)
        _same_block(rpc, result.snapshot)
        return result
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('No se ha podido revisar la parada del operador') from None


@dataclass(frozen=True, slots=True)
class OperatorNonceObservation:
    snapshot: ExecutorState = field(repr=False)
    operator: str = field(repr=False)
    operator_nonce: int


def read_operator_nonce_observation(rpc: Callable, wallet: str, creation_nonce: int, fee: int, *,
                                    operator: str, now: datetime) -> OperatorNonceObservation:
    from app.portfolio.wallet_proof import canonical_wallet_address
    now = _time(now)
    address = canonical_wallet_address(operator)
    if address == ZERO:raise DexExecutorError('Indica la cuenta operadora original')
    try:
        state = read_executor_state(rpc,wallet,creation_nonce,fee,now=now,block_tag='finalized')
        nonce = quantity(rpc('eth_getTransactionCount',[address,hex(state.block_number)]))
        _integer(nonce,'Nonce finalizado del operador',2**64-1,zero=True)
        _same_block(rpc,state)
        return OperatorNonceObservation(state,address,nonce)
    except DexExecutorError:
        raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('No se pudo verificar el nonce finalizado del operador') from None
