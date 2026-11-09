"""Durable operator nonce/gas authority; actual EVM, no external transactions."""

from app.portfolio.vault import SCHEMA_VERSION
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from copy import deepcopy

import pytest
from eth_utils import keccak

from app.portfolio import dex_operator_journal as journal
from app.live_execution.dex_operator import DexOperatorSession
from app.live_execution.dex_trade import read_trade_plan
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_allowance import allowance_cap, approval_transaction
from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.vault import RealVault, VaultError
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup, PASSWORD
from tests.test_dex_allowance_journal import ready
from tests.test_dex_operator import KEY, write


@contextmanager
def case(directory,*,evm_backend=None):
    vault, account, now = setup(directory)
    try:
        with evm(directory/'evm',backend=evm_backend) as (meta, rpc):
            deployment = ready(vault, account, now, meta, rpc);nonce = int(deployment['nonce'])
            write(rpc, meta, meta['router'], 'set(uint256,bool)', ['uint256','bool'], [10000, False])
            for token, recipient, amount in [('usdt',meta['owner'],10**18),('wbnb',meta['router'],10**18),('wbnb',meta['owner'],5*10**17)]:
                write(rpc, meta, meta[token], 'mint(address,uint256)', ['address','uint256'], [recipient, amount])
            for token in ('USDT','WBNB'):
                now = datetime.now(timezone.utc);state = read_executor_state(rpc, meta['owner'], nonce, 500, now=now)
                cap = allowance_cap(state, token, now=now)
                tx = approval_transaction(meta['owner'], nonce, token, cap, cap=cap,
                    nonce=state.pending_nonce, gas=100000, gas_price=10**9)
                assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
            trade = trade_plan(meta, rpc, nonce)
            with vault.session() as db:
                grant = journal.create_grant(db, identifier=str(uuid4()), account_id=account, deployment_id=deployment['id'],
                    wallet_address=meta['owner'], snapshot=trade.snapshot, gas_budget_wei=2*10**15, now=datetime.now(timezone.utc))
            yield vault, account, meta, rpc, deployment, grant, trade
    finally:
        vault.lock()


def trade_plan(meta, rpc, nonce, *, buy=True, amount=10**16):
    return read_trade_plan(rpc, meta['owner'], nonce, 500, buy=buy, amount=amount, slippage_bps=25,
        gas=500000, gas_price=10**9, max_gas_wei=10**15, now=datetime.now(timezone.utc))


def reserve(vault, account, grant, trade, *, identifier=None, decision=None):
    with vault.session() as db:
        return journal.reserve_intent(db, identifier=identifier or str(uuid4()), account_id=account, grant_id=grant['id'],
            plan=trade, decision_ref=decision or keccak(text=str(uuid4())).hex(), now=datetime.now(timezone.utc))


def test_nonce_gas_and_decision_reservations_survive_reopen_and_do_not_store_addresses(tmp_path):
    with case(tmp_path) as (vault, account, meta, rpc, deployment, grant, trade):
        identifier, decision = str(uuid4()), keccak(text='closed-candle:one').hex()
        first = reserve(vault, account, grant, trade, identifier=identifier, decision=decision)
        assert first['status'] == 'RESERVED' and first['reserved'] and not first['automatic_execution']
        assert not reserve(vault, account, grant, trade, identifier=identifier, decision=decision)['reserved']
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            row, terms, state = journal.intent_record(db, first['id'])
            raw = row.payload_json+row.state_json
            for address in (meta['owner'], meta['operator'], trade.snapshot.executor_address):assert address.lower() not in raw.lower()
            with pytest.raises(DexExecutorError):assert_wallet_nonce_available(db, meta['operator'], str(trade.operator_nonce+1))
            assert journal.grant_view(db, grant['id'], now=datetime.now(timezone.utc))['gas_reserved_wei'] == str(500000*10**9)
        with pytest.raises(DexExecutorError):reserve(vault, account, grant, trade)
        other = RealVault(vault.path);other.unlock(PASSWORD)
        with pytest.raises(DexExecutorError):reserve(other, account, grant, trade)
        other.lock()
        vault.backup(tmp_path/'reserved.db')


