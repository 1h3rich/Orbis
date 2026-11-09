"""Read native BNB funding state/receipts. Never signs or sends funds."""
from dataclasses import dataclass
from datetime import datetime

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_state import ExecutorState,read_executor_state
from app.live_execution.evm_transaction import quantity,word


@dataclass(frozen=True,slots=True)
class FundingState:
    executor:ExecutorState
    operator_balance:int


@dataclass(frozen=True, slots=True)
class OperatorGasCoverage:
    available_wei: int
    stop_reserve_wei: int
    order_wei: int

    @property
    def required_wei(self) -> int:
        return self.stop_reserve_wei + self.order_wei


def operator_gas_coverage(snapshot: FundingState, budget: dict, order_wei: int) -> OperatorGasCoverage:
    if type(order_wei) is not int or order_wei < 0:
        raise DexExecutorError('Gas por orden no válido')
    return OperatorGasCoverage(min(snapshot.operator_balance, int(budget['remaining_gas_wei'])),
                               int(budget['stop_reserve_wei']), order_wei)


def read_funding_state(rpc,wallet:str,creation_nonce:int,fee:int,*,now:datetime)->FundingState:
    snapshot=read_executor_state(rpc,wallet,creation_nonce,fee,now=now,block_tag='finalized')
    try:
        tag=hex(snapshot.block_number)
        balance=quantity(rpc('eth_getBalance',[snapshot.operator,tag]))
        repeated=rpc('eth_getBlockByNumber',[tag,False])
        if quantity(repeated['number'])!=snapshot.block_number or word(repeated['hash'])!=snapshot.block_hash:
            raise ValueError('block changed')
        return FundingState(snapshot,balance)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('No se pudo verificar el saldo BNB de la operadora en el mismo bloque') from None


def collect_funding_receipt(rpc,tx_hash:str)->dict|None:
    try:
        tx_hash=word(tx_hash)
        if quantity(rpc('eth_chainId',[]))!=56:raise ValueError('chain')
        tx=rpc('eth_getTransactionByHash',[tx_hash]);receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt is None:return None
        if not isinstance(tx,dict) or not isinstance(receipt,dict):raise ValueError('pending')
        tag=hex(quantity(receipt['blockNumber']))
        block=rpc('eth_getBlockByNumber',[tag,False]);finalized=rpc('eth_getBlockByNumber',['finalized',False])
        repeated=rpc('eth_getBlockByNumber',[tag,False])
        if (word(repeated['hash'])!=word(block['hash']) or quantity(repeated['number'])!=quantity(block['number'])):
            raise ValueError('block changed')
        return {'chain_id':'0x38','tx_hash':tx_hash,'transaction':tx,'receipt':receipt,'block':block,'finalized':finalized}
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('No se pudo observar un recibo BNB verificable') from None
