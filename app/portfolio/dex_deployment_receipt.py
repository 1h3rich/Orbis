"""Verify signed CREATE and pinned runtime from a wallet RPC observation.

The transaction signature binds sender, calldata and fee caps. Receipt/finality
still depend on the selected RPC: this is not an independent consensus proof.
Only a sanitized observation is returned; no wallet, signature or tx hash is kept.
"""
from datetime import datetime, timedelta, timezone
import json
import re

from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError, creation_input, executor_address, expected_runtime, executor_artifact
from app.portfolio.wallet_proof import canonical_wallet_address


from app.live_execution.evm_transaction import quantity, word, recover_wallet_call


def _signed_creation(tx: dict) -> tuple[str, int, int, int, int | None]:
    return recover_wallet_call(tx, expected_to=None)


def finalized_block(block: dict, finalized: dict, *, created_at: datetime, now: datetime) -> tuple[int, str, datetime]:
    height, block_hash = quantity(block['number']), word(block['hash'])
    stamp, final_stamp = quantity(block['timestamp']), quantity(finalized['timestamp'])
    final_height, final_hash = quantity(finalized['number']), word(finalized['hash'])
    at = datetime.fromtimestamp(stamp, timezone.utc)
    if (height <= 0 or final_height < height or final_height == height and final_hash != block_hash
            or final_stamp < stamp or final_stamp > int(now.timestamp())+15
            or at < created_at-timedelta(seconds=15) or at > now+timedelta(seconds=15)):
        raise ValueError('finalized block or timestamp')
    return height, block_hash, at



def require_receipt_before_observation(receipt: dict, anchor: dict | None, *, require_success: bool = False) -> None:
    """Validate recovery chronology before an unreported hash can become durable."""
    if anchor is not None and (require_success and receipt['status'] != 'FINALIZED'
            or int(anchor['block_number']) < int(receipt['block_number'])
            or anchor['block_number'] == receipt['block_number'] and anchor['block_hash'] != receipt['block_hash']):
        raise DexExecutorError('El recibo contradice la observación anterior; conserva la operación original')


def observe_receipt(db: Session, row, terms: dict, state: dict, evidence: dict, now: datetime) -> dict:
    from app.portfolio.dex_deployments import _identity
    try:
        raw = json.dumps(evidence, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw) > 500_000 or set(evidence) != {'chain_id', 'tx_hash', 'transaction', 'receipt', 'block', 'finalized', 'pool', 'runtime'}:
            raise ValueError('evidence shape')
        tx_hash = word(evidence['tx_hash'])
        if evidence['chain_id'] != '0x38' or _identity(db, 'tx', tx_hash) != state['tx_ref']:
            raise ValueError('original hash')
        tx, receipt, block, finalized = [evidence[key] for key in ('transaction', 'receipt', 'block', 'finalized')]
        if not all(isinstance(item, dict) for item in (tx, receipt, block, finalized)):
            raise ValueError('pending transaction or finality unavailable')
        sender, tx_type, gas_limit, fee_cap, priority = _signed_creation(tx)
        if (_identity(db, 'wallet', sender) != row.wallet_digest or quantity(tx['nonce']) != int(terms['nonce'])
                or bytes.fromhex(tx['input'][2:]) != creation_input(sender, terms['fee'])
                or not 0 < gas_limit <= 10_000_000 or not 0 < fee_cap <= 10**12
                or gas_limit*fee_cap > int(terms['max_gas_wei'])):
            raise ValueError('deployment plan or maximum fee')
        height, block_hash, at = finalized_block(block, finalized, created_at=row.created_at, now=now)
        if (word(tx['hash']) != tx_hash or word(receipt['transactionHash']) != tx_hash
                or quantity(receipt['type']) != tx_type or canonical_wallet_address(receipt['from']) != sender
                or receipt.get('to') is not None or receipt['logs'] != []
                or any(quantity(item['blockNumber']) != height or word(item['blockHash']) != block_hash for item in (tx, receipt))
                or quantity(tx['transactionIndex']) != quantity(receipt['transactionIndex'])):
            raise ValueError('receipt linkage')
        gas_used, price, succeeded = quantity(receipt['gasUsed']), quantity(receipt['effectiveGasPrice']), quantity(receipt['status'])
        base_fee = quantity(block['baseFeePerGas']) if tx_type == 2 else None
        expected_price = min(fee_cap, base_fee+priority) if tx_type == 2 else fee_cap
        minimum_gas = 53_000+(200*len(executor_artifact().runtime) if succeeded == 1 else 0)
        if (not minimum_gas <= gas_used <= gas_limit or price != expected_price or succeeded not in (0, 1)
                or tx_type == 2 and fee_cap < base_fee):
            raise ValueError('gas accounting')
        deployed = executor_address(sender, int(terms['nonce']))
        if succeeded:
            if (canonical_wallet_address(receipt['contractAddress']) != deployed
                    or evidence['runtime'].lower() != expected_runtime(sender, evidence['pool'], terms['fee'])):
                raise ValueError('deployed program or immutable owner/pool')
        elif evidence['runtime'] != '0x' or receipt['contractAddress'] is not None and canonical_wallet_address(receipt['contractAddress']) != deployed:
            raise ValueError('reverted deployment has code')
        result = {'status': 'FINALIZED' if succeeded else 'REVERTED', 'source': 'WALLET_RPC_OBSERVED',
            'block_number': str(height), 'block_hash': block_hash, 'block_at': at.isoformat(),
            'transaction_type': tx_type, 'gas_limit': str(gas_limit), 'gas_used': str(gas_used),
            'fee_cap': str(fee_cap), 'priority_fee': str(priority) if priority is not None else None,
            'base_fee': str(base_fee) if base_fee is not None else None, 'effective_gas_price': str(price),
            'gas_fee_wei': str(gas_used*price), 'observation_ref': _identity(db, 'receipt', raw)}
        require_receipt_before_observation(result, state['code_observation'], require_success=True)
        return result
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, BadSignature, ValidationError):
        raise DexExecutorError('La prueba de despliegue no coincide con el plan; revisión necesaria, sin reenvío') from None