def test_same_onchain_policy_cannot_create_another_gas_budget(tmp_path):
    with case(tmp_path) as (vault, account, meta, rpc, deployment, grant, trade):
        with vault.session() as db:
            with pytest.raises(DexExecutorError):journal.create_grant(db, identifier=str(uuid4()), account_id=account,
                deployment_id=deployment['id'], wallet_address=meta['owner'], snapshot=trade.snapshot,
                gas_budget_wei=2*10**15, now=datetime.now(timezone.utc))


def test_only_never_broadcast_reservations_can_be_abandoned_and_release_the_nonce(tmp_path):
    with case(tmp_path) as (vault, account, meta, rpc, deployment, grant, trade):
        first = reserve(vault, account, grant, trade)
        with vault.session() as db:
            abandoned = journal.abandon_unsent(db, first['id'], account_id=account, now=datetime.now(timezone.utc))
            assert abandoned['status'] == 'ABANDONED_UNSENT'
        with vault.session() as db:assert_wallet_nonce_available(db, meta['operator'], str(trade.operator_nonce))
        second = reserve(vault, account, grant, trade)
        session = DexOperatorSession(vault)
        session.connect(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120, gas_budget_wei=2*10**15,
            backup_confirmed=True, now=datetime.now(timezone.utc))
        signed = session.sign_swap(trade, now=datetime.now(timezone.utc))
        with vault.session() as db:journal.record_signature(db, second['id'], account_id=account,
            signed=signed, plan=trade, now=datetime.now(timezone.utc))
        with vault.session() as db:
            assert journal.abandon_unsent(db, second['id'], account_id=account, now=datetime.now(timezone.utc))['status'] == 'ABANDONED_UNSENT'
        session.revoke()


def test_signed_hash_is_archived_before_the_unique_post_and_receipt_preserves_actual_cost(tmp_path):
    from app.live_execution.dex_operator_outbox import prepare_execution
    with case(tmp_path) as (vault, account, meta, rpc, deployment, grant, trade):
        session = DexOperatorSession(vault)
        session.connect(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120, gas_budget_wei=2*10**15,
            backup_confirmed=True, now=datetime.now(timezone.utc))
        prepared = prepare_execution(vault, session, identifier=str(uuid4()), account_id=account, grant_id=grant['id'],
            plan=trade, decision_ref=keccak(text='entry:one').hex(), now=datetime.now(timezone.utc))
        posts = []
        def post(raw):
            with vault.session() as db:
                row, terms, state = journal.intent_record(db, prepared.identifier)
                assert row.status == 'BROADCAST_ATTEMPTED' and state['tx_ref']
            posts.append(raw)
            return rpc('eth_sendRawTransaction', ['0x'+raw.hex()])
        result = prepared.broadcast_once(post, now=datetime.now(timezone.utc))
        assert result['send_result'] == 'HASH_MATCHED' and len(posts) == 1
        with pytest.raises(DexExecutorError):prepared.broadcast_once(post, now=datetime.now(timezone.utc))
        sent = '0x'+keccak(posts[0]).hex();receipt = rpc('eth_getTransactionReceipt', [sent])
        block = rpc('eth_getBlockByNumber', ['finalized',False])
        proof = {'chain_id':'0x38','wallet_address':meta['owner'],'tx_hash':sent,'transaction':rpc('eth_getTransactionByHash',[sent]),
            'receipt':receipt,'block':block,'finalized':block,'pool':meta['pool'],
            'runtime':rpc('eth_getCode',[trade.snapshot.executor_address,block['number']])}
        with vault.session() as db:
            checked = journal.check_intent(db, prepared.identifier, account_id=account, evidence=proof, now=datetime.now(timezone.utc))
            assert checked['status'] == 'FINALIZED'
            assert checked['receipt']['amount_out_units'] == str(10**16)
            assert checked['receipt']['gas_fee_wei'] == str(int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16))
            view = journal.grant_view(db, grant['id'], now=datetime.now(timezone.utc))
            assert view['gas_spent_wei'] == checked['receipt']['gas_fee_wei'] and view['gas_reserved_wei'] == '0'
            assert view['remaining_gas_wei'] == str(2*10**15-int(view['gas_spent_wei']))
        session.revoke();vault.backup(tmp_path/'filled.db')


