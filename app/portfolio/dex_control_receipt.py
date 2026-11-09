"""Signed owner control + exact pinned-program event, without inferring a bot."""
from datetime import datetime
import json

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_keys.exceptions import BadSignature
from eth_utils import keccak, ValidationError

from app.live_execution.dex_artifact import DexExecutorError, expected_runtime, executor_address
from app.live_execution.dex_policy import POLICY_ABI
from app.live_execution.evm_transaction import recover_wallet_call, quantity, word
from app.portfolio.dex_deployments import deployment_record
from app.portfolio.dex_deployment_receipt import finalized_block, validate_archived_receipt, require_receipt_before_observation
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_funding import _uint
from app.portfolio.wallet_swap import TOKENS


CONFIGURED = '0x'+keccak(text='PolicyConfigured(uint256,address,'+POLICY_ABI+')').hex()
STOPPED = '0x'+keccak(text='Stopped(uint256,uint256,uint256)').hex()
APPROVAL = '0x'+keccak(text='Approval(address,address,uint256)').hex()
EVENT_FIELDS = frozenset({'action', 'new_policy_id', 'operator_ref', 'owned_wbnb', 'cost_usdt'})


def observe_control(db, row, terms: dict, state: dict, evidence: dict, now: datetime) -> dict:
    from app.portfolio.dex_controls import _identity, _compile
    try:
        raw = json.dumps(evidence, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw) > 500_000 or set(evidence) != {'chain_id', 'tx_hash', 'transaction', 'receipt', 'block', 'finalized', 'pool', 'runtime'}:
            raise ValueError('shape')
        tx_hash = word(evidence['tx_hash'])
        if evidence['chain_id'] != '0x38' or _identity(db, 'tx', tx_hash) != state['tx_ref']:
            raise ValueError('original hash')
        tx, receipt, block, finalized = [evidence[key] for key in ('transaction', 'receipt', 'block', 'finalized')]
        if not all(isinstance(item, dict) for item in (tx, receipt, block, finalized)):
            raise ValueError('pending transaction or finality')
        owner = canonical_wallet_address(tx['from'])
        if _identity(db, 'wallet', owner) != row.wallet_digest:raise ValueError('owner')
        executor = executor_address(owner, int(terms['creation_nonce']))
        is_allowance = terms['action'] in {'APPROVE', 'REVOKE'}
        target = TOKENS[terms['allowance']['token']] if is_allowance else executor
        sender, tx_type, gas_limit, fee_cap, priority = recover_wallet_call(tx, expected_to=target)
        operator = None
        if terms['action'] == 'CONFIGURE':
            reviewed = decode(['uint256', POLICY_ABI], bytes.fromhex(tx['input'][10:]), strict=True)
            operator = canonical_wallet_address(reviewed[1][0])
            if _identity(db, 'operator', operator) != terms['operator_ref']:raise ValueError('operator')
        expected = _compile(terms, owner, operator, now=row.created_at)
        if (quantity(tx['nonce']) != int(terms['nonce']) or tx['input'].lower() != expected['data']
                or not 0 < gas_limit <= 2_000_000 or not 0 < fee_cap <= 10**12
                or gas_limit*fee_cap > int(terms['max_gas_wei'])):raise ValueError('signed call or fee cap')
        height, block_hash, at = finalized_block(block, finalized, created_at=row.created_at, now=now)
        if (word(tx['hash']) != tx_hash or word(receipt['transactionHash']) != tx_hash
                or quantity(receipt['type']) != tx_type or canonical_wallet_address(receipt['from']) != owner
                or canonical_wallet_address(receipt['to']) != target or receipt['contractAddress'] is not None
                or any(quantity(item['blockNumber']) != height or word(item['blockHash']) != block_hash for item in (tx, receipt))
                or quantity(receipt['transactionIndex']) != quantity(tx['transactionIndex'])):raise ValueError('receipt linkage')
        _, deployment, _ = deployment_record(db, row.deployment_id)
        if evidence['runtime'].lower() != expected_runtime(owner, evidence['pool'], deployment['fee']):raise ValueError('program')
        gas_used, price, success = quantity(receipt['gasUsed']), quantity(receipt['effectiveGasPrice']), quantity(receipt['status'])
        base_fee = quantity(block['baseFeePerGas']) if tx_type == 2 else None
        expected_price = min(fee_cap, base_fee+priority) if tx_type == 2 else fee_cap
        if (not 21_000 <= gas_used <= gas_limit or price != expected_price or success not in (0, 1)
                or tx_type == 2 and fee_cap < base_fee):raise ValueError('gas')
        logs = receipt['logs']
        new_id, operator_ref, owned, cost = None, None, None, None
        approved_units = None
        if not isinstance(logs, list) or len(logs) != (1 if success else 0):raise ValueError('event count')
        if success:
            event = logs[0]
            quantity(event['logIndex'])
            if (canonical_wallet_address(event['address']) != target or event['removed'] is not False
                    or word(event['transactionHash']) != tx_hash or quantity(event['transactionIndex']) != quantity(tx['transactionIndex'])
                    or quantity(event['blockNumber']) != height or word(event['blockHash']) != block_hash):raise ValueError('event linkage')
            topics = [word(topic) for topic in event['topics']]
            if is_allowance:
                approved_units = int(word(event['data']), 16)
                if (topics != [APPROVAL, '0x'+int(owner, 16).to_bytes(32, 'big').hex(),
                        '0x'+int(executor, 16).to_bytes(32, 'big').hex()]
                        or approved_units != int(terms['allowance']['units'])):raise ValueError('approval event')
            elif terms['action'] == 'CONFIGURE':
                values = decode([POLICY_ABI], bytes.fromhex(event['data'][2:]), strict=True)[0]
                expected_values = decode(['uint256', POLICY_ABI], bytes.fromhex(expected['data'][10:]), strict=True)[1]
                new_id = int(terms['anchor']['counters']['policy_id'])+1
                if (values != expected_values or topics != [CONFIGURED, '0x'+new_id.to_bytes(32, 'big').hex(),
                        '0x'+int(operator, 16).to_bytes(32, 'big').hex()]):raise ValueError('configured policy event')
                operator_ref = terms['operator_ref']
            else:
                if len(topics) != 2 or topics[0] != STOPPED:raise ValueError('STOP event')
                new_id = int(topics[1], 16)
                if new_id <= int(terms['anchor']['counters']['policy_id']):raise ValueError('STOP revision')
                owned, cost = decode(['uint256', 'uint256'], bytes.fromhex(event['data'][2:]), strict=True)
        result = {'status': 'FINALIZED' if success else 'REVERTED', 'source': 'WALLET_RPC_OBSERVED',
            'block_number': str(height), 'block_hash': block_hash, 'block_at': at.isoformat(), 'transaction_type': tx_type,
            'gas_limit': str(gas_limit), 'gas_used': str(gas_used), 'fee_cap': str(fee_cap),
            'priority_fee': str(priority) if priority is not None else None, 'base_fee': str(base_fee) if base_fee is not None else None,
            'effective_gas_price': str(price), 'gas_fee_wei': str(gas_used*price), 'observation_ref': _identity(db, 'receipt', raw),
            'action': terms['action'], 'new_policy_id': str(new_id) if new_id is not None else None,
            'operator_ref': operator_ref, 'owned_wbnb': str(owned) if owned is not None else None,
            'cost_usdt': str(cost) if cost is not None else None}
        if is_allowance:result['approved_units'] = str(approved_units) if approved_units is not None else None
        observation = state['nonce_observation']
        require_receipt_before_observation(result, observation['anchor'] if observation else None)
        return result
    except DexExecutorError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, DecodingError, BadSignature, ValidationError):
        raise DexExecutorError('El recibo de control DEX no coincide con la revisión; no se repetirá el envío') from None


