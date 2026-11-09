"""Wallet configuration is reviewed against fresh chain state and claimed once."""

from app.portfolio.vault import SCHEMA_VERSION
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from copy import deepcopy
from eth_abi import encode
from eth_utils import keccak

from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_controls as controls, dex_deployments as deployments
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup, PASSWORD
from tests.test_dex_deployment_receipt import deployment_on_evm
from tests.test_dex_policy import policy
from app.portfolio.vault import VaultError


def confirmed_deployment(vault, account, now, meta, rpc):
    plan, evidence = deployment_on_evm(vault, account, now, meta, rpc)
    with vault.session() as db:
        deployments.check_deployment(db, plan['id'], account_id=account, evidence=evidence, now=datetime.now(timezone.utc))
    return plan


def prepared_control(vault, account, now, meta, rpc, *, action='CONFIGURE'):
    deployment = confirmed_deployment(vault, account, now, meta, rpc)
    now = datetime.now(timezone.utc)
    state = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500,
        candidate_operator=meta['operator'] if action == 'CONFIGURE' else None, now=now)
    with vault.session() as db:
        plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
            wallet_address=meta['owner'], action=action, operator=meta['operator'] if action == 'CONFIGURE' else None,
            limits=policy(int(now.timestamp())) if action == 'CONFIGURE' else None, snapshot=state,
            gas='1000000', gas_price='1000000000', max_gas_bnb='0.002', now=now)
    args = {'account_id': account, 'wallet_address': meta['owner'], 'operator': meta['operator'] if action == 'CONFIGURE' else None,
        'plan_digest': plan['plan_digest'], 'snapshot': state, 'now': now}
    return deployment, plan, state, args


def test_reviewed_configuration_is_durable_private_and_claimed_once_before_the_wallet_call(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = confirmed_deployment(vault, account, now, meta, rpc)
        now = datetime.now(timezone.utc)
        state = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, candidate_operator=meta['operator'], now=now)
        with vault.session() as db:
            plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
                wallet_address=meta['owner'], action='CONFIGURE', operator=meta['operator'], limits=policy(int(now.timestamp())),
                snapshot=state, gas='1000000', gas_price='1000000000', max_gas_bnb='0.002', now=now)
        assert plan['status'] == 'PREPARED' and not plan['automatic_execution']
        args = {'account_id': account, 'wallet_address': meta['owner'], 'operator': meta['operator'],
            'plan_digest': plan['plan_digest'], 'snapshot': state, 'now': now}
        with vault.session() as db:assert controls.claim_control(db, plan['id'], **args)['claim_granted']
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert not controls.claim_control(db, plan['id'], **args)['claim_granted']
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [plan['transaction']])])['status'] == '0x1'
        with vault.session() as db:
            row, _, _ = controls.control_record(db, plan['id'])
            history = controls.list_controls(db, account, now=now)
            assert not history[0]['automatic_execution'] and 'transaction"' not in str(history)
            for address in (meta['owner'], meta['operator'], state.executor_address):
                assert address.lower() not in (row.payload_json+row.state_json+str(history)).lower()
        vault.backup(tmp_path/'claimed-control.db')
    vault.lock()


def test_stop_claim_does_not_fail_because_the_operator_changed_the_position_after_review(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment, configuration, first, args = prepared_control(vault, account, now, meta, rpc)
        with vault.session() as db:controls.claim_control(db, configuration['id'], **args)
        sent = rpc('eth_sendTransaction', [configuration['transaction']])
        with vault.session() as db:controls.report_control(db, configuration['id'], account_id=account, tx_hash=sent, now=datetime.now(timezone.utc))
        def send(sender, target, signature, types, values):
            tx = {'from': sender, 'to': target, 'data': '0x'+(keccak(text=signature)[:4]+encode(types, values)).hex(),
                'gas': hex(1000000), 'gasPrice': hex(1000000000), 'value': '0x0'}
            receipt = rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])
            assert receipt['status'] == '0x1', receipt
        send(meta['operator'], meta['usdt'], 'mint(address,uint256)', ['address', 'uint256'], [meta['owner'], 10**18])
        send(meta['operator'], meta['wbnb'], 'mint(address,uint256)', ['address', 'uint256'], [meta['router'], 10**18])
        send(meta['operator'], meta['router'], 'set(uint256,bool)', ['uint256', 'bool'], [10000, False])
        send(meta['owner'], meta['usdt'], 'approve(address,uint256)', ['address', 'uint256'], [first.executor_address, 10**18])
        now = datetime.now(timezone.utc)
        reviewed = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=now)
        with vault.session() as db:
            stop = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
                wallet_address=meta['owner'], action='STOP', operator=None, limits=None, snapshot=reviewed,
                gas='1000000', gas_price='1000000000', max_gas_bnb='0.002', now=now)
        send(meta['operator'], first.executor_address, 'executeSwap(uint256,uint256,bool,uint256,uint256,uint256)',
            ['uint256', 'uint256', 'bool', 'uint256', 'uint256', 'uint256'], [1, 0, True, 10**16, 10**16, int(now.timestamp())+60])
        current = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=datetime.now(timezone.utc))
        assert current.owned_wbnb > reviewed.owned_wbnb and current.pending_nonce == reviewed.pending_nonce
        with vault.session() as db:
            assert controls.claim_control(db, stop['id'], account_id=account, wallet_address=meta['owner'], operator=None,
                plan_digest=stop['plan_digest'], snapshot=current, now=datetime.now(timezone.utc))['claim_granted']
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [stop['transaction']])])['status'] == '0x1'
        stopped = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=datetime.now(timezone.utc))
        assert stopped.operator == '0x'+'00'*20 and stopped.owned_wbnb == current.owned_wbnb
    vault.lock()