def prepared_execution(vault, account, grant, trade, session=None):
    from app.live_execution.dex_operator_outbox import prepare_execution
    if session is None:
        session = DexOperatorSession(vault)
        session.connect(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120, gas_budget_wei=2*10**15,
            backup_confirmed=True, now=datetime.now(timezone.utc))
    prepared = prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=grant['id'],
        plan=trade,decision_ref=keccak(text=str(uuid4())).hex(),now=datetime.now(timezone.utc))
    return session,prepared


def evidence(meta,rpc,trade,sent):
    receipt = rpc('eth_getTransactionReceipt',[sent])
    block = rpc('eth_getBlockByNumber',[receipt['blockNumber'],False])
    return {'chain_id':'0x38','wallet_address':meta['owner'],'tx_hash':sent,'transaction':rpc('eth_getTransactionByHash',[sent]),
        'receipt':receipt,'block':block,'finalized':rpc('eth_getBlockByNumber',['finalized',False]),'pool':meta['pool'],
        'runtime':rpc('eth_getCode',[trade.snapshot.executor_address,block['number']])}


def test_lost_response_survives_reopen_and_recovers_nonce_without_a_second_post_or_invented_gas(tmp_path):
    from app.live_execution.dex_operator_outbox import prepare_execution
    from app.live_execution.dex_trade import read_operator_nonce_observation
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade)
        posts = []
        def lost(raw):
            posts.append(raw);rpc('eth_sendRawTransaction',['0x'+raw.hex()]);raise OSError('Fixture reply lost after actual mining')
        assert prepared.broadcast_once(lost,now=datetime.now(timezone.utc))['send_result'] == 'UNKNOWN'
        session.revoke();vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:_,terms,_ = journal.intent_record(db,prepared.identifier)
        replay = prepare_execution(vault,DexOperatorSession(vault),identifier=prepared.identifier,account_id=account,
            grant_id=grant['id'],plan=trade,decision_ref=terms['decision_ref'],now=trade.observed_at+timedelta(days=1))
        assert replay['replayed_without_post'] and not replay['new_order_post_attempted'] and len(posts) == 1
        observed = read_operator_nonce_observation(rpc,meta['owner'],int(deployment['nonce']),500,
            operator=meta['operator'],now=datetime.now(timezone.utc))
        with vault.session() as db:
            recovered = journal.recover_intent(db,prepared.identifier,account_id=account,observation=observed,now=datetime.now(timezone.utc))
            assert recovered['status'] == 'NONCE_OBSERVED' and recovered['receipt'] is None
            view = journal.grant_view(db,grant['id'],now=datetime.now(timezone.utc))
            assert view['gas_spent_wei'] == '0' and view['gas_reserved_wei'] == str(500000*10**9)
        sent = '0x'+keccak(posts[0]).hex()
        with vault.session() as db:
            assert journal.check_intent(db,prepared.identifier,account_id=account,evidence=evidence(meta,rpc,trade,sent),now=datetime.now(timezone.utc))['status'] == 'FINALIZED'
        vault.backup(tmp_path/'recovered.db')


