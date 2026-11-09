"""Observe an unknown successful signed call without resolving or booking it.

Known-call/cancellation readers retain their published semantics. This profile
contains normalized facts and keyed references only; it grants no authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hmac
import hashlib
import json
import re
from typing import Callable
from types import MappingProxyType
from uuid import UUID

from eth_abi.exceptions import DecodingError
from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError, keccak

from app.live_execution.dex_artifact import DexExecutorError, executor_address
from app.live_execution.dex_call_resolution import CallSource
from app.live_execution.evm_transaction import _recover_wallet_transaction, quantity, word
from app.portfolio.dex_deployment_receipt import finalized_block, require_receipt_before_observation
from app.portfolio.wallet_funding import _encoded, _time
from app.portfolio.wallet_proof import canonical_wallet_address

SCHEMA = 'orbis.dex.external.observation.v1'
TOKEN_REGISTRY = 'orbis.bnb.external.tokens.v1'
# Frozen wire semantics. Expanding/changing asset identity needs another
# registry version, rather than reinterpreting an archived token symbol.
TOKENS = MappingProxyType({'USDT': '0x55d398326f99059ff775485246999027b3197955',
                          'WBNB': '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'})
MAX_LOGS = 2048
MAX_BYTES = 750000
TRANSFER = '0x'+keccak(text='Transfer(address,address,uint256)').hex()
APPROVAL = '0x'+keccak(text='Approval(address,address,uint256)').hex()
ZERO = '0x'+'00'*20
REF = re.compile(r'[0-9a-f]{64}')
Reference = Callable[[str, str], str]
ROLES = {'OWNER', 'SIGNER', 'OTHER', 'ZERO'}


def source_reference(kind: str, salt: bytes) -> Reference:
    """Detached original reference domains; captured salt is never spending authority.

    The caller owns this short-lived secret and must still enforce vault
    generation/revocation before returning or storing any observation.
    """
    if kind not in ('WALLET_SWAP', 'OPERATOR') or type(salt) is not bytes or len(salt) != 32:
        raise ValueError('source reference material')
    def reference(label: str, value: str) -> str:
        prefix = 'wallet-swap-v1' if kind == 'WALLET_SWAP' else 'dex-deployment-v1' if label == 'wallet' else 'dex-operator-v1'
        return hmac.new(salt, f'{prefix}:{label}:0x38:{value.lower()}'.encode(), hashlib.sha256).hexdigest()
    return reference


def _ref(reference: Reference, label: str, value: object) -> str:
    result = reference('external-observation-v1:'+label, _encoded(value)[0])
    if not isinstance(result, str) or REF.fullmatch(result) is None:
        raise ValueError('keyed reference')
    return result


def _data(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r'0x(?:[0-9a-fA-F]{2}){0,50000}', value) is None:
        raise ValueError('RPC data')
    return value.lower()


def _uint(value: object, maximum: int = 2**256-1) -> int:
    if not isinstance(value, str) or re.fullmatch(r'0|[1-9][0-9]{0,77}', value) is None:
        raise ValueError('canonical unsigned integer')
    parsed = int(value)
    if parsed > maximum:
        raise ValueError('integer bound')
    return parsed


def _address_topic(value: str) -> str:
    value = word(value)
    if value[2:26] != '0'*24:
        raise ValueError('noncanonical indexed address')
    return canonical_wallet_address('0x'+value[-40:])


def _role(address: str, owner: str, sender: str) -> str:
    return 'ZERO' if address == ZERO else 'OWNER' if address == owner else 'SIGNER' if address == sender else 'OTHER'


def _logs(logs: object, *, owner: str, sender: str, height: int, block_hash: str,
          tx_hash: str, index: int, reference: Reference) -> tuple[list[dict], list[dict], list[dict]]:
    if not isinstance(logs, list) or len(logs) > MAX_LOGS:
        raise ValueError('receipt log limit')
    tokens = {canonical_wallet_address(address): symbol for symbol, address in TOKENS.items()}
    transfers: list[dict] = []; approvals: list[dict] = []; remaining: list[dict] = []
    previous = -1
    for log in logs:
        if not isinstance(log, dict):
            raise ValueError('receipt log')
        at = quantity(log['logIndex']); topics = log['topics']
        if (at <= previous or at >= 100000 or 'removed' in log and log['removed'] is not False
                or quantity(log['blockNumber']) != height or word(log['blockHash']) != block_hash
                or quantity(log['transactionIndex']) != index or word(log['transactionHash']) != tx_hash
                or not isinstance(topics, list) or len(topics) > 4):
            raise ValueError('log origin/order')
        previous = at
        emitter = canonical_wallet_address(log['address'])
        topics = [word(topic) for topic in topics]; data = _data(log['data'])
        canonical = {'emitter': emitter, 'topics': topics, 'data': data, 'index': str(at),
                     'block_number': str(height), 'block_hash': block_hash, 'transaction_index': str(index), 'tx_hash': tx_hash}
        log_ref = _ref(reference, 'log', canonical)
        if emitter in tokens and topics and topics[0] in (TRANSFER, APPROVAL):
            if len(topics) != 3 or len(data) != 66:
                raise ValueError('malformed known token event')
            left = _address_topic(topics[1]); right = _address_topic(topics[2])
            left_role = _role(left, owner, sender); right_role = _role(right, owner, sender)
            relevant = left_role in ('OWNER', 'SIGNER') or topics[0] == TRANSFER and right_role in ('OWNER', 'SIGNER')
            if relevant:
                item = {'asset': tokens[emitter], 'contract_ref': _ref(reference, 'address', emitter),
                        'units': str(int(data, 16)), 'log_index': str(at), 'log_ref': log_ref,
                        'left_role': left_role, 'left_ref': _ref(reference, 'address', left),
                        'right_role': right_role, 'right_ref': _ref(reference, 'address', right)}
                (transfers if topics[0] == TRANSFER else approvals).append(item)
                continue
        remaining.append({'log_index': str(at), 'log_ref': log_ref, 'contract_ref': _ref(reference, 'address', emitter)})
    return transfers, approvals, remaining


def _attestation(source: CallSource, proof: dict, reference: Reference) -> str:
    return _ref(reference, 'attestation', {'source_kind': source.kind, 'source_id': source.id,
        'terms_digest': _encoded(source.terms)[1], 'grant_digest': _encoded(source.grant)[1] if source.grant is not None else None,
        'source_created_at': _time(source.created_at).isoformat(), 'reported_tx_ref': source.original_tx_ref,
        'prior_observation': source.observation, 'source_fee': source.fee,
        'proof': {key: value for key, value in proof.items() if key != 'observation_ref'}})


def verify_external_call(source: CallSource, evidence: dict, *, reference: Reference, now: datetime) -> dict:
    """A success receipt plus signature facts; never a trade, cancellation or settlement."""
    try:
        if (not isinstance(source, CallSource) or source.kind not in ('WALLET_SWAP', 'OPERATOR')
                or str(UUID(source.id)) != source.id or not isinstance(evidence, dict)
                or len(json.dumps(evidence, allow_nan=False).encode()) > MAX_BYTES
                or set(evidence) != {'chain_id', 'tx_hash', 'transaction', 'receipt', 'block', 'finalized',
                                    'source_code', 'target_code', 'source_nonce', 'pool', 'runtime', 'post_allowance'}
                or evidence['chain_id'] != '0x38' or evidence['source_code'] != '0x'):
            raise ValueError('source/evidence profile')
        tx, receipt, block, final = [evidence[key] for key in ('transaction', 'receipt', 'block', 'finalized')]
        tx_hash = word(evidence['tx_hash']); sender = canonical_wallet_address(tx['from']); owner = canonical_wallet_address(source.owner)
        target = None if tx.get('to') is None else canonical_wallet_address(tx['to']); value = quantity(tx['value']); data = _data(tx['input'])
        if quantity(receipt['status']) != 1:
            raise ValueError('successful receipt required')
        recovered, typed, limit, cap, priority = _recover_wallet_transaction(tx, expected_to=target,
            expected_value=value, native=False, allow_empty_data=True)
        expected = source.expected_call()
        if (target == expected['to'] and data == expected['data'].lower() and value == 0
                or target == sender and data == '0x' and value == 0):
            raise ValueError('use the existing known-call/cancellation profile')
        owner_digest = source.terms['wallet_digest'] if source.kind == 'WALLET_SWAP' else source.terms['owner_digest']
        if (recovered != sender or reference('wallet', sender) != source.signer_digest
                or reference('wallet', owner) != owner_digest or quantity(tx['nonce']) != source.nonce):
            raise ValueError('signer/owner/nonce')
        height, block_hash, at = finalized_block(block, final, created_at=source.created_at, now=_time(now))
        index = quantity(tx['transactionIndex']); transactions = block['transactions']
        if (not isinstance(transactions, list) or len(transactions) > 100000 or index >= len(transactions)
                or word(transactions[index]) != tx_hash or word(tx['hash']) != tx_hash or word(receipt['transactionHash']) != tx_hash
                or canonical_wallet_address(receipt['from']) != sender or quantity(receipt['type']) != typed
                or (None if receipt.get('to') is None else canonical_wallet_address(receipt['to'])) != target
                or quantity(receipt['transactionIndex']) != index
                or any(quantity(item['blockNumber']) != height or word(item['blockHash']) != block_hash for item in (tx, receipt))):
            raise ValueError('receipt/block membership')
        created = receipt['contractAddress']
        if (target is None and (created is None or canonical_wallet_address(created) != executor_address(sender, source.nonce))
                or target is not None and created is not None):
            raise ValueError('creation result')
        if target is None:
            call_kind = 'CREATE'
        else:
            code = _data(evidence['target_code'])
            call_kind = 'CONTRACT_CALL' if code != '0x' else 'NATIVE_TRANSFER' if data == '0x' and value > 0 else 'EOA_CALL'
        consumed = quantity(evidence['source_nonce']); used = quantity(receipt['gasUsed'])
        base = quantity(block['baseFeePerGas']) if typed == 2 else None
        price = min(cap, base+priority) if typed == 2 else cap
        if (not source.nonce < consumed <= 2**64-1 or not 21000 <= used <= limit <= quantity(block['gasLimit'])
                or limit*cap+value >= 2**256 or typed == 2 and cap < base
                or quantity(receipt['effectiveGasPrice']) != price or target is None and used < 53000):
            raise ValueError('gas/nonce accounting')
        transfers, approvals, remaining = _logs(receipt['logs'], owner=owner, sender=sender, height=height,
            block_hash=block_hash, tx_hash=tx_hash, index=index, reference=reference)
        receipt_facts = {'status': 'FINALIZED', 'block_number': str(height), 'block_hash': block_hash, 'block_at': at.isoformat(),
            'finality': 'FINALIZED_RPC_OBSERVED',
            'gas_used': str(used), 'gas_limit': str(limit), 'block_gas_limit': str(quantity(block['gasLimit'])),
            'transaction_type': typed, 'fee_cap': str(cap), 'priority_fee': None if priority is None else str(priority),
            'base_fee': None if base is None else str(base), 'effective_gas_price': str(price), 'gas_fee_wei': str(used*price)}
        proof = {'schema': SCHEMA, 'source_kind': source.kind, 'source_id': source.id, 'chain_id': '0x38',
            'token_registry': TOKEN_REGISTRY, 'native_coverage': 'TOP_LEVEL_VALUE_AND_GAS_ONLY',
            'token_coverage': 'REGISTERED_TOKEN_EVENTS_ONLY', 'gas_payer_role': 'OWNER' if sender == owner else 'SIGNER',
            'outcome': 'EXTERNAL_SUCCEEDED', 'nonce': str(source.nonce), 'source_nonce': str(consumed), 'transaction_index': str(index),
            'tx_ref': reference('tx', tx_hash), 'signer_ref': source.signer_digest, 'owner_ref': owner_digest,
            'owner_address_ref': _ref(reference, 'address', owner), 'signer_address_ref': _ref(reference, 'address', sender),
            'signer_is_owner': sender == owner, 'target_ref': None if target is None else _ref(reference, 'address', target),
            'created_ref': None if created is None else _ref(reference, 'address', canonical_wallet_address(created)),
            'call_ref': _ref(reference, 'call', {'to': target, 'value': str(value), 'data': data}),
            'expected_call_ref': _ref(reference, 'call', {'to': expected['to'], 'value': '0', 'data': expected['data'].lower()}),
            'call_kind': call_kind, 'native_value_wei': str(value), 'native_target_role': None if target is None else _role(target, owner, sender),
            'receipt': receipt_facts, 'transfers': transfers, 'approvals': approvals, 'uninterpreted_logs': remaining,
            'effects_complete': False, 'accounting_complete': False, 'resolves_intent': False, 'trading_authority': False,
            'matches_reported_tx': None if source.original_tx_ref is None else reference('tx', tx_hash) == source.original_tx_ref,
            'fee_changed': typed != 0 or limit != int(source.terms['gas']) or cap != int(source.terms['gas_price']),
            'fee_overrun': limit*cap > int(source.terms['max_gas_wei']) or cap > int(source.terms['gas_price'])}
        proof['observation_ref'] = _attestation(source, proof, reference)
        validate_external_proof(proof, source, reference=reference, now=now)
        return proof
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, DecodingError, BadSignature, ValidationError):
        raise DexExecutorError('La llamada externa no demuestra firma, nonce, recibo y efectos observados coherentes') from None


def validate_external_proof(proof: dict, source: CallSource, *, reference: Reference, now: datetime) -> None:
    """Reopen an attested observation without retaining a live owner address."""
    keys = {'schema', 'source_kind', 'source_id', 'chain_id', 'token_registry', 'native_coverage', 'token_coverage',
        'gas_payer_role', 'outcome', 'nonce', 'source_nonce', 'transaction_index', 'tx_ref',
        'signer_ref', 'owner_ref', 'owner_address_ref', 'signer_address_ref', 'signer_is_owner', 'target_ref', 'created_ref', 'call_ref', 'expected_call_ref', 'call_kind',
        'native_value_wei', 'native_target_role', 'receipt', 'transfers', 'approvals', 'uninterpreted_logs', 'effects_complete',
        'accounting_complete', 'resolves_intent', 'trading_authority', 'matches_reported_tx', 'fee_changed', 'fee_overrun', 'observation_ref'}
    if (not isinstance(proof, dict) or set(proof) != keys or proof['schema'] != SCHEMA or proof['chain_id'] != '0x38'
            or proof['source_kind'] != source.kind or proof['source_id'] != source.id or proof['outcome'] != 'EXTERNAL_SUCCEEDED'
            or proof['token_registry'] != TOKEN_REGISTRY or proof['native_coverage'] != 'TOP_LEVEL_VALUE_AND_GAS_ONLY'
            or proof['token_coverage'] != 'REGISTERED_TOKEN_EVENTS_ONLY'
            or proof['nonce'] != str(source.nonce) or proof['signer_ref'] != source.signer_digest
            or proof['owner_ref'] != source.terms['wallet_digest' if source.kind == 'WALLET_SWAP' else 'owner_digest']
            or any(proof[key] is not False for key in ('effects_complete', 'accounting_complete', 'resolves_intent', 'trading_authority'))
            or any(type(proof[key]) is not bool for key in ('signer_is_owner', 'fee_changed', 'fee_overrun'))
            or any(not isinstance(proof[key], str) or REF.fullmatch(proof[key]) is None
                   for key in ('tx_ref', 'signer_ref', 'owner_ref', 'owner_address_ref', 'signer_address_ref', 'call_ref', 'expected_call_ref', 'observation_ref'))
            or proof['call_ref'] == proof['expected_call_ref']):
        raise ValueError('external observation identity/profile')
    if proof['signer_is_owner'] != (proof['owner_ref'] == proof['signer_ref']):
        raise ValueError('signer role')
    if proof['signer_is_owner'] != (proof['owner_address_ref'] == proof['signer_address_ref']):
        raise ValueError('address role')
    if proof['gas_payer_role'] != ('OWNER' if proof['signer_is_owner'] else 'SIGNER'):
        raise ValueError('gas payer')
    reported = proof['matches_reported_tx']
    if ((source.original_tx_ref is None and reported is not None) or source.original_tx_ref is not None and (
            type(reported) is not bool or reported != (proof['tx_ref'] == source.original_tx_ref))):
        raise ValueError('reported transaction reference')
    if _uint(proof['source_nonce'], 2**64-1) <= source.nonce or _uint(proof['transaction_index'], 99999) >= 100000:
        raise ValueError('nonce/index')
    value = _uint(proof['native_value_wei'])
    if proof['call_kind'] not in ('CREATE', 'CONTRACT_CALL', 'NATIVE_TRANSFER', 'EOA_CALL'):
        raise ValueError('call kind')
    creation = proof['call_kind'] == 'CREATE'
    if (creation != (proof['target_ref'] is None) or creation != (proof['created_ref'] is not None)
            or creation != (proof['native_target_role'] is None)
            or not creation and proof['native_target_role'] not in ROLES
            or proof['call_kind'] == 'NATIVE_TRANSFER' and not value):
        raise ValueError('call target/value')
    for key in ('target_ref', 'created_ref'):
        if proof[key] is not None and (not isinstance(proof[key], str) or REF.fullmatch(proof[key]) is None):
            raise ValueError('target reference')
    expected = {'OWNER': proof['owner_address_ref'], 'SIGNER': proof['signer_address_ref'],
                'ZERO': _ref(reference, 'address', ZERO)}
    def validate_role(role: str, address_ref: str) -> None:
        if (role not in ROLES or role == 'SIGNER' and proof['signer_is_owner']
                or (address_ref in expected.values() if role == 'OTHER' else address_ref != expected[role])):
            raise ValueError('address role binding')
    if not creation:
        validate_role(proof['native_target_role'], proof['target_ref'])
    receipt = proof['receipt']
    receipt_keys = {'status', 'block_number', 'block_hash', 'block_at', 'finality',
        'gas_used', 'gas_limit', 'block_gas_limit', 'transaction_type', 'fee_cap', 'priority_fee', 'base_fee', 'effective_gas_price', 'gas_fee_wei'}
    if (not isinstance(receipt, dict) or set(receipt) != receipt_keys or receipt['status'] != 'FINALIZED'
            or receipt['finality'] != 'FINALIZED_RPC_OBSERVED'):
        raise ValueError('receipt shape')
    height = _uint(receipt['block_number']); block_hash = word(receipt['block_hash'])
    at = _time(datetime.fromisoformat(receipt['block_at']))
    if (height == 0 or at.isoformat() != receipt['block_at'] or at > _time(now)+timedelta(seconds=15)
            or at < _time(source.created_at)-timedelta(seconds=15)):
        raise ValueError('receipt chronology')
    used = _uint(receipt['gas_used'], 2**64-1); limit = _uint(receipt['gas_limit'], 2**64-1)
    cap = _uint(receipt['fee_cap']); price = _uint(receipt['effective_gas_price']); gas = _uint(receipt['gas_fee_wei'])
    typed = receipt['transaction_type']
    if type(typed) is not int or typed not in (0, 2):
        raise ValueError('transaction type')
    if typed == 0:
        if receipt['priority_fee'] is not None or receipt['base_fee'] is not None or price != cap:
            raise ValueError('legacy fee')
    else:
        priority = _uint(receipt['priority_fee']); base = _uint(receipt['base_fee'])
        if priority > cap or base > cap or price != min(cap, base+priority):
            raise ValueError('1559 fee')
    if (not (53000 if creation else 21000) <= used <= limit <= _uint(receipt['block_gas_limit'], 2**64-1)
            or limit*cap+value >= 2**256 or gas != used*price
            or proof['fee_changed'] != (typed != 0 or limit != int(source.terms['gas']) or cap != int(source.terms['gas_price']))
            or proof['fee_overrun'] != (limit*cap > int(source.terms['max_gas_wei']) or cap > int(source.terms['gas_price']))):
        raise ValueError('gas arithmetic')
    indices: set[int] = set()
    for group in ('transfers', 'approvals', 'uninterpreted_logs'):
        entries = proof[group]
        if not isinstance(entries, list) or len(entries) > MAX_LOGS:
            raise ValueError('log array')
        previous = -1
        for item in entries:
            expected_keys = {'log_index', 'log_ref', 'contract_ref'} if group == 'uninterpreted_logs' else {
                'asset', 'contract_ref', 'units', 'log_index', 'log_ref', 'left_role', 'left_ref', 'right_role', 'right_ref'}
            if not isinstance(item, dict) or set(item) != expected_keys:
                raise ValueError('log facts shape')
            index = _uint(item['log_index'], 99999)
            if index <= previous or index in indices:
                raise ValueError('log order/duplicate')
            indices.add(index); previous = index
            for name in ('log_ref', 'contract_ref') if group == 'uninterpreted_logs' else ('log_ref', 'contract_ref', 'left_ref', 'right_ref'):
                if not isinstance(item[name], str) or REF.fullmatch(item[name]) is None:
                    raise ValueError('log reference')
            if group != 'uninterpreted_logs':
                _uint(item['units'])
                if (item['asset'] not in TOKENS or item['left_role'] not in ROLES or item['right_role'] not in ROLES
                        or not (item['left_role'] in ('OWNER', 'SIGNER') or group == 'transfers' and item['right_role'] in ('OWNER', 'SIGNER'))):
                    raise ValueError('token log scope')
                if item['contract_ref'] != _ref(reference, 'address', TOKENS[item['asset']]):
                    raise ValueError('token registry binding')
                validate_role(item['left_role'], item['left_ref'])
                validate_role(item['right_role'], item['right_ref'])
    if len(indices) > MAX_LOGS:
        raise ValueError('total log limit')
    if source.kind == 'OPERATOR':
        anchor = source.terms['anchor']
        if height < int(anchor['block_number']) or height == int(anchor['block_number']) and block_hash != anchor['block_hash']:
            raise ValueError('operator anchor')
    require_receipt_before_observation(receipt, source.observation)
    if source.observation is not None and receipt['block_number'] == source.observation['block_number'] and proof['source_nonce'] != source.observation['operator_nonce']:
        raise ValueError('nonce observation')
    if not hmac.compare_digest(proof['observation_ref'], _attestation(source, proof, reference)):
        raise ValueError('observation attestation')
