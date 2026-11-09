"""Supplemental order certificate for external-call observations, without I/O.

Source observations keep their v1 wire format. The certificate has its own
profile/seal, plus the same block-list/signer domains as earlier order proofs.
It is never a trade, booking or authority to execute a strategy.
"""
from datetime import datetime
import hashlib
import hmac
import re
from uuid import UUID

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import CallSource
from app.live_execution.dex_external_observation import Reference, verify_external_call
from app.live_execution.evm_transaction import word
from app.portfolio.wallet_funding import _encoded, _time

SCHEMA = 'orbis.dex.external.order.proof.v1'
REF = re.compile(r'[0-9a-f]{64}')


def common_reference(salt: bytes) -> Reference:
    if type(salt) is not bytes or len(salt) != 32:
        raise ValueError('order reference material')
    return lambda label, value: hmac.new(salt,
        f'dex-deployment-v1:{label}:0x38:{value.lower()}'.encode(), hashlib.sha256).hexdigest()


def _fixed(source: CallSource, observation_id: str, observation_digest: str, observation: dict) -> dict:
    if (str(UUID(observation_id)) != observation_id or REF.fullmatch(observation_digest) is None
            or observation['source_kind'] != source.kind or observation['source_id'] != source.id):
        raise ValueError('source identity')
    receipt = observation['receipt']
    return {'schema': SCHEMA, 'observation_id': observation_id, 'observation_digest': observation_digest,
        'observation_ref': observation['observation_ref'], 'observation_proof_digest': _encoded(observation)[1],
        'source_context_digest': _encoded({'terms': source.terms, 'grant': source.grant,
            'created_at': _time(source.created_at).isoformat(), 'prior_tx_ref': source.original_tx_ref,
            'prior_observation': source.observation, 'fee': source.fee})[1],
        'source_kind': source.kind, 'source_id': source.id, 'context_account_id': source.terms['account_id'],
        'chain_id': 56, 'nonce': observation['nonce'], 'block_number': receipt['block_number'],
        'block_hash': receipt['block_hash'], 'block_at': receipt['block_at'],
        'transaction_index': observation['transaction_index']}


def _seal(proof: dict, common: Reference) -> str:
    return common('dex-external-order-proof', _encoded({k: v for k, v in proof.items() if k != 'certificate_ref'})[0])


def verify_external_order(source: CallSource, observation_id: str, observation_digest: str, observation: dict,
        evidence: dict, *, reference: Reference, common: Reference, now: datetime) -> dict:
    try:
        if verify_external_call(source, evidence, reference=reference, now=now) != observation:
            raise ValueError('observation changed')
        hashes = [word(value) for value in evidence['block']['transactions']]
        if not 1 <= len(hashes) <= 100000 or len(set(hashes)) != len(hashes):
            raise ValueError('block membership')
        proof = {**_fixed(source, observation_id, observation_digest, observation),
            'block_transaction_count': str(len(hashes)),
            'block_transactions_ref': common('dex-receipt-order-block', _encoded(hashes)[0]),
            'common_tx_ref': common('tx', word(evidence['tx_hash'])),
            'owner_digest': common('wallet', source.owner),
            'signer_digest': common('wallet', evidence['transaction']['from'])}
        proof['certificate_ref'] = _seal(proof, common)
        validate_external_order(source, observation_id, observation_digest, observation, proof, common=common)
        return proof
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('El orden externo no conserva su observación firmada y su bloque') from None


def validate_external_order(source: CallSource, observation_id: str, observation_digest: str, observation: dict,
        proof: dict, *, common: Reference) -> None:
    """Reopen without a wallet address; the sealed live mapping stays explicit."""
    try:
        fixed = _fixed(source, observation_id, observation_digest, observation)
        extra = {'block_transaction_count', 'block_transactions_ref', 'common_tx_ref', 'owner_digest', 'signer_digest', 'certificate_ref'}
        if (not isinstance(proof, dict) or set(proof) != set(fixed) | extra
                or any(type(proof[key]) is not type(value) or proof[key] != value for key, value in fixed.items())
                or not isinstance(proof['block_transaction_count'], str)
                or re.fullmatch(r'[1-9][0-9]{0,5}', proof['block_transaction_count']) is None
                or not 0 <= int(proof['transaction_index']) < int(proof['block_transaction_count']) <= 100000
                or any(not isinstance(proof[key], str) or REF.fullmatch(proof[key]) is None for key in extra-{'block_transaction_count'})
                or (proof['owner_digest'] == proof['signer_digest']) != observation['signer_is_owner']
                or source.kind == 'OPERATOR' and (proof['owner_digest'] != observation['owner_ref'] or proof['signer_digest'] != observation['signer_ref'])
                or not hmac.compare_digest(proof['certificate_ref'], _seal(proof, common))):
            raise ValueError('order certificate')
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('La prueba de orden externa cambió o perdió su origen') from None