@pytest.mark.parametrize('stop_kind',['local-grant','local-key','vault-generation'])
def test_revocation_before_the_durable_attempt_prevents_any_post(tmp_path,stop_kind):
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade)
        if stop_kind == 'local-grant':
            with vault.session() as db:journal.stop_grant(db,grant['id'],account_id=account,now=datetime.now(timezone.utc))
        elif stop_kind == 'local-key':session.revoke()
        else:vault.lock();vault.unlock(PASSWORD)
        posts = []
        with pytest.raises(DexExecutorError):prepared.broadcast_once(lambda raw:posts.append(raw),now=datetime.now(timezone.utc))
        assert posts == []
        session.revoke()


def test_partial_and_complete_sales_keep_exact_cost_pnl_and_foreign_units_and_stop_has_its_own_receipt(tmp_path):
    from app.live_execution.dex_trade import read_stop_plan
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session = None;pnl = 0
        for buy,amount in [(True,10**16),(False,4*10**15),(False,6*10**15)]:
            current = trade_plan(meta,rpc,int(deployment['nonce']),buy=buy,amount=amount)
            session,prepared = prepared_execution(vault,account,grant,current,session=session)
            hashes = []
            def post(raw):
                sent = rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(sent);return sent
            prepared.broadcast_once(post,now=datetime.now(timezone.utc))
            with vault.session() as db:
                observed = journal.check_intent(db,prepared.identifier,account_id=account,
                    evidence=evidence(meta,rpc,current,hashes[0]),now=datetime.now(timezone.utc))
            assert observed['status'] == 'FINALIZED'
            if not buy:
                assert observed['receipt']['assigned_cost_usdt'] == str(amount)
                assert observed['receipt']['realized_pnl_usdt'] == str(-amount*50//10000)
                pnl += int(observed['receipt']['realized_pnl_usdt'])
            if buy:
                # Router's test control changes price after the original entry.
                from eth_abi import encode
                data = keccak(text='set(uint256,bool)')[:4]+encode(['uint256','bool'],[9950,False])
                rpc('eth_sendTransaction',[{'from':meta['owner'],'to':meta['router'],'data':'0x'+data.hex(),'gas':hex(100000),'gasPrice':hex(10**9)}])
        final = read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc))
        assert final.owned_wbnb == final.cost_usdt == 0 and final.balance_wbnb == 5*10**17
        assert pnl == -5*10**13 and final.realized_loss == -pnl
        stop = read_stop_plan(rpc,meta['owner'],int(deployment['nonce']),500,gas=100000,gas_price=10**9,max_gas_wei=10**14,now=datetime.now(timezone.utc))
        _,prepared = prepared_execution(vault,account,grant,stop,session=session)
        hashes = []
        prepared.broadcast_once(post,now=datetime.now(timezone.utc))
        with vault.session() as db:
            halted = journal.check_intent(db,prepared.identifier,account_id=account,evidence=evidence(meta,rpc,stop,hashes[0]),now=datetime.now(timezone.utc))
            assert halted['receipt']['amount_out_units'] is None
            assert journal.grant_view(db,grant['id'],now=datetime.now(timezone.utc))['status'] == 'STOPPED'
        vault.backup(tmp_path/'closed.db')


def test_reverted_swap_records_only_actual_gas_and_does_not_create_a_fill(tmp_path):
    from eth_abi import encode
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade)
        data = keccak(text='set(uint256,bool)')[:4]+encode(['uint256','bool'],[10000,True])
        rpc('eth_sendTransaction',[{'from':meta['owner'],'to':meta['router'],'data':'0x'+data.hex(),'gas':hex(100000),'gasPrice':hex(10**9)}])
        hashes = []
        def post(raw):
            sent = rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(sent);return sent
        prepared.broadcast_once(post,now=datetime.now(timezone.utc))
        with vault.session() as db:
            observed = journal.check_intent(db,prepared.identifier,account_id=account,evidence=evidence(meta,rpc,trade,hashes[0]),now=datetime.now(timezone.utc))
            assert observed['status'] == 'REVERTED' and observed['receipt']['amount_out_units'] is None
            assert int(observed['receipt']['gas_fee_wei']) > 0 and observed['receipt']['realized_pnl_usdt'] is None
        session.revoke();vault.backup(tmp_path/'reverted.db')