def test_control_backup_rejects_altered_gas_and_policy_event_even_after_recomputing_the_digest(tmp_path):
    from app.portfolio.wallet_funding import _encoded
    from app.portfolio.models import RealDexControlIntent
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        _, plan, snapshot, args = prepared_control(vault, account, now, meta, rpc)
        with vault.session() as db:controls.claim_control(db, plan['id'], **args)
        sent = rpc('eth_sendTransaction', [plan['transaction']]);receipt = rpc('eth_getTransactionReceipt', [sent])
        with vault.session() as db:controls.report_control(db, plan['id'], account_id=account, tx_hash=sent, now=datetime.now(timezone.utc))
        block = rpc('eth_getBlockByNumber', ['finalized', False])
        evidence = {'chain_id': '0x38', 'tx_hash': sent, 'transaction': rpc('eth_getTransactionByHash', [sent]),
            'receipt': receipt, 'block': block, 'finalized': block, 'pool': snapshot.pool,
            'runtime': rpc('eth_getCode', [snapshot.executor_address, block['number']])}
        for section, key, value in [('transaction', 'to', meta['operator']), ('transaction', 'r', '0x1'),
                ('receipt', 'effectiveGasPrice', '0x1'), ('receipt', 'logs', []), ('finalized', 'number', '0x0')]:
            changed = deepcopy(evidence);changed[section][key] = value
            with vault.session() as db:
                with pytest.raises(controls.DexExecutorError):controls.check_control(db, plan['id'], account_id=account, evidence=changed, now=datetime.now(timezone.utc))
        with vault.session() as db:controls.check_control(db, plan['id'], account_id=account, evidence=evidence, now=datetime.now(timezone.utc))
        with vault.session() as db:original = deepcopy(controls.control_record(db, plan['id'])[2])
        for key, value in [('gas_fee_wei', '1'), ('new_policy_id', '2'), ('owned_wbnb', '1'), ('operator_ref', 'a'*64)]:
            changed = deepcopy(original);changed['receipt'][key] = value
            with vault.session() as db:
                row = db.get(RealDexControlIntent, plan['id']);row.state_json, row.state_digest = _encoded(changed);db.commit()
            with pytest.raises(VaultError, match='DEX control'):vault.backup(tmp_path/(key+'-invalid.db'))
            with vault.session() as db:
                row = db.get(RealDexControlIntent, plan['id']);row.state_json, row.state_digest = _encoded(original);db.commit()
        vault.backup(tmp_path/'restored-control.db')
    vault.lock()


