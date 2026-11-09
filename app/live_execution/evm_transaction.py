"""Canonical BNB EIP-155/1559 wallet calls; public recovery, never signing."""
import re
from eth_account import Account
from eth_keys.constants import SECPK1_N
from eth_utils import keccak
import rlp
from app.portfolio.wallet_proof import canonical_wallet_address


def quantity(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r'0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})', value):
        raise ValueError('RPC quantity')
    return int(value, 16)


def word(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        raise ValueError('RPC word')
    return value.lower()


def _serial(value: int) -> bytes:
    return value.to_bytes((value.bit_length()+7)//8, 'big')


def recover_wallet_call(tx: dict, *, expected_to: str | None) -> tuple[str, int, int, int, int | None]:
    """Recover an exact zero-value call; native payments use a distinct gate."""
    return _recover_wallet_transaction(tx,expected_to=expected_to,expected_value=0,native=False)


def recover_wallet_cancellation(tx:dict,*,expected_owner:str)->tuple[str,int,int,int,int|None]:
    """A signed empty zero-value self-call only; never an arbitrary recipient."""
    owner=canonical_wallet_address(expected_owner)
    result=_recover_wallet_transaction(tx,expected_to=owner,expected_value=0,native=True)
    if result[0]!=owner:raise ValueError('cancellation sender')
    return result


def recover_wallet_transfer(tx:dict,*,expected_to:str,expected_value:int)->tuple[str,int,int,int,int|None]:
    """Native BNB only: positive pinned value, explicit recipient, empty calldata."""
    if type(expected_value) is not int or not 0<expected_value<2**256:
        raise ValueError('native transfer value')
    return _recover_wallet_transaction(tx,expected_to=canonical_wallet_address(expected_to),expected_value=expected_value,native=True)


def recover_reverted_call(tx:dict,*,receipt_status:int)->tuple[str,int,int,int,int|None]:
    """Read-only signature recovery for a failed call; never a signing gate.

    Its caller must independently bind the failed receipt/block/hash/logs. No
    successful unknown call can use this entry point to claim economic effects.
    """
    if type(receipt_status) is not int or receipt_status!=0:raise ValueError('failed receipt required')
    target=None if tx.get('to') is None else canonical_wallet_address(tx['to'])
    return _recover_wallet_transaction(tx,expected_to=target,expected_value=quantity(tx['value']),native=False,allow_empty_data=True)


def _recover_wallet_transaction(tx:dict,*,expected_to:str|None,expected_value:int,native:bool,allow_empty_data:bool=False)->tuple[str,int,int,int,int|None]:
    """Rebuild signed bytes; never trusts the sender/hash supplied by an RPC."""
    tx_type = quantity(tx['type'])
    # Legacy RPC responses may omit chainId; EIP-155 v below authenticates it.
    chain = quantity(tx['chainId']) if 'chainId' in tx else (56 if tx_type == 0 else None)
    nonce, gas, value = [quantity(tx[key]) for key in ('nonce', 'gas', 'value')]
    data = tx['input']
    if (chain != 56 or (tx.get('to') is not None if expected_to is None else canonical_wallet_address(tx.get('to')) != expected_to) or value != expected_value or not isinstance(data, str)
            or (data!='0x' if native else not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})'+('{0,50000}' if allow_empty_data else '{1,50000}'),data))):
        raise ValueError('wallet call')
    target = b'' if expected_to is None else bytes.fromhex(expected_to[2:])
    signature = []
    for key in ('r', 's'):
        item = tx[key]
        if not isinstance(item, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', item):
            raise ValueError('signature scalar')
        signature.append(int(item, 16))
    r, s = signature
    if not 0 < r < SECPK1_N or not 0 < s <= SECPK1_N//2:
        raise ValueError('noncanonical signature')
    if tx_type == 0:
        price, v = quantity(tx['gasPrice']), quantity(tx['v'])
        if v not in (56*2+35, 56*2+36):
            raise ValueError('unprotected or different chain signature')
        raw = rlp.encode([_serial(nonce), _serial(price), _serial(gas), target, _serial(value), bytes.fromhex(data[2:]),
                          _serial(v), _serial(r), _serial(s)])
        priority = None
    elif tx_type == 2:
        price, priority = quantity(tx['maxFeePerGas']), quantity(tx['maxPriorityFeePerGas'])
        parity = quantity(tx['yParity'] if 'yParity' in tx else tx['v'])
        if 'v' in tx and quantity(tx['v']) != parity:
            raise ValueError('inconsistent signature parity')
        if parity not in (0, 1) or tx.get('accessList') != [] or priority > price:
            raise ValueError('unsupported access list or priority fee')
        raw = b'\x02'+rlp.encode([_serial(chain), _serial(nonce), _serial(priority), _serial(price), _serial(gas),
            target, _serial(value), bytes.fromhex(data[2:]), [], _serial(parity), _serial(r), _serial(s)])
    else:
        raise ValueError('unsupported transaction type')
    sender = canonical_wallet_address(Account.recover_transaction(raw))
    if sender != canonical_wallet_address(tx['from']) or '0x'+keccak(raw).hex() != word(tx['hash']):
        raise ValueError('signed transaction hash or sender')
    return sender, tx_type, gas, price, priority