def test_receipt_rejects_tampered_signature_token_flows_cost_and_gas_and_backup_replays_arithmetic(tmp_path):
    from app.portfolio.models import RealDexOperatorIntent
    from app.portfolio.wallet_funding import _encoded
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade);hashes = []
        def post(raw):
            sent = rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(sent);return sent
        prepared.broadcast_once(post,now=datetime.now(timezone.utc))
        proof = evidence(meta,rpc,trade,hashes[0])
        for fault in ('signature','gas','token-flow','position-cost','abi-prefix','finality'):
            changed = deepcopy(proof)
            if fault == 'signature':changed['transaction']['r'] = '0x1'
            if fault == 'gas':changed['receipt']['effectiveGasPrice'] = '0x1'
            if fault == 'finality':changed['finalized']['number'] = '0x0'
            if fault == 'token-flow':changed['receipt']['logs'][0]['data'] = '0x'+(1).to_bytes(32,'big').hex()
            if fault == 'position-cost':
                event = changed['receipt']['logs'][-1]
                event['data'] = event['data'][:-64]+(1).to_bytes(32,'big').hex()
            if fault == 'abi-prefix':
                event = changed['receipt']['logs'][-1];event['data'] = 'zz'+event['data'][2:]
            with vault.session() as db:
                with pytest.raises(DexExecutorError):journal.check_intent(db,prepared.identifier,account_id=account,evidence=changed,now=datetime.now(timezone.utc))
        with vault.session() as db:
            journal.check_intent(db,prepared.identifier,account_id=account,evidence=proof,now=datetime.now(timezone.utc))
            original = deepcopy(journal.intent_record(db,prepared.identifier)[2])
        for key,value in [('gas_fee_wei','1'),('owned_wbnb','1'),('amount_out_units','1'),('realized_pnl_usdt','1')]:
            changed = deepcopy(original);changed['receipt'][key] = value
            with vault.session() as db:
                row = db.get(RealDexOperatorIntent,prepared.identifier);row.state_json,row.state_digest = _encoded(changed);db.commit()
            with pytest.raises(VaultError,match='DEX operator'):vault.backup(tmp_path/(key+'.db'))
            with vault.session() as db:
                row = db.get(RealDexOperatorIntent,prepared.identifier);row.state_json,row.state_digest = _encoded(original);db.commit()
        session.revoke();vault.backup(tmp_path/'restored.db')


def test_crash_after_reservation_is_read_only_on_replay_and_has_no_copyable_sending_permission(tmp_path):
    from copy import copy,deepcopy
    import pickle
    from app.live_execution.dex_operator_outbox import prepare_execution
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade)
        for clone in (copy,deepcopy,pickle.dumps):
            with pytest.raises(TypeError):clone(prepared)
        with vault.session() as db:_,terms,_ = journal.intent_record(db,prepared.identifier)
        other = RealVault(vault.path);other.unlock(PASSWORD)
        replay = prepare_execution(other,DexOperatorSession(other),identifier=prepared.identifier,account_id=account,
            grant_id=grant['id'],plan=trade,decision_ref=terms['decision_ref'],now=datetime.now(timezone.utc))
        assert replay['replayed_without_post'] and not replay['new_order_post_attempted']
        other.lock();session.revoke()


def test_v60_migration_preserves_owner_controls_and_creates_empty_operator_tables(tmp_path):
    from app.portfolio.models import RealDexControlIntent
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        with vault.session() as db:
            before = [(row.id,row.payload_json,row.state_json) for row in db.query(RealDexControlIntent)]
            db.connection().exec_driver_sql('DROP TABLE real_dex_operator_intents')
            db.connection().exec_driver_sql('DROP TABLE real_dex_operator_grants')
            db.connection().exec_driver_sql('PRAGMA user_version=60');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() ==SCHEMA_VERSION
            assert [(row.id,row.payload_json,row.state_json) for row in db.query(RealDexControlIntent)] == before
            assert db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_operator_intents').scalar_one() == 0
            assert db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_operator_grants').scalar_one() == 0
        vault.backup(tmp_path/'migrated.db')


