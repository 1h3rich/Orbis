"""Wallet deployment → signed transaction/receipt → durable private confirmation."""
from datetime import datetime, timezone
from uuid import uuid4
from copy import deepcopy

import pytest
from app.live_execution.dex_artifact import DexExecutorError, executor_address

from app.portfolio import dex_deployments as journal
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup, prepare, PASSWORD
from app.portfolio.domain import PortfolioError
from app.portfolio.vault import VaultError


def deployment_on_evm(vault, account, now, meta, rpc, *, typed=False, fee=500):
    nonce = int(rpc('eth_getTransactionCount', [meta['owner'], 'pending']), 16)
    plan = prepare(vault, account, now, wallet_address=meta['owner'], nonce=str(nonce), fee=fee)
    with vault.session() as db:
        assert journal.claim_deployment(db, plan['id'], account_id=account, wallet_address=meta['owner'],
            plan_digest=plan['plan_digest'], now=now)['claim_granted']
    transaction = dict(plan['transaction'])
    if typed:
        transaction.pop('gasPrice')
        transaction.update(type='0x2', maxFeePerGas=hex(1_200_000_000), maxPriorityFeePerGas=hex(100_000_000), accessList=[])
    tx_hash = rpc('eth_sendTransaction', [transaction])
    tx = rpc('eth_getTransactionByHash', [tx_hash])
    receipt = rpc('eth_getTransactionReceipt', [tx_hash])
    block = rpc('eth_getBlockByNumber', [receipt['blockNumber'], False])
    evidence = {'chain_id': '0x38', 'tx_hash': tx_hash, 'transaction': tx, 'receipt': receipt, 'block': block,
        'finalized': rpc('eth_getBlockByNumber', ['finalized', False]), 'pool': meta['pool'],
        'runtime': rpc('eth_getCode', [executor_address(meta['owner'], nonce), receipt['blockNumber']])}
    with vault.session() as db:
        journal.report_deployment(db, plan['id'], account_id=account, tx_hash=tx_hash, now=datetime.now(timezone.utc))
    return plan, evidence


@pytest.mark.parametrize('typed', [False, True], ids=['legacy', 'eip1559'])
def test_signed_creation_receipt_confirms_exact_program_without_persisting_wallet_or_transaction(tmp_path, typed):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        plan, evidence = deployment_on_evm(vault, account, now, meta, rpc, typed=typed)
        checked_at = datetime.now(timezone.utc)
        with vault.session() as db:
            result = journal.check_deployment(db, plan['id'], account_id=account, evidence=evidence, now=checked_at)
        assert result['status'] == 'FINALIZED' and result['executor_verified'] and not result['automatic_execution']
        receipt = evidence['receipt']
        assert result['receipt']['gas_fee_wei'] == str(int(receipt['gasUsed'], 16)*int(receipt['effectiveGasPrice'], 16))
        assert result['receipt']['transaction_type'] == (2 if typed else 0)
        vault.lock(); vault.unlock(PASSWORD)
        with vault.session() as db:
            assert journal.list_deployments(db, account, now=checked_at)[0] == result
            row, _, _ = journal.deployment_record(db, plan['id'])
            for private in [meta['owner'].lower(), receipt['contractAddress'].lower(), evidence['tx_hash'].lower(),
                            evidence['transaction']['r'].lower(), evidence['runtime'].lower()]:
                assert private not in (row.payload_json+row.state_json).lower()
        vault.backup(tmp_path/'verified-executor.db')
    vault.lock()


@pytest.mark.parametrize('hash_already_reported', [False, True], ids=['lost-hash', 'known-hash'])
def test_wallet_request_can_be_recovered_from_finalized_code_without_inventing_a_fee_or_resending(tmp_path, hash_already_reported):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = int(rpc('eth_getTransactionCount', [meta['owner'], 'pending']), 16)
        plan = prepare(vault, account, now, wallet_address=meta['owner'], nonce=str(nonce))
        with vault.session() as db:
            journal.claim_deployment(db, plan['id'], account_id=account, wallet_address=meta['owner'],
                plan_digest=plan['plan_digest'], now=now)
        tx_hash = rpc('eth_sendTransaction', [plan['transaction']])  # Wallet sent, HTTP reply lost before journal report.
        if hash_already_reported:
            with vault.session() as db:
                journal.report_deployment(db, plan['id'], account_id=account, tx_hash=tx_hash, now=datetime.now(timezone.utc))
        receipt = rpc('eth_getTransactionReceipt', [tx_hash])
        block = rpc('eth_getBlockByNumber', ['finalized', False])
        evidence = {'chain_id': '0x38', 'block': block, 'pool': meta['pool'], 'contract_address': receipt['contractAddress'],
            'runtime': rpc('eth_getCode', [receipt['contractAddress'], block['number']]),
            'owner_nonce': rpc('eth_getTransactionCount', [meta['owner'], block['number']])}
        with vault.session() as db:
            recovered = journal.recover_deployment(db, plan['id'], account_id=account, wallet_address=meta['owner'],
                evidence=evidence, now=datetime.now(timezone.utc))
        assert recovered['status'] == 'CODE_OBSERVED' and recovered['executor_verified']
        assert recovered['receipt'] is None and not recovered['automatic_execution']
        assert recovered['code_observation']['source'] == 'WALLET_RPC_CODE_OBSERVED'
        vault.lock(); vault.unlock(PASSWORD)
        with vault.session() as db:
            from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
            with pytest.raises(PortfolioError):assert_wallet_nonce_available(db, meta['owner'], str(nonce))
            assert_wallet_nonce_available(db, meta['owner'], str(nonce+1))
        with vault.session() as db:
            reported = journal.report_deployment(db, plan['id'], account_id=account, tx_hash=tx_hash, now=datetime.now(timezone.utc))
        assert reported['status'] == ('CODE_OBSERVED' if hash_already_reported else 'HASH_REPORTED') and reported['executor_verified']
        receipt_evidence = {'chain_id': '0x38', 'tx_hash': tx_hash, 'transaction': rpc('eth_getTransactionByHash', [tx_hash]),
            'receipt': receipt, 'block': rpc('eth_getBlockByNumber', [receipt['blockNumber'], False]),
            'finalized': block, 'pool': meta['pool'], 'runtime': evidence['runtime']}
        with vault.session() as db:
            completed = journal.check_deployment(db, plan['id'], account_id=account, evidence=receipt_evidence, now=datetime.now(timezone.utc))
        assert completed['status'] == 'FINALIZED' and completed['receipt']['gas_fee_wei'] is not None
        assert rpc('eth_getTransactionCount', [meta['owner'], 'pending']) == hex(nonce+1)
        vault.backup(tmp_path/'code-recovery.db')
    vault.lock()


