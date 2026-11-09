"""Finite token grant uses the durable owner-control flow and survives restart."""

from app.portfolio.vault import SCHEMA_VERSION
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from eth_abi import encode, decode
from eth_utils import keccak
from copy import deepcopy
from app.portfolio.vault import VaultError

from app.portfolio import dex_controls as controls
from app.live_execution.dex_state import read_executor_state
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup, PASSWORD
from tests.test_dex_controls import prepared_control


def ready(vault, account, now, meta, rpc):
    deployment, plan, snapshot, args = prepared_control(vault, account, now, meta, rpc)
    with vault.session() as db:controls.claim_control(db, plan['id'], **args)
    sent = rpc('eth_sendTransaction', [plan['transaction']]);receipt = rpc('eth_getTransactionReceipt', [sent])
    with vault.session() as db:controls.report_control(db, plan['id'], account_id=account, tx_hash=sent, now=datetime.now(timezone.utc))
    block = rpc('eth_getBlockByNumber', ['finalized', False])
    evidence = {'chain_id': '0x38', 'tx_hash': sent, 'transaction': rpc('eth_getTransactionByHash', [sent]), 'receipt': receipt,
        'block': block, 'finalized': block, 'pool': snapshot.pool, 'runtime': rpc('eth_getCode', [snapshot.executor_address, block['number']])}
    with vault.session() as db:controls.check_control(db, plan['id'], account_id=account, evidence=evidence, now=datetime.now(timezone.utc))
    return deployment


def test_finite_executor_approval_is_reviewed_once_and_does_not_change_the_router_allowance(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = ready(vault, account, now, meta, rpc);now = datetime.now(timezone.utc)
        state = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=now)
        with vault.session() as db:
            plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
                wallet_address=meta['owner'], action='APPROVE', operator=None, limits=None, token='USDT', allowance_units='1000000000000000001',
                snapshot=state, gas='100000', gas_price='1000000000', max_gas_bnb='0.001', now=now)
        assert plan['allowance']['units'] == '1000000000000000001' and not plan['automatic_execution']
        args = dict(account_id=account, wallet_address=meta['owner'], operator=None, plan_digest=plan['plan_digest'], snapshot=state, now=now)
        with vault.session() as db:assert controls.claim_control(db, plan['id'], **args)['claim_granted']
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert not controls.claim_control(db, plan['id'], **args)['claim_granted']
        tx_hash = rpc('eth_sendTransaction', [plan['transaction']])
        assert rpc('eth_getTransactionReceipt', [tx_hash])['status'] == '0x1'
        with vault.session() as db:
            controls.report_control(db, plan['id'], account_id=account, tx_hash=tx_hash, now=datetime.now(timezone.utc))
            row, terms, _ = controls.control_record(db, plan['id'])
            assert terms['schema'] == 'orbis.dex.control.v2'
            for address in (meta['owner'], state.executor_address, meta['operator']):assert address.lower() not in (row.payload_json+row.state_json).lower()
        current = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=datetime.now(timezone.utc))
        assert current.allowance_usdt == 1000000000000000001
        data = '0x'+(keccak(text='allowance(address,address)')[:4]+encode(['address', 'address'], [meta['owner'], meta['router']])).hex()
        assert decode(['uint256'], bytes.fromhex(rpc('eth_call', [{'to': meta['usdt'], 'data': data}, 'latest'])[2:])) == (0,)
        vault.backup(tmp_path/'pending-allowance.db')
    vault.lock()