def test_an_operator_record_cannot_be_hidden_inside_a_v60_backup(tmp_path):
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        with vault.session() as db:db.connection().exec_driver_sql('PRAGMA user_version=60');db.commit()
        with pytest.raises(VaultError,match='DEX operator'):vault.backup(tmp_path/'mistamped.db')
        with vault.session() as db:db.connection().exec_driver_sql('PRAGMA user_version=62');db.commit()
        vault.backup(tmp_path/'valid.db')


def _reserve_in_process(path,account,grant_id,trade,barrier,queue):
    """Each spawned process owns its SQLCipher engine, key and transaction."""
    import os
    vault = RealVault(path)
    try:
        vault.unlock(PASSWORD);barrier.wait(timeout=15)
        with vault.session() as db:
            result = journal.reserve_intent(db,identifier=str(uuid4()),account_id=account,grant_id=grant_id,plan=trade,
                decision_ref=keccak(text=str(uuid4())).hex(),now=datetime.now(timezone.utc))
        queue.put(('reserved',os.getpid(),result['reserved']))
    except DexExecutorError as exc:queue.put(('rejected',os.getpid(),str(exc)))
    except Exception as exc:queue.put(('unexpected',os.getpid(),type(exc).__name__))
    finally:vault.lock()


def test_two_processes_cannot_reserve_the_same_operator_nonce(tmp_path):
    import multiprocessing
    import os
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        context = multiprocessing.get_context('spawn')
        barrier,queue = context.Barrier(2),context.Queue()
        processes = [context.Process(target=_reserve_in_process,args=(vault.path,account,grant['id'],trade,barrier,queue)) for _ in range(2)]
        try:
            for process in processes:process.start()
            results = [queue.get(timeout=25) for _ in processes]
            for process in processes:process.join(timeout=5)
            assert all(process.exitcode == 0 for process in processes)
            assert sorted(value[0] for value in results) == ['rejected','reserved']
            assert len({value[1] for value in results}) == 2 and all(value[1] != os.getpid() for value in results)
            with vault.session() as db:
                assert db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_operator_intents').scalar_one() == 1
        finally:
            for process in processes:
                if process.is_alive():process.terminate();process.join(timeout=5)
            queue.close()


def test_replaced_nonce_is_never_attributed_to_the_bot_and_only_stop_may_continue(tmp_path):
    from app.live_execution.dex_trade import read_operator_nonce_observation,read_stop_plan
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade)
        replacement = {'from':meta['operator'],'to':meta['operator'],'value':'0x0','nonce':hex(trade.operator_nonce),
                       'gas':hex(21000),'gasPrice':hex(10**9)}
        rpc('eth_sendTransaction',[replacement])
        result = prepared.broadcast_once(lambda raw:rpc('eth_sendRawTransaction',['0x'+raw.hex()]),now=datetime.now(timezone.utc))
        assert result['send_result'] == 'UNKNOWN'
        observation = read_operator_nonce_observation(rpc,meta['owner'],int(deployment['nonce']),500,operator=meta['operator'],now=datetime.now(timezone.utc))
        with vault.session() as db:
            recovered = journal.recover_intent(db,prepared.identifier,account_id=account,observation=observation,now=datetime.now(timezone.utc))
            assert recovered['status'] == 'NONCE_OBSERVED' and recovered['receipt'] is None
        fresh = trade_plan(meta,rpc,int(deployment['nonce']))
        with pytest.raises(DexExecutorError):reserve(vault,account,grant,fresh)
        stop = read_stop_plan(rpc,meta['owner'],int(deployment['nonce']),500,gas=100000,gas_price=10**9,max_gas_wei=10**14,now=datetime.now(timezone.utc))
        _,halt = prepared_execution(vault,account,grant,stop,session=session)
        hashes = []
        def post(raw):
            sent = rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(sent);return sent
        halt.broadcast_once(post,now=datetime.now(timezone.utc))
        with vault.session() as db:
            journal.check_intent(db,halt.identifier,account_id=account,evidence=evidence(meta,rpc,stop,hashes[0]),now=datetime.now(timezone.utc))
            remaining = journal.grant_view(db,grant['id'],now=datetime.now(timezone.utc))
            assert remaining['status'] == 'STOPPED' and remaining['unresolved_outcomes']
            assert remaining['gas_reserved_wei'] == str(500000*10**9)
        vault.backup(tmp_path/'replaced.db')