def validate_control_receipt(receipt: dict, terms: dict, row) -> None:
    is_allowance = terms['action'] in {'APPROVE', 'REVOKE'}
    event_fields = EVENT_FIELDS | ({'approved_units'} if is_allowance else set())
    if not isinstance(receipt, dict) or not event_fields <= receipt.keys():raise ValueError('control receipt')
    validate_archived_receipt({key: value for key, value in receipt.items() if key not in event_fields}, terms, row, creation=False)
    if receipt['action'] != terms['action']:raise ValueError('action')
    if row.status == 'REVERTED':
        if any(receipt[key] is not None for key in event_fields-{'action'}):raise ValueError('reverted event')
    elif is_allowance:
        if (receipt['approved_units'] != terms['allowance']['units']
                or any(receipt[key] is not None for key in EVENT_FIELDS-{'action'})):raise ValueError('approval event')
    else:
        policy_id = _uint(receipt['new_policy_id'], 'Policy ID')
        if terms['action'] == 'CONFIGURE':
            if (policy_id != int(terms['anchor']['counters']['policy_id'])+1 or receipt['operator_ref'] != terms['operator_ref']
                    or receipt['owned_wbnb'] is not None or receipt['cost_usdt'] is not None):raise ValueError('configured event')
        elif (policy_id <= int(terms['anchor']['counters']['policy_id']) or receipt['operator_ref'] is not None
                or (_uint(receipt['owned_wbnb'], 'WBNB', positive=False) == 0) != (_uint(receipt['cost_usdt'], 'Coste', positive=False) == 0)):
            raise ValueError('STOP event')
