"""Revocable dedicated EOA signer, RAM only, with conservative gas reservation.

Internal kernel, not a sending API. It does not broadcast or replace a durable
outbox. A worker must archive its intent and uncertainty before using a signature.
No owner key, arbitrary calldata, recipient, native transfer or generic signing.
"""
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from threading import RLock, Timer
import time

from eth_account import Account
from eth_utils import to_checksum_address, keccak
from eth_keys.constants import SECPK1_N
from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError
import rlp

from app.live_execution.dex_artifact import DexExecutorError, _integer
from app.live_execution.dex_permission import MAX_PERMISSION_SECONDS
from app.live_execution.dex_state import BnbReadClient, ExecutorState, ZERO
from app.live_execution.dex_policy import PolicyLimits
from app.live_execution.dex_trade import (
    DexTradePlan, DexStopPlan, MAX_GAS_BUDGET, STOP_GAS_LIMIT, read_trade_plan, read_stop_plan,
)
from app.portfolio.vault import RealVault, VaultError
from app.portfolio.wallet_funding import _time


@dataclass(frozen=True, slots=True, repr=False)
class DexSignature:
    raw_transaction: bytes = field(repr=False)
    tx_hash: str = field(repr=False)
    permission_revision: int = field(repr=False)


def verify_operator_signature(plan: DexTradePlan | DexStopPlan, signed: DexSignature) -> None:
    """Verify exact protected legacy bytes without broadcasting or persisting."""
    try:
        if type(plan) not in (DexTradePlan,DexStopPlan) or type(signed) is not DexSignature:raise ValueError('type')
        raw = signed.raw_transaction
        if not isinstance(raw,bytes) or not 100 <= len(raw) <= 1000:raise ValueError('RLP size')
        values = rlp.decode(raw,strict=True)
        tx = plan.transaction()
        integers = [int(tx[key],16) for key in ('nonce','gasPrice','gas')]
        expected = [value.to_bytes((value.bit_length()+7)//8,'big') for value in integers]
        expected += [bytes.fromhex(tx['to'][2:]),b'',bytes.fromhex(tx['data'][2:])]
        if len(values) != 9 or values[:6] != expected or values[6] not in (b'\x93',b'\x94'):raise ValueError('call/chain')
        for scalar in values[7:]:
            if not isinstance(scalar,bytes) or not 1 <= len(scalar) <= 32 or scalar[0] == 0:raise ValueError('scalar')
        if (not 0 < int.from_bytes(values[7]) < SECPK1_N or not 0 < int.from_bytes(values[8]) <= SECPK1_N//2
                or Account.recover_transaction(raw).lower() != plan.snapshot.operator
                or signed.tx_hash != '0x'+keccak(raw).hex()):raise ValueError('signature/hash')
    except (ValueError,TypeError,KeyError,AttributeError,rlp.exceptions.DecodingError,BadSignature,ValidationError):
        raise DexExecutorError('La firma no coincide con la llamada exacta del operador') from None


@dataclass(slots=True, repr=False)
class _Lease:
    key: bytearray = field(repr=False)
    generation: int
    owner: str = field(repr=False)
    executor: str = field(repr=False)
    operator: str = field(repr=False)
    policy_id: int
    limits: PolicyLimits = field(repr=False)
    deadline: float
    expires_at: int
    gas_budget: int
    stop_reserve: int
    gas_reserved: int = 0


class DexOperatorSession:
    def __init__(self, vault: RealVault, *, monotonic=time.monotonic):
        self.vault = vault
        self._clock = monotonic
        self._lock = RLock()
        self._lease: _Lease | None = None
        self._timer: Timer | None = None
        self._epoch = 0
        # Preserve reservations across explicit reconnects within this process.
        # This is intentionally not represented as crash-safe nonce recovery.
        self._used_nonces: dict[tuple[str, int],tuple[int,int]] = {}

    def revoke(self) -> None:
        with self._lock:
            self._epoch += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._lease is not None:
                self._lease.key[:] = bytes(len(self._lease.key))
            self._lease = None

    @property
    def permission_revision(self) -> int:
        """Opaque in-process revision; it conveys no signing authority."""
        with self._lock:return self._epoch

    def revoke_revision(self,revision:int) -> bool:
        """A stale connection must never erase a newer signing lease."""
        with self._lock:
            if type(revision) is not int or self._epoch!=revision:return False
            self.revoke();return True

    @contextmanager
    def guard_emission(self, revision: int):
        """Linearize the durable attempt against local revocation, never RPC."""
        with self._lock:
            if self._lease and self._clock() >= self._lease.deadline:
                self.revoke()
            if type(revision) is not int or revision != self._epoch:
                raise DexExecutorError('La firma preparada pertenece a un permiso local retirado')
            yield

    def _expire(self, lease: _Lease) -> None:
        with self._lock:
            if self._lease is lease:
                self.revoke()

    def _discard_unsent_reservation(self, plan: DexTradePlan | DexStopPlan) -> None:
        """Only outbox abandonment, after durable proof that no POST was granted."""
        if type(plan) not in (DexTradePlan,DexStopPlan):raise DexExecutorError('Reserva de firma no válida')
        with self._lock:
            key = (plan.snapshot.operator,plan.operator_nonce)
            saved = self._used_nonces.get(key)
            if saved is None:return
            epoch,gas = saved
            if gas != plan.gas*plan.gas_price:raise DexExecutorError('La reserva de gas pertenece a otra preparación')
            if self._lease is not None and self._epoch == epoch:
                if gas > self._lease.gas_reserved:raise DexExecutorError('Reserva local de gas incoherente')
                self._lease.gas_reserved -= gas
            del self._used_nonces[key]

    def connect(self, *, private_key: bytes, snapshot: ExecutorState, ttl_seconds: int, gas_budget_wei: int,
                backup_confirmed: bool, now: datetime) -> dict:
        now = _time(now)
        _integer(ttl_seconds, 'Duración de firma local', MAX_PERMISSION_SECONDS)
        _integer(gas_budget_wei, 'Presupuesto local de gas', MAX_GAS_BUDGET)
        if backup_confirmed is not True or type(private_key) is not bytes or len(private_key) != 32:
            raise DexExecutorError('Usa una clave operadora dedicada y conserva su recuperación fuera de ORBIS')
        generation = self.vault.session_generation
        try:
            if generation is None or type(snapshot) is not ExecutorState:
                raise ValueError('locked or invalid state')
            snapshot.require_fresh(now)
            if (snapshot.operator == ZERO or snapshot.policy_limits is None or snapshot.current_operator_code_empty is not True
                    or snapshot.operator in {snapshot.owner, snapshot.executor_address}
                    or Account.from_key(private_key).address.lower() != snapshot.operator):
                raise ValueError('dedicated operator')
            snapshot.policy_limits.validate_at(int(now.timestamp()), snapshot.owned_wbnb)
            seconds = min(ttl_seconds, snapshot.policy_limits.expires_at-now.timestamp())
            reserve = STOP_GAS_LIMIT*snapshot.policy_limits.max_gas_price
            if seconds <= 0 or gas_budget_wei < reserve:raise ValueError('deadline or STOP gas')
            with self.vault.guard_session_generation(generation), self._lock:
                self.revoke()
                self._lease = _Lease(bytearray(private_key), generation, snapshot.owner, snapshot.executor_address,
                    snapshot.operator, snapshot.policy_id, snapshot.policy_limits, self._clock()+seconds,
                    snapshot.policy_limits.expires_at, gas_budget_wei, reserve)
                self._timer = Timer(seconds, self._expire, args=(self._lease,))
                self._timer.daemon = True
                try:
                    self._timer.start()
                except Exception:
                    self.revoke()
                    raise DexExecutorError('No se pudo iniciar la caducidad; firma local retirada') from None
            return self.status()
        except (ValueError, TypeError, VaultError):
            raise DexExecutorError('La sesión no coincide con el operador autorizado o con sus límites') from None
        finally:
            del private_key

    def status(self) -> dict:
        generation = self.vault.session_generation
        with self._lock:
            lease = self._lease
            if lease and (generation != lease.generation or self._clock() >= lease.deadline):
                self.revoke()
                lease = None
            return {'connected': lease is not None, 'credential_storage': 'MEMORY_ONLY', 'automatic_execution': False,
                    'remaining_seconds': max(0, int(lease.deadline-self._clock())) if lease else 0,
                    'gas_budget_wei': str(lease.gas_budget) if lease else '0',
                    'gas_reserved_wei': str(lease.gas_reserved) if lease else '0',
                    'remaining_gas_wei': str(lease.gas_budget-lease.gas_reserved) if lease else '0',
                    'stop_reserve_wei': str(lease.stop_reserve) if lease else '0'}

    def sign_swap(self, plan: DexTradePlan, *, now: datetime) -> DexSignature:
        if type(plan) is not DexTradePlan:raise DexExecutorError('Plan de intercambio no validado')
        return self._sign(plan, now=now, stop=False)

    def sign_stop(self, plan: DexStopPlan, *, now: datetime) -> DexSignature:
        if type(plan) is not DexStopPlan:raise DexExecutorError('Plan de parada no validado')
        return self._sign(plan, now=now, stop=True)

    def _sign(self, plan: DexTradePlan | DexStopPlan, *, now: datetime, stop: bool) -> DexSignature:
        now = _time(now)
        generation = self.vault.session_generation
        if generation is None:
            self.revoke()
            raise DexExecutorError('La bóveda está bloqueada; firma local retirada')
        try:
            with self.vault.guard_session_generation(generation), self._lock:
                lease = self._lease
                if not lease or lease.generation != generation or self._clock() >= lease.deadline or now.timestamp() >= lease.expires_at:
                    self.revoke()
                    raise DexExecutorError('La firma local ha sido retirada o ha caducado')
                plan.validate(now)
                state = plan.snapshot
                if (state.owner != lease.owner or state.executor_address != lease.executor or state.operator != lease.operator
                        or state.policy_id != lease.policy_id or state.policy_limits != lease.limits):
                    self.revoke()
                    raise DexExecutorError('La política DEX cambió; revisa y conecta de nuevo')
                nonce = (lease.operator, plan.operator_nonce)
                gas = plan.gas*plan.gas_price
                available = lease.gas_budget-lease.gas_reserved-(0 if stop else lease.stop_reserve)
                if (nonce in self._used_nonces or gas > available or (not stop and len(self._used_nonces) >= 10000)
                        or plan.operator_balance < gas+(0 if stop else lease.stop_reserve)):
                    raise DexExecutorError('Nonce ya reservado o presupuesto local de gas insuficiente')
                transaction = plan.transaction()
                # Only internally compiled plans reach the signer, never a client
                # transaction dictionary. Reserve conservatively before signing.
                self._used_nonces[nonce] = (self._epoch,gas)
                lease.gas_reserved += gas
                unsigned = {name: (int(value, 16) if name in {'chainId','nonce','gas','gasPrice','value'} else value)
                            for name, value in transaction.items() if name not in {'from','type'}}
                unsigned['to'] = to_checksum_address(unsigned['to'])
                try:
                    signed = Account.sign_transaction(unsigned, bytes(lease.key))
                except Exception:
                    raise DexExecutorError('No se produjo una firma; el nonce sigue reservado para revisión') from None
                if stop:self.revoke()
                return DexSignature(bytes(signed.raw_transaction), '0x'+bytes(signed.hash).hex(), self._epoch)
        except VaultError:
            self.revoke()
            raise DexExecutorError('La sesión de bóveda cambió; firma local retirada') from None