def test_partial_cost_rounds_down_and_the_full_close_assigns_the_exact_remainder(tmp_path):
    from eth_abi import encode
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        # The favorable entry creates profit; the mock router needs reserves
        # to pay that profit on the final sale, just like a real liquidity pool.
        write(rpc,meta,meta['usdt'],'mint(address,uint256)',['address','uint256'],[meta['router'],100])
        session = None
        for buy,amount,expected in [(True,17,(25,17,None,None)),(False,7,(18,13,4,3)),(False,18,(0,0,13,5))]:
            rate = 15000 if buy else 10000
            data = keccak(text='set(uint256,bool)')[:4]+encode(['uint256','bool'],[rate,False])
            rpc('eth_sendTransaction',[{'from':meta['owner'],'to':meta['router'],'data':'0x'+data.hex(),'gas':hex(100000),'gasPrice':hex(10**9)}])
            current = trade_plan(meta,rpc,int(deployment['nonce']),buy=buy,amount=amount)
            session,prepared = prepared_execution(vault,account,grant,current,session=session);hashes = []
            def post(raw):
                sent = rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(sent);return sent
            prepared.broadcast_once(post,now=datetime.now(timezone.utc))
            with vault.session() as db:
                observed = journal.check_intent(db,prepared.identifier,account_id=account,evidence=evidence(meta,rpc,current,hashes[0]),now=datetime.now(timezone.utc))['receipt']
            actual = tuple(int(observed[key]) if observed[key] is not None else None
                for key in ('owned_wbnb','cost_usdt','assigned_cost_usdt','realized_pnl_usdt'))
            assert actual == expected
        session.revoke();vault.backup(tmp_path/'rounding.db')


def test_abandoning_a_prepared_signature_prevents_its_post_and_releases_only_its_unsent_gas_and_nonce(tmp_path):
    from app.live_execution.dex_operator_outbox import abandon_execution
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade)
        discarded = abandon_execution(vault,session,prepared.identifier,account_id=account,plan=trade,now=datetime.now(timezone.utc))
        assert discarded['status'] == 'ABANDONED_UNSENT'
        assert session.status()['gas_reserved_wei'] == '0'
        posts = []
        with pytest.raises(DexExecutorError):prepared.broadcast_once(lambda raw:posts.append(raw),now=datetime.now(timezone.utc))
        assert posts == []
        # A newly reviewed decision may use the nonce; the old continuation cannot.
        fresh = trade_plan(meta,rpc,int(deployment['nonce']))
        _,retry = prepared_execution(vault,account,grant,fresh,session=session)
        def post(raw):posts.append(raw);return rpc('eth_sendRawTransaction',['0x'+raw.hex()])
        retry.broadcast_once(post,now=datetime.now(timezone.utc))
        assert len(posts) == 1
        with pytest.raises(DexExecutorError):abandon_execution(vault,session,retry.identifier,account_id=account,plan=fresh,now=datetime.now(timezone.utc))
        session.revoke();vault.backup(tmp_path/'discarded.db')
