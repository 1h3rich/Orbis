"""Exact v1 executor policy and owner-wallet calls, without signing or sending.

Amounts are integers in 18-decimal token units; gas prices are integer wei.
Addresses exist only while composing calls. EOA/code, nonce and current policy
must additionally be checked by a fresh chain observation before permission.
"""
from dataclasses import dataclass, fields

from eth_abi import encode
from eth_utils import keccak

from app.live_execution.dex_artifact import DexExecutorError, executor_address, _owner, _integer
from app.portfolio.wallet_funding import _uint


POLICY_ABI = '(address,uint64,uint64,uint32,uint32,uint16,uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool,bool)'
CONFIGURE_SIGNATURE = 'configure(uint256,'+POLICY_ABI+')'
UNIT = 10**18
_AMOUNTS = ('max_buy_per_order', 'buy_budget', 'max_exposure', 'max_exit_notional',
            'max_realized_loss', 'max_gas_price', 'max_wbnb_acquired')


@dataclass(frozen=True, slots=True)
class PolicyLimits:
    expires_at: int
    cooldown_seconds: int
    max_entries: int
    twap_seconds: int
    max_deviation_bps: int
    max_buy_per_order: int
    buy_budget: int
    max_exposure: int
    max_exit_notional: int
    max_realized_loss: int
    max_gas_price: int
    max_wbnb_acquired: int
    allow_buy: bool
    allow_sell: bool

    def __post_init__(self) -> None:
        for name in ('allow_buy', 'allow_sell'):
            if type(getattr(self, name)) is not bool:
                raise DexExecutorError('Los permisos DEX deben ser booleanos')
        _integer(self.expires_at, 'Caducidad', 2**64-1)
        _integer(self.cooldown_seconds, 'Espera', 86400, zero=True)
        _integer(self.max_entries, 'Entradas', 1000, zero=True)
        _integer(self.twap_seconds, 'TWAP', 86400)
        _integer(self.max_deviation_bps, 'Desviación', 500)
        if self.twap_seconds < 300 or not (self.allow_buy or self.allow_sell):
            raise DexExecutorError('TWAP o permisos DEX no válidos')
        for name in ('max_buy_per_order', 'buy_budget', 'max_exposure', 'max_exit_notional', 'max_realized_loss'):
            _integer(getattr(self, name), name, (1000 if name == 'max_buy_per_order' else 10000)*UNIT, zero=True)
        _integer(self.max_gas_price, 'Precio máximo de gas', 10**12)
        _integer(self.max_wbnb_acquired, 'Cuota WBNB', 2**128-1)
        entries = (self.cooldown_seconds, self.max_entries, self.max_buy_per_order, self.buy_budget,
                   self.max_exposure, self.max_realized_loss)
        if self.allow_buy:
            if (self.cooldown_seconds < 60 or any(value == 0 for value in entries)
                    or self.buy_budget < self.max_buy_per_order):
                raise DexExecutorError('Los límites de compra DEX no son coherentes')
        elif any(entries):
            raise DexExecutorError('El permiso de sólo cierre requiere límites de entrada cero')
        if self.allow_sell and self.max_exit_notional == 0:
            raise DexExecutorError('Define el límite de salida DEX')

    @classmethod
    def parse(cls, value: dict) -> 'PolicyLimits':
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise DexExecutorError('Parámetros de política DEX no válidos')
        normalized = dict(value)
        for name in _AMOUNTS:
            try:
                normalized[name] = _uint(value[name], name, positive=False)
            except ValueError:
                raise DexExecutorError('Los importes DEX deben ser enteros decimales exactos') from None
        return cls(**normalized)

    def as_dict(self) -> dict:
        return {field.name: str(getattr(self, field.name)) if field.name in _AMOUNTS else getattr(self, field.name)
                for field in fields(self)}

    def validate_at(self, now: int, owned_wbnb: int) -> None:
        _integer(now, 'Reloj', 2**64-1)
        _integer(owned_wbnb, 'Posición WBNB', 2**128-1, zero=True)
        if not now < self.expires_at <= now+86400:
            raise DexExecutorError('La política DEX debe caducar en un máximo de 24 horas')
        if self.max_wbnb_acquired < owned_wbnb:
            raise DexExecutorError('La cuota WBNB no cubre la posición propia existente')

    def abi_values(self, operator: str) -> tuple:
        return (_owner(operator), *(getattr(self, field.name) for field in fields(self)))


def _transaction(wallet: str, creation_nonce: int, data: bytes, *, nonce: int, gas: int, gas_price: int) -> dict:
    _integer(nonce, 'Nonce', 2**64-1, zero=True)
    _integer(gas, 'Gas', 2_000_000)
    _integer(gas_price, 'Gas price', 10**12)
    return {'from': _owner(wallet), 'to': executor_address(wallet, creation_nonce), 'chainId': '0x38', 'type': '0x0',
            'value': '0x0', 'data': '0x'+data.hex(), 'nonce': hex(nonce), 'gas': hex(gas), 'gasPrice': hex(gas_price)}


def configure_transaction(wallet: str, creation_nonce: int, operator: str, expected_policy: int, limits: PolicyLimits, *,
                          nonce: int, gas: int, gas_price: int, now: int, owned_wbnb: int) -> dict:
    if type(limits) is not PolicyLimits:
        raise DexExecutorError('Política DEX no validada')
    _integer(expected_policy, 'Revisión de política', 2**256-2, zero=True)
    limits.validate_at(now, owned_wbnb)
    owner, operator = _owner(wallet), _owner(operator)
    if operator in {owner, executor_address(owner, creation_nonce)}:
        raise DexExecutorError('Utiliza un operador distinto de la wallet propietaria y el ejecutor')
    data = keccak(text=CONFIGURE_SIGNATURE)[:4]+encode(['uint256', POLICY_ABI], [expected_policy, limits.abi_values(operator)])
    return _transaction(owner, creation_nonce, data, nonce=nonce, gas=gas, gas_price=gas_price)


def stop_transaction(wallet: str, creation_nonce: int, *, nonce: int, gas: int, gas_price: int) -> dict:
    return _transaction(wallet, creation_nonce, keccak(text='stop()')[:4], nonce=nonce, gas=gas, gas_price=gas_price)
