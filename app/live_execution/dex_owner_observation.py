"""Read signed owner receipts at a coherent finalized block. Never sends."""
from eth_abi import encode, decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from app.live_execution.dex_artifact import DexExecutorError, executor_address, FACTORY
from app.live_execution.evm_transaction import quantity, word
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_swap import TOKENS



def _pool(rpc, fee: int, tag: str) -> str:
    data = keccak(text='getPool(address,address,uint24)')[:4]+encode(
        ['address', 'address', 'uint24'], [TOKENS['USDT'], TOKENS['WBNB'], fee])
    raw = rpc('eth_call', [{'to': FACTORY, 'data': '0x'+data.hex()}, tag])
    if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith('0x'):
        raise ValueError('pool ABI')
    return canonical_wallet_address(decode(['address'], bytes.fromhex(raw[2:]), strict=True)[0])


def _same_block(rpc, block: dict, tag: str) -> None:
    repeated = rpc('eth_getBlockByNumber', [tag, False])
    if word(repeated['hash']) != word(block['hash']) or quantity(repeated['number']) != quantity(block['number']):
        raise ValueError('block changed')


def collect_owner_receipt(rpc, wallet: str, creation_nonce: int, fee: int, tx_hash: str) -> dict | None:
    """None means not finalized; only the journal verifier can accept evidence."""
    try:
        owner = canonical_wallet_address(wallet); tx_hash = word(tx_hash)
        if quantity(rpc('eth_chainId', [])) != 56:raise ValueError('chain')
        receipt = rpc('eth_getTransactionReceipt', [tx_hash])
        if receipt is None:return None
        if not isinstance(receipt, dict):raise ValueError('receipt')
        tag = hex(quantity(receipt['blockNumber']))
        finalized = rpc('eth_getBlockByNumber', ['finalized', False])
        if quantity(finalized['number']) < quantity(receipt['blockNumber']):return None
        block = rpc('eth_getBlockByNumber', [tag, False])
        tx = rpc('eth_getTransactionByHash', [tx_hash])
        runtime = rpc('eth_getCode', [executor_address(owner, creation_nonce), tag])
        pool = _pool(rpc, fee, tag)
        _same_block(rpc, block, tag)
        return {'chain_id': '0x38', 'tx_hash': tx_hash, 'transaction': tx, 'receipt': receipt,
                'block': block, 'finalized': finalized, 'pool': pool, 'runtime': runtime}
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, DecodingError):
        raise DexExecutorError('BNB no aportó un recibo verificable; conserva la operación original') from None


def collect_owner_code(rpc, wallet: str, creation_nonce: int, fee: int) -> dict | None:
    """Recover a lost deployment hash without claiming a receipt or a gas cost."""
    try:
        owner = canonical_wallet_address(wallet)
        if quantity(rpc('eth_chainId', [])) != 56:raise ValueError('chain')
        block = rpc('eth_getBlockByNumber', ['finalized', False])
        tag = hex(quantity(block['number']))
        executor = executor_address(owner, creation_nonce)
        nonce = rpc('eth_getTransactionCount', [owner, tag])
        runtime = rpc('eth_getCode', [executor, tag])
        if quantity(nonce) <= creation_nonce or runtime == '0x':return None
        pool = _pool(rpc, fee, tag)
        _same_block(rpc, block, tag)
        return {'chain_id': '0x38', 'block': block, 'pool': pool, 'runtime': runtime,
                'owner_nonce': nonce, 'contract_address': executor}
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, DecodingError):
        raise DexExecutorError('BNB no aportó el contrato esperado; no se repetirá el despliegue') from None
