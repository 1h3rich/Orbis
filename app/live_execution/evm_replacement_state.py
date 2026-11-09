"""Fresh EOA nonce/balance observations shared by native replacement plans.

Only public reads. A matching observation cannot make the later network POST
atomic with the chain; the winning receipt remains necessary.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
from app.live_execution.dex_artifact import DexExecutorError,_owner,_integer
from app.live_execution.dex_owner_observation import _same_block
from app.live_execution.dex_state import Rpc
from app.live_execution.evm_transaction import quantity,word
from app.portfolio.wallet_funding import _time


@dataclass(frozen=True,slots=True)
class EoaReplacementState:
    sender:str=field(repr=False)
    counterparty:str=field(repr=False)
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
    sender_code_empty:bool
    counterparty_code_empty:bool

    def require_fresh(self,now:datetime)->None:
        now=_time(now)
        try:
            if (self.sender!=_owner(self.sender) or self.counterparty!=_owner(self.counterparty) or self.sender==self.counterparty
                    or self.sender_code_empty is not True or self.counterparty_code_empty is not True):raise ValueError('EOA')
            for value in (self.block_number,self.latest_number):_integer(value,'Bloque',2**256-1)
            for value in (self.block_hash,self.latest_hash):
                if value!=word(value):raise ValueError('block hash')
            for value in (self.balance,self.latest_balance):_integer(value,'BNB',2**256-1,zero=True)
            for value in (self.nonce,self.latest_nonce,self.live_nonce,self.pending_nonce):_integer(value,'Nonce',2**64-1,zero=True)
            if (self.latest_number<self.block_number or self.latest_at<self.block_at or self.latest_balance<self.balance
                    or self.block_number==self.latest_number and (self.block_hash!=self.latest_hash or self.block_at!=self.latest_at)
                    or self.latest_nonce!=self.nonce or self.live_nonce!=self.nonce or self.pending_nonce not in (self.nonce,self.nonce+1)
                    or not 0<=(now-_time(self.observed_at)).total_seconds()<=15
                    or any(not -15<=(now-_time(at)).total_seconds()<=30 for at in (self.block_at,self.latest_at))):raise ValueError('nonce/freshness')
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise DexExecutorError('El nonce, las cuentas o el saldo de cancelación cambiaron; comprueba el original') from None


def read_eoa_replacement_state(rpc:Rpc,sender:str,counterparty:str,*,nonce:int,now:datetime)->EoaReplacementState:
    now=_time(now);sender,counterparty=_owner(sender),_owner(counterparty);_integer(nonce,'Nonce original',2**64-2,zero=True)
    try:
        if sender==counterparty or quantity(rpc('eth_chainId',[]))!=56:raise ValueError('chain/owner')
        final=rpc('eth_getBlockByNumber',['finalized',False]);latest=rpc('eth_getBlockByNumber',['latest',False])
        f,l=quantity(final['number']),quantity(latest['number']);ft,lt=hex(f),hex(l);cache={}
        def read(method:str,params:list)->object:
            key=(method,*params)
            if key not in cache:cache[key]=rpc(method,params)
            return cache[key]
        counterparty_empty=all(read('eth_getCode',[counterparty,tag])=='0x' for tag in (ft,lt))
        sender_empty=all(read('eth_getCode',[sender,tag])=='0x' for tag in (ft,lt))
        balances=[quantity(read('eth_getBalance',[sender,tag])) for tag in (ft,lt)]
        nonces=[quantity(read('eth_getTransactionCount',[sender,tag])) for tag in (ft,lt)]
        pending=quantity(rpc('eth_getTransactionCount',[sender,'pending']))
        _same_block(rpc,final,ft)
        if lt!=ft:_same_block(rpc,latest,lt)
        live=quantity(rpc('eth_getTransactionCount',[sender,'latest']))
        state=EoaReplacementState(sender,counterparty,f,word(final['hash']),datetime.fromtimestamp(quantity(final['timestamp']),timezone.utc),
            l,word(latest['hash']),datetime.fromtimestamp(quantity(latest['timestamp']),timezone.utc),now,*balances,*nonces,live,pending,sender_empty,counterparty_empty)
        state.require_fresh(now)
        if state.nonce!=nonce:raise ValueError('original consumed')
        return state
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('No se pudo verificar un nonce original sin minar y saldo para cancelarlo') from None
