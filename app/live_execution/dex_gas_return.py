"""Exact owner-only residual BNB plans; no keys, signing, DB writes or sends.

A plan is not authority to broadcast. Its future journal/outbox must bind the
verified draft owner, stopped bot family, generation and unique native nonce.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
from typing import Callable,Any

from eth_account import Account
from eth_keys.constants import SECPK1_N
from eth_keys.exceptions import BadSignature
from eth_utils import keccak,ValidationError
import rlp

from app.live_execution.dex_artifact import DexExecutorError,_owner,_integer
from app.live_execution.dex_trade import MAX_GAS_BUDGET
from app.live_execution.evm_transaction import quantity,word
from app.portfolio.wallet_funding import _time

RETURN_GAS=21000
Rpc=Callable[[str,list],Any]


@dataclass(frozen=True,slots=True)
class GasReturnState:
    owner:str=field(repr=False)
    operator:str=field(repr=False)
    block_number:int
    block_hash:str
    block_at:datetime
    observed_at:datetime
    operator_balance:int=field(repr=False)
    operator_nonce:int
    pending_nonce:int
    owner_code_empty:bool
    operator_code_empty:bool

    def require_fresh(self,now:datetime)->None:
        now=_time(now)
        try:
            if (self.owner!=_owner(self.owner) or self.operator!=_owner(self.operator)
                    or self.owner==self.operator or self.owner_code_empty is not True or self.operator_code_empty is not True):
                raise ValueError('EOA scope')
            _integer(self.block_number,'Bloque',2**256-1)
            if self.block_hash!=word(self.block_hash):raise ValueError('block hash')
            _integer(self.operator_balance,'Saldo BNB',2**256-1,zero=True)
            for value in (self.operator_nonce,self.pending_nonce):_integer(value,'Nonce operadora',2**64-1,zero=True)
            if (self.operator_nonce!=self.pending_nonce or not 0<=(now-_time(self.observed_at)).total_seconds()<=15
                    or not -15<=(now-_time(self.block_at)).total_seconds()<=30):raise ValueError('nonce/freshness')
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise DexExecutorError('Revisa el saldo, nonce y cuentas de la devolución BNB') from None


@dataclass(frozen=True,slots=True)
class GasReturnPlan:
    snapshot:GasReturnState=field(repr=False)
    amount_wei:int
    gas_price:int
    max_gas_wei:int

    @property
    def gas_wei(self)->int:return RETURN_GAS*self.gas_price

    def validate(self,now:datetime)->None:
        if type(self.snapshot) is not GasReturnState:raise DexExecutorError('La devolución necesita una lectura BNB verificada')
        self.snapshot.require_fresh(now)
        _integer(self.gas_price,'Precio del gas',10**12)
        _integer(self.max_gas_wei,'Gas máximo',MAX_GAS_BUDGET)
        _integer(self.amount_wei,'Devolución BNB',2**256-1)
        if self.gas_wei>self.max_gas_wei or self.amount_wei!=self.snapshot.operator_balance-self.gas_wei:
            raise DexExecutorError('El saldo BNB no cubre la devolución exacta y su gas')

    def transaction(self)->dict[str,str]:
        return {'from':self.snapshot.operator,'to':self.snapshot.owner,'chainId':'0x38','type':'0x0',
            'nonce':hex(self.snapshot.operator_nonce),'gas':hex(RETURN_GAS),'gasPrice':hex(self.gas_price),
            'value':hex(self.amount_wei),'data':'0x'}


def read_return_state(rpc:Rpc,wallet:str,operator:str,*,now:datetime)->GasReturnState:
    now=_time(now);wallet,operator=_owner(wallet),_owner(operator)
    try:
        if wallet==operator or quantity(rpc('eth_chainId',[]))!=56:raise ValueError('chain/owner')
        block=rpc('eth_getBlockByNumber',['finalized',False])
        number=quantity(block['number']);block_hash=word(block['hash']);tag=hex(number)
        at=datetime.fromtimestamp(quantity(block['timestamp']),timezone.utc)
        owner_empty=rpc('eth_getCode',[wallet,tag])=='0x'
        operator_empty=rpc('eth_getCode',[operator,tag])=='0x'
        balance=quantity(rpc('eth_getBalance',[operator,tag]))
        nonce=quantity(rpc('eth_getTransactionCount',[operator,tag]))
        pending=quantity(rpc('eth_getTransactionCount',[operator,'pending']))
        repeated=rpc('eth_getBlockByNumber',[tag,False])
        if (quantity(repeated['number'])!=number or word(repeated['hash'])!=block_hash
                or quantity(repeated['timestamp'])!=quantity(block['timestamp'])):raise ValueError('block changed')
        result=GasReturnState(wallet,operator,number,block_hash,at,now,balance,nonce,pending,owner_empty,operator_empty)
        result.require_fresh(now);return result
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('No se pudo verificar la devolución BNB en un bloque finalizado') from None


def plan_return(state:GasReturnState,*,gas_price:int,max_gas_wei:int,now:datetime)->GasReturnPlan:
    if type(state) is not GasReturnState:raise DexExecutorError('Revisa el estado BNB antes de devolver gas')
    # Validate before arithmetic: bool/float inputs never become financial ints.
    _integer(gas_price,'Precio del gas',10**12)
    state.require_fresh(now)
    result=GasReturnPlan(state,state.operator_balance-RETURN_GAS*gas_price,gas_price,max_gas_wei)
    result.validate(now);return result


def verify_return_signature(plan:GasReturnPlan,raw:bytes,tx_hash:str)->None:
    """Exact EIP-155 legacy bytes, native value to this owner and no calldata."""
    try:
        if type(plan) is not GasReturnPlan or type(raw) is not bytes or not 90<=len(raw)<=200:raise ValueError('signature scope')
        plan.validate(plan.snapshot.observed_at)
        values=rlp.decode(raw,strict=True);tx=plan.transaction()
        def encoded(value:int)->bytes:return value.to_bytes((value.bit_length()+7)//8,'big')
        expected=[encoded(int(tx[key],16)) for key in ('nonce','gasPrice','gas')]
        expected+=[bytes.fromhex(tx['to'][2:]),encoded(plan.amount_wei),b'']
        if len(values)!=9 or values[:6]!=expected or values[6] not in (b'\x93',b'\x94'):raise ValueError('native call/chain')
        for scalar in values[7:]:
            if type(scalar) is not bytes or not 1<=len(scalar)<=32 or scalar[0]==0:raise ValueError('scalar')
        if (not 0<int.from_bytes(values[7])<SECPK1_N or not 0<int.from_bytes(values[8])<=SECPK1_N//2
                or Account.recover_transaction(raw).lower()!=plan.snapshot.operator
                or tx_hash!='0x'+keccak(raw).hex()):raise ValueError('signature/hash')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,rlp.exceptions.DecodingError,BadSignature,ValidationError):
        raise DexExecutorError('La firma no corresponde a la devolución exacta al propietario') from None
