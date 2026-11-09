"""Bounded zero-to-self cancellation plans. No keys, SQL or transaction sends."""
from dataclasses import dataclass,field
from datetime import datetime
from eth_account import Account
from eth_keys.constants import SECPK1_N
from eth_keys.exceptions import BadSignature
from eth_utils import keccak,ValidationError
import rlp
from app.live_execution.dex_artifact import DexExecutorError,_integer
from app.live_execution.evm_replacement_state import EoaReplacementState,read_eoa_replacement_state
from app.live_execution.dex_trade import MAX_GAS_BUDGET

GAS=21000


@dataclass(frozen=True,slots=True)
class CancellationState:
    owner:str=field(repr=False)
    operator:str=field(repr=False)
    block_number:int
    block_hash:str
    block_at:datetime
    latest_number:int
    latest_hash:str
    latest_at:datetime
    observed_at:datetime
    balance:int=field(repr=False)
    latest_balance:int=field(repr=False)
    nonce:int
    latest_nonce:int
    live_nonce:int
    pending_nonce:int
    owner_code_empty:bool
    operator_code_empty:bool

    def require_fresh(self,now):
        EoaReplacementState(self.operator,self.owner,self.block_number,self.block_hash,self.block_at,
            self.latest_number,self.latest_hash,self.latest_at,self.observed_at,self.balance,self.latest_balance,
            self.nonce,self.latest_nonce,self.live_nonce,self.pending_nonce,self.operator_code_empty,self.owner_code_empty).require_fresh(now)


@dataclass(frozen=True,slots=True)
class CancellationPlan:
    snapshot:CancellationState=field(repr=False)
    original_nonce:int
    original_gas_price:int
    gas_price:int
    bump_percent:int
    max_gas_wei:int

    @property
    def gas_wei(self):return GAS*self.gas_price

    @property
    def minimum_price(self):return max(self.original_gas_price+1,(self.original_gas_price*(100+self.bump_percent)+99)//100)

    def validate(self,now):
        if type(self.snapshot) is not CancellationState:raise DexExecutorError('Revisa el estado de cancelación exacto')
        self.snapshot.require_fresh(now)
        _integer(self.original_nonce,'Nonce original',2**64-2,zero=True)
        _integer(self.original_gas_price,'Precio original',10**12)
        _integer(self.gas_price,'Precio de cancelación',10**12)
        _integer(self.bump_percent,'Aumento mínimo',1000)
        _integer(self.max_gas_wei,'Coste máximo',MAX_GAS_BUDGET)
        if self.snapshot.nonce!=self.original_nonce:raise DexExecutorError('El nonce original ya cambió; comprueba su recibo')
        if self.gas_price<self.minimum_price:raise DexExecutorError('El precio de cancelación no cubre el aumento mínimo revisado')
        if self.gas_wei>self.max_gas_wei or self.gas_wei>self.snapshot.balance:raise DexExecutorError('El saldo o coste máximo no cubre la cancelación')

    def transaction(self):
        return {'from':self.snapshot.operator,'to':self.snapshot.operator,'value':'0x0','data':'0x','type':'0x0',
            'chainId':'0x38','nonce':hex(self.original_nonce),'gas':hex(GAS),'gasPrice':hex(self.gas_price)}


def plan_cancellation(state,*,original_nonce,original_gas_price,gas_price,bump_percent,max_gas_wei,now):
    result=CancellationPlan(state,original_nonce,original_gas_price,gas_price,bump_percent,max_gas_wei)
    result.validate(now);return result


def read_cancellation_state(rpc,wallet,operator,*,nonce,now):
    state=read_eoa_replacement_state(rpc,operator,wallet,nonce=nonce,now=now)
    return CancellationState(state.counterparty,state.sender,state.block_number,state.block_hash,state.block_at,
        state.latest_number,state.latest_hash,state.latest_at,state.observed_at,state.balance,state.latest_balance,
        state.nonce,state.latest_nonce,state.live_nonce,state.pending_nonce,state.counterparty_code_empty,state.sender_code_empty)


def verify_cancellation_signature(plan,raw,tx_hash):
    """Exact protected legacy zero-self bytes; never a transfer or swap signer."""
    try:
        if type(plan) is not CancellationPlan or type(raw) is not bytes or not 90<=len(raw)<=200:raise ValueError('scope')
        plan.validate(plan.snapshot.observed_at);values=rlp.decode(raw,strict=True)
        def encoded(value):return value.to_bytes((value.bit_length()+7)//8,'big')
        expected=[encoded(plan.original_nonce),encoded(plan.gas_price),encoded(GAS),bytes.fromhex(plan.snapshot.operator[2:]),b'',b'']
        if len(values)!=9 or values[:6]!=expected or values[6] not in (b'\x93',b'\x94'):raise ValueError('native call/chain')
        for value in values[7:]:
            if type(value) is not bytes or not 1<=len(value)<=32 or value[0]==0:raise ValueError('scalar')
        if (not 0<int.from_bytes(values[7])<SECPK1_N or not 0<int.from_bytes(values[8])<=SECPK1_N//2
                or Account.recover_transaction(raw).lower()!=plan.snapshot.operator or tx_hash!='0x'+keccak(raw).hex()):raise ValueError('signature/hash')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,rlp.exceptions.DecodingError,BadSignature,ValidationError):
        raise DexExecutorError('La firma no corresponde a la cancelación exacta de valor cero') from None
