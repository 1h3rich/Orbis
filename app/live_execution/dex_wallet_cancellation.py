"""Review a funding cancellation for the owner wallet; never signs or sends.

Actual signed original fees are optional evidence, never inferred from the
funding draft. Missing evidence requires explicit uncertainty acceptance.
"""
from dataclasses import dataclass,field
from datetime import datetime
from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError
from app.live_execution.dex_artifact import DexExecutorError,_owner,_integer
from app.live_execution.dex_state import Rpc
from app.live_execution.dex_trade import MAX_GAS_BUDGET
from app.live_execution.evm_replacement_state import EoaReplacementState,read_eoa_replacement_state
from app.live_execution.evm_transaction import quantity,word,recover_wallet_transfer

GAS=21000


@dataclass(frozen=True,slots=True)
class PendingFundingFee:
    wallet:str=field(repr=False)
    recipient:str=field(repr=False)
    tx_hash:str=field(repr=False)
    nonce:int
    amount_wei:int=field(repr=False)
    transaction_type:int
    gas_limit:int
    fee_cap:int
    priority_fee:int|None

    def validate(self)->None:
        if self.wallet!=_owner(self.wallet) or self.recipient!=_owner(self.recipient) or self.wallet==self.recipient:
            raise DexExecutorError('La financiación pertenece a otras cuentas')
        if self.tx_hash!=word(self.tx_hash):raise DexExecutorError('Hash de financiación no canónico')
        _integer(self.nonce,'Nonce original',2**64-2,zero=True)
        _integer(self.amount_wei,'Importe original',MAX_GAS_BUDGET)
        _integer(self.gas_limit,'Gas original',100000)
        _integer(self.fee_cap,'Precio original firmado',10**12)
        if self.gas_limit<GAS or type(self.transaction_type) is not int or self.transaction_type not in (0,2):
            raise DexExecutorError('Tipo o gas de financiación no compatible')
        if self.transaction_type==0:
            if self.priority_fee is not None:raise DexExecutorError('Propina legacy no válida')
        else:
            _integer(self.priority_fee,'Propina original',10**12,zero=True)
            if self.priority_fee>self.fee_cap:raise DexExecutorError('Propina superior al máximo original')


def _pending_fee(tx:object,wallet:str,recipient:str,nonce:int,amount:int,tx_hash:str)->PendingFundingFee|None:
    if tx is None:return None
    try:
        if not isinstance(tx,dict):raise ValueError('transaction')
        sender,kind,gas,cap,priority=recover_wallet_transfer(tx,expected_to=recipient,expected_value=amount)
        if sender!=wallet or quantity(tx['nonce'])!=nonce or word(tx['hash'])!=tx_hash:raise ValueError('original scope')
        positions=('blockNumber','blockHash','transactionIndex')
        if not all(key in tx for key in positions):raise ValueError('pending shape')
        if any(tx[key] is not None for key in positions):raise DexExecutorError('El original ya figura minado; comprueba su recibo')
        result=PendingFundingFee(wallet,recipient,tx_hash,nonce,amount,kind,gas,cap,priority);result.validate();return result
    except DexExecutorError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,BadSignature,ValidationError):
        raise DexExecutorError('No se pudo verificar la firma, tarifa y alcance de la financiación pendiente') from None


@dataclass(frozen=True,slots=True)
class WalletCancellationState:
    snapshot:EoaReplacementState=field(repr=False)
    original_amount_wei:int=field(repr=False)
    original_hash:str|None=field(repr=False)
    original_fee:PendingFundingFee|None

    def require_fresh(self,now:datetime)->None:
        if type(self.snapshot) is not EoaReplacementState:raise DexExecutorError('Revisa el estado exacto de la wallet')
        self.snapshot.require_fresh(now)
        _integer(self.original_amount_wei,'Importe original',MAX_GAS_BUDGET)
        if self.original_hash is not None and self.original_hash!=word(self.original_hash):raise DexExecutorError('Hash original no canónico')
        if self.original_fee is not None:
            fee=self.original_fee
            if type(fee) is not PendingFundingFee:raise DexExecutorError('Revisa la tarifa original firmada')
            fee.validate()
            if (fee.wallet!=self.snapshot.sender or fee.recipient!=self.snapshot.counterparty or fee.nonce!=self.snapshot.nonce
                    or fee.amount_wei!=self.original_amount_wei or fee.tx_hash!=self.original_hash):
                raise DexExecutorError('La evidencia de tarifa pertenece a otra financiación')