def validate_archived_receipt(receipt: dict, terms: dict, row, *, creation: bool = True) -> None:
    """Replay archived financial and chronology invariants during reads/backups."""
    from app.portfolio.wallet_funding import _uint, _time
    if (not isinstance(receipt, dict) or set(receipt) != {'status', 'source', 'block_number', 'block_hash', 'block_at',
            'transaction_type', 'gas_limit', 'gas_used', 'fee_cap', 'priority_fee', 'base_fee', 'effective_gas_price', 'gas_fee_wei', 'observation_ref'}
            or receipt['status'] != row.status or receipt['source'] != 'WALLET_RPC_OBSERVED'
            or type(receipt['transaction_type']) is not int or receipt['transaction_type'] not in (0, 2)
            or not re.fullmatch(r'[0-9a-f]{64}', receipt['observation_ref'])):
        raise ValueError('archived receipt')
    _uint(receipt['block_number'], 'Bloque'); word(receipt['block_hash'])
    gas_limit = _uint(receipt['gas_limit'], 'Gas máximo', maximum=10_000_000 if creation else 2_000_000)
    gas_used = _uint(receipt['gas_used'], 'Gas usado', maximum=gas_limit)
    fee_cap = _uint(receipt['fee_cap'], 'Fee cap', maximum=10**12)
    price = _uint(receipt['effective_gas_price'], 'Precio gas')
    if receipt['transaction_type'] == 2:
        priority = _uint(receipt['priority_fee'], 'Priority', maximum=fee_cap, positive=False)
        base_fee = _uint(receipt['base_fee'], 'Base fee', maximum=fee_cap, positive=False)
        expected_price = min(fee_cap, base_fee+priority)
    else:
        if receipt['priority_fee'] is not None or receipt['base_fee'] is not None:
            raise ValueError('legacy gas')
        expected_price = fee_cap
    stamp = _time(datetime.fromisoformat(receipt['block_at']))
    minimum_gas = (53_000+(200*len(executor_artifact().runtime) if row.status == 'FINALIZED' else 0)) if creation else 21_000
    if (gas_used < minimum_gas or price != expected_price or receipt['gas_fee_wei'] != str(gas_used*price)
            or gas_limit*fee_cap > int(terms['max_gas_wei']) or stamp.isoformat() != receipt['block_at']
            or stamp < row.created_at-timedelta(seconds=15) or stamp > row.updated_at+timedelta(seconds=15)):
        raise ValueError('archived gas accounting or clock')


def observe_code(db: Session, row, terms: dict, wallet: str, evidence: dict, now: datetime) -> dict:
    from app.portfolio.dex_deployments import _identity
    try:
        raw = json.dumps(evidence, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw) > 250_000 or set(evidence) != {'chain_id', 'block', 'pool', 'runtime', 'owner_nonce', 'contract_address'}:
            raise ValueError('code evidence shape')
        height, block_hash, at = finalized_block(evidence['block'], evidence['block'], created_at=row.created_at, now=now)
        nonce = quantity(evidence['owner_nonce'])
        if (evidence['chain_id'] != '0x38' or not int(terms['nonce']) < nonce <= 2**64-1
                or canonical_wallet_address(evidence['contract_address']) != executor_address(wallet, int(terms['nonce']))
                or evidence['runtime'].lower() != expected_runtime(wallet, evidence['pool'], terms['fee'])):
            raise ValueError('code or CREATE nonce')
        return {'source': 'WALLET_RPC_CODE_OBSERVED', 'block_number': str(height), 'block_hash': block_hash,
            'block_at': at.isoformat(), 'owner_nonce': str(nonce), 'observation_ref': _identity(db, 'code', raw)}
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('No se ha verificado el contrato esperado; no se repetirá el despliegue') from None


def validate_archived_code(observation: dict, terms: dict, row, observed_at: datetime) -> None:
    from app.portfolio.wallet_funding import _uint, _time
    if (not isinstance(observation, dict) or set(observation) != {'source', 'block_number', 'block_hash', 'block_at', 'owner_nonce', 'observation_ref'}
            or observation['source'] != 'WALLET_RPC_CODE_OBSERVED'
            or not re.fullmatch(r'[0-9a-f]{64}', observation['observation_ref'])):
        raise ValueError('archived code observation')
    _uint(observation['block_number'], 'Bloque'); word(observation['block_hash'])
    nonce = _uint(observation['owner_nonce'], 'Nonce', maximum=2**64-1)
    stamp = _time(datetime.fromisoformat(observation['block_at']))
    if (nonce <= int(terms['nonce']) or stamp.isoformat() != observation['block_at']
            or stamp < row.created_at-timedelta(seconds=15) or stamp > observed_at+timedelta(seconds=15)):
        raise ValueError('archived code chronology')