@pytest.mark.parametrize('token,action,typed', [('USDT', 'APPROVE', False), ('WBNB', 'APPROVE', True), ('USDT', 'REVOKE', False)])
def test_signed_allowance_event_and_gas_are_archived_without_claiming_current_authority(tmp_path, token, action, typed):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = ready(vault, account, now, meta, rpc);now = datetime.now(timezone.utc)
        snapshot = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=now)
        units = '1' if action == 'APPROVE' else '0'
        with vault.session() as db:
            plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
                wallet_address=meta['owner'], action=action, operator=None, limits=None, token=token, allowance_units=units,
                snapshot=snapshot, gas='100000', gas_price='1000000000', max_gas_bnb='0.001', now=now)
        with vault.session() as db:
            assert controls.claim_control(db, plan['id'], account_id=account, wallet_address=meta['owner'], operator=None,
                plan_digest=plan['plan_digest'], snapshot=snapshot, now=now)['claim_granted']
        tx = dict(plan['transaction'])
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2', maxFeePerGas=hex(1200000000), maxPriorityFeePerGas=hex(100000000), accessList=[])
        sent = rpc('eth_sendTransaction', [tx]);receipt = rpc('eth_getTransactionReceipt', [sent])
        assert receipt['status'] == '0x1'
        with vault.session() as db:controls.report_control(db, plan['id'], account_id=account, tx_hash=sent, now=datetime.now(timezone.utc))
        block = rpc('eth_getBlockByNumber', ['finalized', False])
        evidence = {'chain_id': '0x38', 'tx_hash': sent, 'transaction': rpc('eth_getTransactionByHash', [sent]), 'receipt': receipt,
            'block': block, 'finalized': block, 'pool': snapshot.pool, 'runtime': rpc('eth_getCode', [snapshot.executor_address, block['number']])}
        with vault.session() as db:
            result = controls.check_control(db, plan['id'], account_id=account, evidence=evidence, now=datetime.now(timezone.utc))
        assert result['status'] == 'FINALIZED' and result['receipt']['approved_units'] == units
        assert result['receipt']['new_policy_id'] is None and not result['automatic_execution']
        assert result['receipt']['gas_fee_wei'] == str(int(receipt['gasUsed'], 16)*int(receipt['effectiveGasPrice'], 16))
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert controls.control_record(db, plan['id'])[2]['receipt'] == result['receipt']
        vault.backup(tmp_path/'approval-receipt.db')
    vault.lock()


def test_lost_approval_response_recovers_the_nonce_and_allowance_without_fabricating_a_receipt(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = ready(vault, account, now, meta, rpc);now = datetime.now(timezone.utc)
        snapshot = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=now)
        with vault.session() as db:
            plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
                wallet_address=meta['owner'], action='APPROVE', operator=None, limits=None, token='USDT', allowance_units='1',
                snapshot=snapshot, gas='100000', gas_price='1000000000', max_gas_bnb='0.001', now=now)
        with vault.session() as db:controls.claim_control(db, plan['id'], account_id=account, wallet_address=meta['owner'],
            operator=None, plan_digest=plan['plan_digest'], snapshot=snapshot, now=now)
        sent = rpc('eth_sendTransaction', [plan['transaction']])
        observed = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=datetime.now(timezone.utc), block_tag='finalized')
        with vault.session() as db:
            result = controls.recover_control(db, plan['id'], account_id=account, wallet_address=meta['owner'], snapshot=observed, now=datetime.now(timezone.utc))
        assert result['status'] == 'NONCE_OBSERVED' and result['nonce_observation']['effect_matches']
        assert result['nonce_observation']['allowance_units'] == '1' and result['receipt'] is None
        assert not result['automatic_execution']
        with vault.session() as db:controls.report_control(db, plan['id'], account_id=account, tx_hash=sent, now=datetime.now(timezone.utc))
        vault.backup(tmp_path/'recovered-approval.db')
    vault.lock()


def test_v59_migration_preserves_existing_policy_control_bytes_and_allows_new_finite_grants(tmp_path):
    from app.portfolio.models import RealDexControlIntent
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = ready(vault, account, now, meta, rpc)
        with vault.session() as db:
            row = db.query(RealDexControlIntent).one();before = (row.payload_json, row.payload_digest, row.state_json, row.state_digest)
            db.connection().exec_driver_sql('PRAGMA user_version=59');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            row = db.query(RealDexControlIntent).one()
            assert (row.payload_json, row.payload_digest, row.state_json, row.state_digest) == before
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() ==SCHEMA_VERSION
            assert controls.control_record(db, row.id)[1]['schema'] == 'orbis.dex.control.v1'
        now = datetime.now(timezone.utc)
        state = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=now)
        with vault.session() as db:
            plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'], wallet_address=meta['owner'],
                action='APPROVE', operator=None, limits=None, token='WBNB', allowance_units='1', snapshot=state,
                gas='100000', gas_price='1000000000', max_gas_bnb='0.001', now=now)
        assert plan['allowance']['units'] == '1'
        vault.backup(tmp_path/'migrated-allowance.db')
    vault.lock()