@dataclass(frozen=True,slots=True)
class WalletCancellationPlan:
    state:WalletCancellationState=field(repr=False)
    gas_price:int
    bump_percent:int
    max_gas_wei:int
    accept_unknown_fee:bool
    previous_prices:tuple[int,...]=()
    previous_fees_unknown:bool=False

    @property
    def original_fee_unknown(self)->bool:return self.state.original_fee is None

    @property
    def uncertain_fee_basis(self)->bool:return self.original_fee_unknown or self.previous_fees_unknown

    @property
    def minimum_price(self)->int|None:
        prices=(*self.previous_prices,*((self.state.original_fee.fee_cap,) if self.state.original_fee is not None else ()))
        if not prices:return None
        base=max(prices)
        return max(base+1,(base*(100+self.bump_percent)+99)//100)

    @property
    def gas_wei(self)->int:return GAS*self.gas_price

    def validate(self,now:datetime)->None:
        if type(self.state) is not WalletCancellationState:raise DexExecutorError('Revisa la cancelación exacta de la wallet')
        self.state.require_fresh(now)
        _integer(self.gas_price,'Precio de cancelación',10**12)
        _integer(self.bump_percent,'Aumento mínimo',1000)
        _integer(self.max_gas_wei,'Coste máximo',MAX_GAS_BUDGET)
        if type(self.accept_unknown_fee) is not bool or type(self.previous_fees_unknown) is not bool or type(self.previous_prices) is not tuple:
            raise DexExecutorError('Revisa la aceptación y tarifas anteriores')
        for price in self.previous_prices:_integer(price,'Tarifa anterior',10**12)
        if self.uncertain_fee_basis and not self.accept_unknown_fee:
            raise DexExecutorError('Acepta expresamente que la tarifa firmada original o anterior es desconocida')
        minimum=self.minimum_price
        if minimum is not None and self.gas_price<minimum:raise DexExecutorError('El precio no cubre el aumento sobre las tarifas conocidas')
        if self.gas_wei>self.max_gas_wei or self.gas_wei>self.state.snapshot.balance:
            raise DexExecutorError('El saldo o coste revisado no cubre la cancelación')

    def transaction(self)->dict[str,str]:
        self.validate(self.state.snapshot.observed_at)
        return {'from':self.state.snapshot.sender,'to':self.state.snapshot.sender,'value':'0x0','data':'0x','type':'0x0',
            'chainId':'0x38','nonce':hex(self.state.snapshot.nonce),'gas':hex(GAS),'gasPrice':hex(self.gas_price)}


def plan_wallet_cancellation(state:WalletCancellationState,*,gas_price:int,bump_percent:int,max_gas_wei:int,
        accept_unknown_fee:bool,now:datetime,previous_prices:tuple[int,...]=(),previous_fees_unknown:bool=False)->WalletCancellationPlan:
    result=WalletCancellationPlan(state,gas_price,bump_percent,max_gas_wei,accept_unknown_fee,previous_prices,previous_fees_unknown)
    result.validate(now);return result


def read_wallet_cancellation_state(rpc:Rpc,wallet:str,recipient:str,*,nonce:int,amount_wei:int,
        original_hash:str|None,now:datetime)->WalletCancellationState:
    wallet,recipient=_owner(wallet),_owner(recipient);_integer(nonce,'Nonce original',2**64-2,zero=True)
    _integer(amount_wei,'Importe original',MAX_GAS_BUDGET)
    tx_hash=word(original_hash) if original_hash is not None else None
    first=_pending_fee(rpc('eth_getTransactionByHash',[tx_hash]),wallet,recipient,nonce,amount_wei,tx_hash) if tx_hash else None
    state=read_eoa_replacement_state(rpc,wallet,recipient,nonce=nonce,now=now)
    repeated=_pending_fee(rpc('eth_getTransactionByHash',[tx_hash]),wallet,recipient,nonce,amount_wei,tx_hash) if tx_hash else None
    if first!=repeated:raise DexExecutorError('La financiación apareció o cambió durante la lectura; revisa de nuevo')
    result=WalletCancellationState(state,amount_wei,tx_hash,first);result.require_fresh(now);return result
