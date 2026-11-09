"""Bounded BNB operator RPC: one exact send, plus public receipt reads.

No retries, caller URLs, keys, arbitrary signing or raw provider error logging.
This adapter is internal; bot activation still requires its LAB/PAPER binding.
"""
from datetime import datetime,timezone

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from app.live_execution.dex_state import BnbReadClient
from app.live_execution.dex_operator import DexSignature, verify_operator_signature
from app.live_execution.dex_trade import DexTradePlan,DexStopPlan
from app.live_execution.dex_artifact import DexExecutorError,executor_address
from app.live_execution.evm_transaction import quantity,word
from app.portfolio.wallet_proof import canonical_wallet_address


class BnbOperatorReadClient(BnbReadClient):
    METHODS = BnbReadClient.METHODS | {'eth_getTransactionByHash','eth_getTransactionReceipt'}


class BnbNonceReadClient(BnbReadClient):
    """A bounded block search for records that predate persisted anchors."""
    MAX_QUERIES = 44


class _SenderRpc(BnbReadClient):
    METHODS = BnbReadClient.METHODS | {'eth_sendRawTransaction'}


class BnbOperatorSender:
    def __init__(self, plan: DexTradePlan | DexStopPlan, *, transport=None, clock=None):
        if type(plan) not in (DexTradePlan,DexStopPlan):raise DexExecutorError('El emisor requiere un plan exacto del operador')
        self._plan = plan
        self._clock = clock or (lambda:datetime.now(timezone.utc))
        self._rpc = _SenderRpc(transport=transport)
        self._used = False
        self._hash: str | None = None

    def __enter__(self):return self
    def __exit__(self,*args):self._rpc.__exit__(*args)

    @property
    def transaction_hash(self) -> str | None:
        """Transient original hash for immediate receipt lookup, never stored."""
        return self._hash

    def send(self, raw_transaction: bytes) -> str:
        if self._used:raise DexExecutorError('El emisor ya consumió su único intento')
        self._used = True
        if not isinstance(raw_transaction,bytes):raise DexExecutorError('Firma del operador no válida')
        self._plan.validate(self._clock())
        signed = DexSignature(raw_transaction,'0x'+keccak(raw_transaction).hex(),0)
        verify_operator_signature(self._plan,signed)
        self._hash = signed.tx_hash
        if quantity(self._rpc('eth_chainId',[])) != 56:raise DexExecutorError('El RPC no confirmó BNB Chain')
        # The durable outbox must mark its single attempt before invoking send.
        # The HTTP client performs no retry or redirect and masks remote errors.
        reported = word(self._rpc('eth_sendRawTransaction',['0x'+raw_transaction.hex()]))
        if reported != self._hash:raise DexExecutorError('El RPC no devolvió el hash original; coteja antes de continuar')
        return reported


def collect_operator_receipt(rpc, wallet: str, creation_nonce: int, tx_hash: str) -> dict | None:
    """Fetch evidence, not a fill. Only the journal verifier may accept it."""
    owner = canonical_wallet_address(wallet);tx_hash = word(tx_hash)
    executor = executor_address(owner,creation_nonce)
    try:
        if quantity(rpc('eth_chainId',[])) != 56:raise ValueError('chain')
        tx = rpc('eth_getTransactionByHash',[tx_hash])
        receipt = rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt is None:return None
        if not isinstance(tx,dict) or not isinstance(receipt,dict):raise ValueError('receipt/transaction')
        tag = hex(quantity(receipt['blockNumber']))
        block = rpc('eth_getBlockByNumber',[tag,False])
        finalized = rpc('eth_getBlockByNumber',['finalized',False])
        raw = rpc('eth_call',[{'to':executor,'data':'0x'+keccak(text='pool()')[:4].hex()},tag])
        if not isinstance(raw,str) or not raw.startswith('0x') or len(raw) != 66:raise ValueError('pool ABI')
        pool = canonical_wallet_address(decode(['address'],bytes.fromhex(raw[2:]),strict=True)[0])
        runtime = rpc('eth_getCode',[executor,tag])
        repeated = rpc('eth_getBlockByNumber',[tag,False])
        if word(repeated['hash']) != word(block['hash']) or quantity(repeated['number']) != quantity(block['number']):raise ValueError('block changed')
        return {'chain_id':'0x38','wallet_address':owner,'tx_hash':tx_hash,'transaction':tx,'receipt':receipt,
                'block':block,'finalized':finalized,'pool':pool,'runtime':runtime}
    except DexExecutorError:
        raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,DecodingError):
        raise DexExecutorError('El RPC no aportó un recibo del operador verificable') from None