def test_backup_rejects_an_allowance_above_its_policy_even_if_the_json_digest_is_recomputed(tmp_path):
    from app.portfolio.wallet_funding import _encoded
    from app.portfolio.models import RealDexControlIntent
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = ready(vault, account, now, meta, rpc);now = datetime.now(timezone.utc)
        state = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=now)
        with vault.session() as db:
            plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'], wallet_address=meta['owner'],
                action='APPROVE', operator=None, limits=None, token='USDT', allowance_units='1', snapshot=state,
                gas='100000', gas_price='1000000000', max_gas_bnb='0.001', now=now)
            original = deepcopy(controls.control_record(db, plan['id'])[1])
        for key, value in [('units', str(2**256-1)), ('cap_units', str(2**256-1)), ('prior_units', '1'), ('operator_eoa_confirmed', False)]:
            changed = deepcopy(original);changed['allowance'][key] = value
            with vault.session() as db:
                row = db.get(RealDexControlIntent, plan['id']);row.payload_json, row.payload_digest = _encoded(changed);db.commit()
            with pytest.raises(VaultError, match='DEX control'):vault.backup(tmp_path/(key+'-invalid.db'))
            with vault.session() as db:
                row = db.get(RealDexControlIntent, plan['id']);row.payload_json, row.payload_digest = _encoded(original);db.commit()
        vault.backup(tmp_path/'valid-approval.db')
    vault.lock()


def test_approval_receipt_rejects_wrong_spender_amount_or_token_without_consuming_the_valid_proof(tmp_path):
    vault, account, now = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        deployment = ready(vault, account, now, meta, rpc);now = datetime.now(timezone.utc)
        snapshot = read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500, now=now)
        with vault.session() as db:
            plan = controls.prepare_control(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
                wallet_address=meta['owner'], action='APPROVE', operator=None, limits=None, token='USDT', allowance_units='1',
                snapshot=snapshot, gas='100000', gas_price='1000000000', max_gas_bnb='0.001', now=now)
        with vault.session() as db:controls.claim_control(db, plan['id'], account_id=account, wallet_address=meta['owner'], operator=None,
            plan_digest=plan['plan_digest'], snapshot=snapshot, now=now)
        sent = rpc('eth_sendTransaction', [plan['transaction']]);receipt = rpc('eth_getTransactionReceipt', [sent])
        with vault.session() as db:controls.report_control(db, plan['id'], account_id=account, tx_hash=sent, now=datetime.now(timezone.utc))
        block = rpc('eth_getBlockByNumber', ['finalized', False])
        proof = {'chain_id': '0x38', 'tx_hash': sent, 'transaction': rpc('eth_getTransactionByHash', [sent]), 'receipt': receipt,
            'block': block, 'finalized': block, 'pool': snapshot.pool, 'runtime': rpc('eth_getCode', [snapshot.executor_address, block['number']])}
        for fault in ('spender', 'amount', 'token'):
            altered = deepcopy(proof);event = altered['receipt']['logs'][0]
            if fault == 'spender':event['topics'][2] = '0x'+int(meta['router'], 16).to_bytes(32, 'big').hex()
            if fault == 'amount':event['data'] = '0x'+(2).to_bytes(32, 'big').hex()
            if fault == 'token':event['address'] = meta['wbnb']
            with vault.session() as db:
                with pytest.raises(controls.DexExecutorError):controls.check_control(db, plan['id'], account_id=account, evidence=altered, now=datetime.now(timezone.utc))
            with vault.session() as db:assert controls.control_record(db, plan['id'])[0].status == 'HASH_REPORTED'
        with vault.session() as db:
            assert controls.check_control(db, plan['id'], account_id=account, evidence=proof, now=datetime.now(timezone.utc))['receipt']['approved_units'] == '1'
    vault.lock()
