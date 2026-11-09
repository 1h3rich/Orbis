"""Find the finalized transaction for an existing BNB sender/nonce."""
from dataclasses import dataclass
import re

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from app.live_execution.dex_artifact import DexExecutorError, executor_address
from app.live_execution.dex_policy import POLICY_ABI
from app.live_execution.dex_state import Rpc
from app.live_execution.evm_transaction import quantity, word
from app.portfolio.wallet_proof import canonical_wallet_address


MAX_SEARCH_BLOCKS = 2**18
MAX_BLOCK_TRANSACTIONS = 10_000


@dataclass(frozen=True, slots=True, repr=False)
class FinalizedNonceMatch:
    tx_hash: str
    block_hash: str
    block_number: int
    index: int

    def check_receipt(self, evidence: dict) -> None:
        try:
            receipt = evidence['receipt']
            if (word(receipt['transactionHash']) != self.tx_hash or word(receipt['blockHash']) != self.block_hash
                    or quantity(receipt['blockNumber']) != self.block_number
                    or quantity(receipt['transactionIndex']) != self.index):
                raise ValueError('receipt changed')
        except (KeyError, TypeError, ValueError):
            raise DexExecutorError('El recibo BNB no coincide con el bloque localizado') from None


def pinned_block(rpc: Rpc, height: int, block_hash: str) -> dict:
    block = rpc('eth_getBlockByNumber', [hex(height), False])
    if quantity(block['number']) != height or word(block['hash']) != word(block_hash):
        raise ValueError('pinned block changed')
    return block


def read_granted_operator(rpc: Rpc, wallet: str, creation_nonce: int, grant_height: int, grant_hash: str) -> str:
    """Recover the public operator from its pinned original grant."""
    try:
        if quantity(rpc('eth_chainId', [])) != 56:
            raise ValueError('chain')
        pinned_block(rpc, grant_height, grant_hash)
        data = '0x'+keccak(text='policy()')[:4].hex()
        raw = rpc('eth_call', [{'to':executor_address(wallet,creation_nonce),'data':data},hex(grant_height)])
        if type(raw) is not str or not re.fullmatch(r'0x[0-9a-fA-F]{960}',raw):
            raise ValueError('historical policy ABI')
        operator = canonical_wallet_address(decode([POLICY_ABI],bytes.fromhex(raw[2:]),strict=True)[0][0])
        if operator in (wallet,'0x'+'0'*40):
            raise ValueError('grant operator')
        pinned_block(rpc, grant_height, grant_hash)
        return operator
    except (KeyError, TypeError, ValueError, DecodingError):
        raise DexExecutorError('No se pudo leer la operadora en su concesión original. Puedes revisar un hash.') from None


def find_finalized_nonce(rpc: Rpc, sender: str, nonce: int, *, anchor_height: int | None = None,
                         anchor_hash: str | None = None) -> FinalizedNonceMatch | None:
    """Use historical nonce counts to locate the first block after consumption."""
    try:
        sender = canonical_wallet_address(sender)
        if (type(nonce) is not int or not 0 <= nonce < 2**64-1
                or (anchor_height is None) != (anchor_hash is None)
                or anchor_height is not None and (type(anchor_height) is not int or not 0 < anchor_height < 2**64)
                or quantity(rpc('eth_chainId', [])) != 56):
            raise ValueError('nonce search scope')
        final = rpc('eth_getBlockByNumber', ['finalized', False])
        final_height, final_hash = quantity(final['number']), word(final['hash'])
        if not 0 < final_height < 2**64:
            raise ValueError('invalid final block')
        def count(height: int) -> int:
            result = quantity(rpc('eth_getTransactionCount', [sender, hex(height)]))
            if result >= 2**64:
                raise ValueError('invalid account nonce')
            return result

        if anchor_height is None:
            high, high_count = final_height, count(final_height)
            if high_count <= nonce:
                pinned_block(rpc, final_height, final_hash)
                return None
            # Old wallet plans have no stored block. Widen from the finalized
            # head so a recent receipt doesn't require deep historical state.
            floor = max(1,final_height-MAX_SEARCH_BLOCKS)
            distance = 1
            while True:
                low = max(floor,final_height-distance)
                low_count = count(low)
                if low_count > high_count:
                    raise ValueError('account nonce regressed')
                if low_count <= nonce:
                    break
                if low == floor:
                    raise DexExecutorError('El nonce no aparece en el tramo consultado. Puedes revisar un hash.')
                high, high_count = low, low_count
                distance *= 2
        else:
            if final_height < anchor_height:
                return None
            pinned_block(rpc, anchor_height, anchor_hash)
            low, high = anchor_height, min(final_height, anchor_height+MAX_SEARCH_BLOCKS)
            low_count = count(low)
            if low_count > nonce:
                raise ValueError('nonce already consumed at original anchor')
            high_count = count(high) if high > low else low_count
            if high_count < low_count:
                raise ValueError('account nonce regressed')
            if high_count <= nonce:
                pinned_block(rpc, final_height, final_hash)
                if high < final_height:
                    raise DexExecutorError('El nonce no aparece en el tramo consultado. Puedes revisar un hash.')
                return None
        while high-low > 1:
            mid = (low+high)//2
            mid_count = count(mid)
            if not low_count <= mid_count <= high_count:
                raise ValueError('account nonce regressed')
            if mid_count > nonce:
                high, high_count = mid, mid_count
            else:
                low, low_count = mid, mid_count

        block = rpc('eth_getBlockByNumber', [hex(high), True])
        block_hash = word(block['hash'])
        transactions = block['transactions']
        if (quantity(block['number']) != high or type(transactions) is not list
                or len(transactions) > MAX_BLOCK_TRANSACTIONS):
            raise ValueError('invalid transaction block')
        match = None
        for index, tx in enumerate(transactions):
            if canonical_wallet_address(tx['from']) != sender or quantity(tx['nonce']) != nonce:
                continue
            if (match is not None or word(tx['blockHash']) != block_hash
                    or quantity(tx['blockNumber']) != high or quantity(tx['transactionIndex']) != index):
                raise ValueError('ambiguous nonce match')
            match = FinalizedNonceMatch(word(tx['hash']), block_hash, high, index)
        if match is None:
            raise ValueError('nonce count changed without a matching transaction')
        if high == final_height:
            if block_hash != final_hash:
                raise ValueError('final block changed')
        else:
            pinned_block(rpc, high, block_hash)
        pinned_block(rpc, final_height, final_hash)
        return match
    except DexExecutorError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DexExecutorError('BNB no devolvió un bloque verificable para este nonce. Puedes revisar un hash.') from None