def test_v58_migration_preserves_verified_deployment_and_adds_an_empty_control_journal(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = confirmed_deployment(vault, account, now, meta, rpc)
        with vault.session() as db:
            before = deployments.list_deployments(db, account, now=now)
            db.connection().exec_driver_sql('DROP TABLE real_dex_control_intents')
            db.connection().exec_driver_sql('PRAGMA user_version=58');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() ==SCHEMA_VERSION
            assert deployments.list_deployments(db, account, now=now) == before
            assert controls.list_controls(db, account, now=now) == []
        vault.backup(tmp_path/'migrated-controls.db')
    vault.lock()


@pytest.mark.parametrize('replacement', [False, True], ids=['control-mined', 'nonce-replaced'])
def test_lost_response_recovery_requires_a_finalized_consumed_nonce_and_never_invents_execution_or_gas(tmp_path, replacement):
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment, plan, _, args = prepared_control(vault, account, now, meta, rpc)
        with vault.session() as db:controls.claim_control(db, plan['id'], **args)
        snapshot = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500,
            now=datetime.now(timezone.utc), block_tag='finalized')
        with vault.session() as db:
            with pytest.raises(controls.DexExecutorError):controls.recover_control(db, plan['id'], account_id=account,
                wallet_address=meta['owner'], snapshot=snapshot, now=datetime.now(timezone.utc))
        tx = dict(plan['transaction'])
        if replacement:tx.update(to=meta['owner'], data='0x', gas=hex(21000))
        sent = rpc('eth_sendTransaction', [tx])  # The original wallet response was lost before HTTP reporting.
        snapshot = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500,
            now=datetime.now(timezone.utc), block_tag='finalized')
        with vault.session() as db:
            result = controls.recover_control(db, plan['id'], account_id=account, wallet_address=meta['owner'],
                snapshot=snapshot, now=datetime.now(timezone.utc))
        assert result['status'] == 'NONCE_OBSERVED' and result['receipt'] is None and not result['automatic_execution']
        assert result['nonce_observation']['effect_matches'] is (not replacement)
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            from app.portfolio.domain import PortfolioError
            with pytest.raises(PortfolioError):assert_wallet_nonce_available(db, meta['owner'], plan['nonce'])
            assert_wallet_nonce_available(db, meta['owner'], str(int(plan['nonce'])+1))
        assert rpc('eth_getTransactionCount', [meta['owner'], 'pending']) == hex(int(plan['nonce'])+1)
        if not replacement:
            with vault.session() as db:
                reported = controls.report_control(db, plan['id'], account_id=account, tx_hash=sent, now=datetime.now(timezone.utc))
            assert reported['status'] == 'HASH_REPORTED' and reported['nonce_observation']['effect_matches']
        vault.backup(tmp_path/'recovered-nonce.db')
    vault.lock()


def test_control_hash_is_original_private_and_never_implies_confirmed_permissions(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        _, plan, _, args = prepared_control(vault, account, now, meta, rpc)
        with vault.session() as db:controls.claim_control(db, plan['id'], **args)
        tx_hash = rpc('eth_sendTransaction', [plan['transaction']])
        with vault.session() as db:
            result = controls.report_control(db, plan['id'], account_id=account, tx_hash=tx_hash, now=datetime.now(timezone.utc))
        assert result['status'] == 'HASH_REPORTED' and result['receipt'] is None and not result['automatic_execution']
        with vault.session() as db:
            with pytest.raises(controls.DexExecutorError):
                controls.report_control(db, plan['id'], account_id=account, tx_hash='0x'+'ab'*32, now=datetime.now(timezone.utc))
        with vault.session() as db:
            assert tx_hash not in controls.control_record(db, plan['id'])[0].state_json
        vault.backup(tmp_path/'reported-control.db')
    vault.lock()


@pytest.mark.parametrize('action,typed', [('CONFIGURE', False), ('CONFIGURE', True), ('STOP', False)])
def test_signed_control_receipt_records_the_exact_policy_event_and_actual_gas_without_starting_a_bot(tmp_path, action, typed):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        _, plan, snapshot, args = prepared_control(vault, account, now, meta, rpc, action=action)
        with vault.session() as db:controls.claim_control(db, plan['id'], **args)
        tx = dict(plan['transaction'])
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2', maxFeePerGas=hex(1_200_000_000), maxPriorityFeePerGas=hex(100_000_000), accessList=[])
        tx_hash = rpc('eth_sendTransaction', [tx]);receipt = rpc('eth_getTransactionReceipt', [tx_hash])
        with vault.session() as db:controls.report_control(db, plan['id'], account_id=account, tx_hash=tx_hash, now=datetime.now(timezone.utc))
        block = rpc('eth_getBlockByNumber', [receipt['blockNumber'], False])
        evidence = {'chain_id': '0x38', 'tx_hash': tx_hash, 'transaction': rpc('eth_getTransactionByHash', [tx_hash]),
            'receipt': receipt, 'block': block, 'finalized': rpc('eth_getBlockByNumber', ['finalized', False]),
            'pool': snapshot.pool, 'runtime': rpc('eth_getCode', [snapshot.executor_address, block['number']])}
        with vault.session() as db:
            result = controls.check_control(db, plan['id'], account_id=account, evidence=evidence, now=datetime.now(timezone.utc))
        assert result['status'] == 'FINALIZED' and not result['automatic_execution']
        assert result['receipt']['new_policy_id'] == '1'
        assert result['receipt']['gas_fee_wei'] == str(int(receipt['gasUsed'], 16)*int(receipt['effectiveGasPrice'], 16))
        assert result['receipt']['action'] == action
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            row, _, _ = controls.control_record(db, plan['id'])
            assert controls.list_controls(db, account, now=datetime.now(timezone.utc))[0]['receipt'] == result['receipt']
            for secret in (meta['owner'], meta['operator'], snapshot.executor_address, tx_hash):
                assert secret.lower() not in (row.payload_json+row.state_json).lower()
        vault.backup(tmp_path/'confirmed-control.db')
    vault.lock()