def test_reverted_constructor_records_actual_gas_but_never_a_verified_executor(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        plan, evidence = deployment_on_evm(vault, account, now, meta, rpc, fee=3000)
        assert evidence['receipt']['status'] == '0x0' and evidence['runtime'] == '0x'
        with vault.session() as db:
            result = journal.check_deployment(db, plan['id'], account_id=account, evidence=evidence, now=datetime.now(timezone.utc))
        assert result['status'] == 'REVERTED' and not result['executor_verified']
        assert int(result['receipt']['gas_fee_wei']) > 0 and not result['automatic_execution']
        vault.backup(tmp_path/'reverted-creation.db')
    vault.lock()


def test_backup_refuses_invalid_financial_receipt_even_with_recomputed_storage_digest(tmp_path):
    from app.portfolio.wallet_funding import _encoded
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        plan, evidence = deployment_on_evm(vault, account, now, meta, rpc, typed=True)
        with vault.session() as db:
            journal.check_deployment(db, plan['id'], account_id=account, evidence=evidence, now=datetime.now(timezone.utc))
        with vault.session() as db:original = deepcopy(journal.deployment_record(db, plan['id'])[2])
        for key, value in [('gas_fee_wei', '1'), ('gas_used', '1'), ('fee_cap', str(10**12)), ('priority_fee', '999999999999'), ('transaction_type', True)]:
            changed = deepcopy(original); changed['receipt'][key] = value
            with vault.session() as db:
                row, _, _ = journal.deployment_record(db, plan['id'])
                row.state_json, row.state_digest = _encoded(changed); db.commit()
            with pytest.raises(VaultError, match='DEX deployment'):vault.backup(tmp_path/(key+'-invalid.db'))
            with vault.session() as db:
                from app.portfolio.models import RealDexDeploymentIntent
                row = db.get(RealDexDeploymentIntent, plan['id'])
                row.state_json, row.state_digest = _encoded(original); db.commit()
        # A code-only observation cannot fabricate the missing original hash
        # and jump directly to a financial receipt in a rewritten archive.
        changed = deepcopy(original); changed['tx_ref'] = None
        changed['history'][2]['status'] = 'CODE_OBSERVED'
        changed['code_observation'] = {'source': 'WALLET_RPC_CODE_OBSERVED', 'block_number': original['receipt']['block_number'],
            'block_hash': original['receipt']['block_hash'], 'block_at': original['receipt']['block_at'],
            'owner_nonce': str(int(plan['nonce'])+1), 'observation_ref': '1'*64}
        with vault.session() as db:
            row, _, _ = journal.deployment_record(db, plan['id'])
            row.state_json, row.state_digest = _encoded(changed); db.commit()
        with pytest.raises(VaultError, match='DEX deployment'):vault.backup(tmp_path/'missing-original-hash.db')
        with vault.session() as db:
            row = db.get(RealDexDeploymentIntent, plan['id'])
            row.state_json, row.state_digest = _encoded(original); db.commit()
        vault.backup(tmp_path/'restored-receipt.db')
    vault.lock()


def test_altered_receipt_signature_program_or_fee_is_rejected_without_consuming_the_original_proof(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        plan, evidence = deployment_on_evm(vault, account, now, meta, rpc, typed=True)
        variants = [
            ('chain_id', None, '0x1'), ('transaction', 'nonce', '0x55'),
            ('transaction', 'from', '0x'+'ab'*20), ('transaction', 'r', '0x1'),
            ('transaction', 'input', evidence['transaction']['input'][:-2]+'00'),
            ('transaction', 'maxFeePerGas', hex(10**12)), ('transaction', 'v', '0x2'),
            ('receipt', 'effectiveGasPrice', '0x1'), ('receipt', 'gasUsed', '0x1'),
            ('receipt', 'transactionHash', '0x'+'cd'*32), ('receipt', 'contractAddress', meta['operator']),
            ('finalized', 'number', '0x0'), ('pool', None, meta['operator']),
            ('runtime', None, evidence['runtime'][:-2]+'00'),
        ]
        for section, field, replacement in variants:
            corrupted = deepcopy(evidence)
            if field is None: corrupted[section] = replacement
            else: corrupted[section][field] = replacement
            with vault.session() as db:
                with pytest.raises(DexExecutorError):
                    journal.check_deployment(db, plan['id'], account_id=account, evidence=corrupted, now=datetime.now(timezone.utc))
            with vault.session() as db:
                assert journal.list_deployments(db, account, now=now)[0]['status'] == 'HASH_REPORTED'
        with vault.session() as db:
            assert journal.check_deployment(db, plan['id'], account_id=account, evidence=evidence,
                now=datetime.now(timezone.utc))['executor_verified']
    vault.lock()
