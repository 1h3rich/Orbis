"""Dedicated RAM operator signs exact executor calls on an isolated real EVM."""
from datetime import datetime, timedelta, timezone

from eth_abi import encode, decode
from eth_account import Account
from eth_utils import keccak
import pytest

from app.live_execution import dex_operator as operator
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_policy import configure_transaction, PolicyLimits
from app.live_execution.dex_allowance import allowance_cap, approval_transaction
from tests.dex_evm_fixture import evm, wait_receipt
from tests.test_dex_allowance import configured
from tests.test_dex_deployments import setup, PASSWORD


KEY = bytes.fromhex('22'*32)  # Ganache-only dedicated fixture account.


def write(rpc, meta, target, signature, types, values):
    tx = {'from': meta['operator'], 'to': target, 'data': '0x'+(keccak(text=signature)[:4]+encode(types, values)).hex(),
          'gas': hex(1000000), 'gasPrice': hex(10**9), 'value': '0x0'}
    sent=rpc('eth_sendTransaction',[tx])
    assert wait_receipt(rpc,sent)['status']=='0x1'


def ready(meta, rpc):
    nonce = configured(meta, rpc)
    write(rpc, meta, meta['router'], 'set(uint256,bool)', ['uint256', 'bool'], [10000, False])
    for token, recipient, amount in [('usdt', meta['owner'], 10**18), ('wbnb', meta['router'], 10**18),
                                     ('wbnb', meta['owner'], 5*10**17)]:
        write(rpc, meta, meta[token], 'mint(address,uint256)', ['address', 'uint256'], [recipient, amount])
    for token in ('USDT', 'WBNB'):
        now = datetime.now(timezone.utc)
        state = read_executor_state(rpc, meta['owner'], nonce, 500, now=now)
        cap = allowance_cap(state, token, now=now)
        tx = approval_transaction(meta['owner'], nonce, token, cap, cap=cap,
            nonce=state.pending_nonce, gas=100000, gas_price=10**9)
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
    return nonce


def plan(rpc, meta, nonce, *, buy=True, amount=10**16, **changes):
    return operator.read_trade_plan(rpc, meta['owner'], nonce, 500, buy=buy, amount=amount,
        slippage_bps=25, gas=500000, gas_price=10**9, max_gas_wei=10**15,
        now=datetime.now(timezone.utc), **changes)


def connect(vault, trade, *, clock=None, **changes):
    session = operator.DexOperatorSession(vault, **({'monotonic': clock} if clock else {}))
    parameters = dict(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120, gas_budget_wei=2*10**15,
                      backup_confirmed=True, now=datetime.now(timezone.utc))
    parameters.update(changes)
    session.connect(**parameters)
    return session


def test_signed_buy_partial_and_full_sale_return_tokens_to_the_owner_and_leave_foreign_units(tmp_path):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc)
        buy = plan(rpc, meta, nonce)
        before = buy.snapshot
        session = connect(vault, buy)
        for direction, units in [(True, 10**16), (False, 4*10**15), (False, 6*10**15)]:
            trade = plan(rpc, meta, nonce, buy=direction, amount=units)
            signed = session.sign_swap(trade, now=datetime.now(timezone.utc))
            assert Account.recover_transaction(signed.raw_transaction).lower() == meta['operator'].lower()
            assert '0x'+keccak(signed.raw_transaction).hex() == signed.tx_hash
            sent = rpc('eth_sendRawTransaction', ['0x'+signed.raw_transaction.hex()])
            assert sent == signed.tx_hash and rpc('eth_getTransactionReceipt', [sent])['status'] == '0x1'
            tx = rpc('eth_getTransactionByHash', [sent])
            assert tx['to'].lower() == before.executor_address and int(tx['value'], 16) == 0
            args = decode(['uint256','uint256','bool','uint256','uint256','uint256'], bytes.fromhex(tx['input'][10:]))
            assert args[:4] == (before.policy_id, trade.snapshot.next_nonce, direction, units)
        after = read_executor_state(rpc, meta['owner'], nonce, 500, now=datetime.now(timezone.utc))
        assert after.balance_usdt == before.balance_usdt and after.balance_wbnb == before.balance_wbnb
        assert after.owned_wbnb == after.cost_usdt == 0 and after.balance_wbnb == 5*10**17
        assert after.buy_spent == 10**16 and after.next_nonce == 3
        assert session.status()['gas_reserved_wei'] == str(3*500000*10**9)
        assert session.status()['automatic_execution'] is False
        session.revoke()
    vault.lock()


def test_quote_is_a_readonly_whole_executor_simulation_on_one_block_and_has_two_price_limits(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);calls = []
        def observed(method, params):
            calls.append((method, params));return rpc(method, params)
        trade = plan(observed, meta, nonce)
        assert len(calls) <= 32 and all(method in operator.BnbReadClient.METHODS for method, _ in calls)
        assert trade.quote_out == trade.twap_out == 10**16
        assert trade.minimum == 10**16*9975//10000
        assert trade.minimum >= trade.twap_out*(10000-trade.snapshot.policy_limits.max_deviation_bps)//10000
        simulated = [params for method, params in calls if method == 'eth_call' and params[0].get('from')]
        assert len(simulated) == 1 and simulated[0][0]['from'] == meta['operator'].lower()
        assert simulated[0][1] == hex(trade.snapshot.block_number)
        unchanged = read_executor_state(rpc, meta['owner'], nonce, 500, now=datetime.now(timezone.utc))
        assert unchanged.next_nonce == unchanged.buy_spent == 0


def test_one_signature_reserves_the_nonce_and_gas_even_without_a_broadcast(tmp_path):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce)
        session = connect(vault, trade, gas_budget_wei=600000*10**9)
        signed = session.sign_swap(trade, now=datetime.now(timezone.utc))
        assert KEY.hex() not in repr(session) and signed.raw_transaction.hex() not in repr(signed)
        with pytest.raises(operator.DexExecutorError):session.sign_swap(trade, now=datetime.now(timezone.utc))
        assert session.status()['gas_reserved_wei'] == str(500000*10**9)
        assert session.status()['remaining_gas_wei'] == str(100000*10**9)
        assert set(session.status()) == {'connected','credential_storage','automatic_execution','remaining_seconds',
                                         'gas_budget_wei','gas_reserved_wei','remaining_gas_wei','stop_reserve_wei'}
        session.revoke()
    vault.lock()


@pytest.mark.parametrize('changed', [
    {'buy': 1}, {'amount': True}, {'amount': 1.0}, {'amount': 2**128},
    {'slippage_bps': True}, {'slippage_bps': 501}, {'gas_price': True},
    {'max_gas_wei': 1}, {'gas': 2000001}, {'ttl_seconds': 121},
])
def test_inexact_or_unbounded_trade_inputs_are_rejected_before_simulating(changed):
    args = dict(buy=True, amount=1, slippage_bps=25, gas=500000, gas_price=10**9,
                max_gas_wei=10**15, ttl_seconds=60)
    args.update(changed)
    # Invalid types and absolute limits must fail before any RPC.
    calls = []
    def no_network(method, params):
        calls.append(method);raise AssertionError('Invalid input reached the network')
    with pytest.raises(operator.DexExecutorError):
        operator.read_trade_plan(no_network, '0x'+'12'*20, 0, 500, now=datetime.now(timezone.utc), **args)
    assert calls == []


def test_pending_operator_nonce_and_inconsistent_final_block_are_not_signable(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc)
        def pending(method, params):
            result = rpc(method, params)
            if method == 'eth_getTransactionCount' and params == [meta['operator'].lower(), 'pending']:
                return hex(int(result, 16)+1)
            return result
        with pytest.raises(operator.DexExecutorError):plan(pending, meta, nonce)
        repeated = 0
        def forked(method, params):
            nonlocal repeated
            result = rpc(method, params)
            if method == 'eth_getBlockByNumber' and params[0] != 'latest':
                repeated += 1
                if repeated == 2:return {**result, 'hash':'0x'+'ab'*32}
            return result
        with pytest.raises(operator.DexExecutorError):plan(forked, meta, nonce)


def test_operator_key_expiry_lock_generation_and_local_revoke_remove_signing_authority(tmp_path):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce);tick = [100.]
        for change in [dict(private_key=bytes.fromhex('11'*32)), dict(private_key=bytes(32)),
                       dict(private_key=KEY.hex()), dict(backup_confirmed=False), dict(ttl_seconds=True),
                       dict(ttl_seconds=86401), dict(gas_budget_wei=10**18)]:
            with pytest.raises(operator.DexExecutorError):connect(vault, trade, **change)
        session = connect(vault, trade, clock=lambda:tick[0], ttl_seconds=2)
        tick[0] = 102.
        assert not session.status()['connected']
        with pytest.raises(operator.DexExecutorError):session.sign_swap(trade, now=datetime.now(timezone.utc))
        session = connect(vault, trade)
        vault.lock();vault.unlock(PASSWORD)
        with pytest.raises(operator.DexExecutorError):session.sign_swap(trade, now=datetime.now(timezone.utc))
        assert not session.status()['connected']
        session = connect(vault, trade)
        session.revoke()
        with pytest.raises(operator.DexExecutorError):session.sign_swap(trade, now=datetime.now(timezone.utc))
    vault.lock()


def test_changed_policy_and_stale_plan_require_a_new_lease_not_silent_renewal(tmp_path):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce);session = connect(vault, trade)
        with pytest.raises(operator.DexExecutorError):session.sign_swap(trade, now=trade.observed_at+timedelta(seconds=16))
        now = datetime.now(timezone.utc)
        tx = configure_transaction(meta['owner'], nonce, meta['operator'], trade.snapshot.policy_id,
            trade.snapshot.policy_limits, nonce=trade.snapshot.pending_nonce, gas=1000000, gas_price=10**9,
            now=int(now.timestamp()), owned_wbnb=0)
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
        changed = plan(rpc, meta, nonce)
        with pytest.raises(operator.DexExecutorError):session.sign_swap(changed, now=datetime.now(timezone.utc))
        assert not session.status()['connected']
    vault.lock()


def test_operator_stop_uses_reserved_gas_and_is_not_a_local_only_stop(tmp_path):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce)
        session = connect(vault, trade, gas_budget_wei=600000*10**9)
        signed = session.sign_swap(trade, now=datetime.now(timezone.utc))
        rpc('eth_sendRawTransaction', ['0x'+signed.raw_transaction.hex()])
        stop = operator.read_stop_plan(rpc, meta['owner'], nonce, 500, gas=100000, gas_price=10**9,
            max_gas_wei=10**14, now=datetime.now(timezone.utc))
        halted = session.sign_stop(stop, now=datetime.now(timezone.utc))
        assert not session.status()['connected']
        # Signing/local revocation is not evidence of an on-chain STOP.
        before_send = read_executor_state(rpc, meta['owner'], nonce, 500, now=datetime.now(timezone.utc))
        assert before_send.operator == meta['operator'].lower()
        sent = rpc('eth_sendRawTransaction', ['0x'+halted.raw_transaction.hex()])
        assert rpc('eth_getTransactionReceipt', [sent])['status'] == '0x1'
        stopped = read_executor_state(rpc, meta['owner'], nonce, 500, now=datetime.now(timezone.utc))
        assert stopped.operator == '0x'+'00'*20 and stopped.policy_id == trade.snapshot.policy_id+1
        assert stopped.owned_wbnb == 10**16 and stopped.balance_wbnb == 5*10**17+10**16
    vault.lock()


def test_operator_native_balance_must_cover_the_swap_and_the_stop_reserve(tmp_path):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc)
        rpc('evm_setAccountBalance', [meta['operator'], hex(500000*10**9)])
        trade = plan(rpc, meta, nonce)
        session = connect(vault, trade)
        with pytest.raises(operator.DexExecutorError):session.sign_swap(trade, now=datetime.now(timezone.utc))
        session.revoke()
    vault.lock()


def test_an_already_firing_expiry_timer_cannot_revoke_a_later_connection(tmp_path, monkeypatch):
    class Timer:
        timers = []
        def __init__(self, seconds, callback, args=()):
            self.callback, self.args = callback, args
            self.timers.append(self)
        def start(self):pass
        def cancel(self):pass
        def fire(self):self.callback(*self.args)
    monkeypatch.setattr(operator, 'Timer', Timer)
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce)
        session = connect(vault, trade);old = Timer.timers[-1]
        session.connect(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120, gas_budget_wei=2*10**15,
                        backup_confirmed=True, now=datetime.now(timezone.utc))
        old.fire()
        assert session.status()['connected']
        session.revoke()
    vault.lock()


@pytest.mark.parametrize('action', ['lock','protect_view','unlock'])
def test_application_access_transitions_drop_the_operator_key(tmp_path, action):
    from app.portfolio.api import RealAccess
    vault, _, _ = setup(tmp_path);vault.lock()
    access = RealAccess(vault.path)
    access.unlock(PASSWORD)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce)
        session = access._dex_operator
        session.connect(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120, gas_budget_wei=2*10**15,
                        backup_confirmed=True, now=datetime.now(timezone.utc))
        key_buffer = session._lease.key
        getattr(access, action)(*([PASSWORD] if action == 'unlock' else []))
        assert not session.status()['connected'] and key_buffer == bytearray(32)
    access.lock()


def test_failure_to_start_expiry_timer_cannot_leave_an_unmanaged_private_key(tmp_path, monkeypatch):
    buffers = []
    class BrokenTimer:
        def __init__(self, seconds, callback, args=()):buffers.append(args[0].key)
        def start(self):raise RuntimeError('No thread available')
        def cancel(self):pass
    monkeypatch.setattr(operator, 'Timer', BrokenTimer)
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce)
        session = operator.DexOperatorSession(vault)
        with pytest.raises(operator.DexExecutorError):
            session.connect(private_key=KEY, snapshot=trade.snapshot, ttl_seconds=120, gas_budget_wei=2*10**15,
                            backup_confirmed=True, now=datetime.now(timezone.utc))
        assert not session.status()['connected'] and buffers == [bytearray(32)]
    vault.lock()


@pytest.mark.parametrize('stage', ['twap','simulation'])
def test_non_hex_abi_prefix_is_rejected_even_with_a_valid_uint256_suffix(tmp_path, stage):
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc)
        def malformed(method, params):
            value = rpc(method, params)
            if method == 'eth_call' and params[0].get('to'):
                data = params[0]['data']
                matches = (data.startswith('0x'+keccak(text='quoteTwap(bool,uint128)')[:4].hex()) if stage == 'twap'
                           else bool(params[0].get('from')))
                if matches:return 'zz'+value[2:]
            return value
        with pytest.raises(operator.DexExecutorError):plan(malformed, meta, nonce)


def test_actual_rpc_refuses_missing_permission_and_excessive_price_impact_then_quotes_a_bounded_trade(tmp_path, monkeypatch):
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc)
        monkeypatch.setenv('ORBIS_BSC_RPC_URL', meta['rpc'])
        def public_plan():
            with operator.BnbReadClient() as public:return plan(public, meta, nonce)
        write(rpc, meta, meta['router'], 'set(uint256,bool)', ['uint256', 'bool'], [9900, False])
        with pytest.raises(operator.DexExecutorError):public_plan()
        write(rpc, meta, meta['router'], 'set(uint256,bool)', ['uint256', 'bool'], [9950, False])
        bounded = public_plan()
        assert bounded.quote_out == bounded.minimum == 10**16*9950//10000
        state = bounded.snapshot
        revoke = approval_transaction(meta['owner'], nonce, 'USDT', 0, cap=0,
            nonce=state.pending_nonce, gas=100000, gas_price=10**9)
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [revoke])])['status'] == '0x1'
        with pytest.raises(operator.DexExecutorError):public_plan()


def test_external_position_change_blocks_swaps_but_leaves_operator_stop_available(tmp_path, monkeypatch):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce);session = connect(vault, trade)
        signed = session.sign_swap(trade, now=datetime.now(timezone.utc))
        rpc('eth_sendRawTransaction', ['0x'+signed.raw_transaction.hex()])
        # Owner moves WBNB independently, leaving less than the bot's position.
        data = keccak(text='transfer(address,uint256)')[:4]+encode(['address','uint256'], [meta['operator'], 505*10**15])
        tx = {'from':meta['owner'], 'to':meta['wbnb'], 'data':'0x'+data.hex(), 'gas':hex(100000), 'gasPrice':hex(10**9)}
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
        monkeypatch.setenv('ORBIS_BSC_RPC_URL', meta['rpc'])
        with operator.BnbReadClient() as public:
            with pytest.raises(operator.DexExecutorError):plan(public, meta, nonce, buy=False, amount=10**16)
        stop = operator.read_stop_plan(rpc, meta['owner'], nonce, 500, gas=100000, gas_price=10**9,
            max_gas_wei=10**14, now=datetime.now(timezone.utc))
        halted = session.sign_stop(stop, now=datetime.now(timezone.utc))
        sent = rpc('eth_sendRawTransaction', ['0x'+halted.raw_transaction.hex()])
        assert rpc('eth_getTransactionReceipt', [sent])['status'] == '0x1'
    vault.lock()


def test_signature_history_capacity_blocks_new_swaps_but_preserves_reserved_operator_stop(tmp_path):
    vault, _, _ = setup(tmp_path)
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = ready(meta, rpc);trade = plan(rpc, meta, nonce)
        session = connect(vault, trade)
        # Simulate a long-lived process at its retained-history bound, without
        # spending gas on ten thousand unrelated fixture transactions.
        session._used_nonces = {(meta['operator'].lower(), 100000+index):(0,0) for index in range(10000)}
        with pytest.raises(operator.DexExecutorError):session.sign_swap(trade, now=datetime.now(timezone.utc))
        stop = operator.read_stop_plan(rpc, meta['owner'], nonce, 500, gas=100000, gas_price=10**9,
            max_gas_wei=10**14, now=datetime.now(timezone.utc))
        signed = session.sign_stop(stop, now=datetime.now(timezone.utc))
        sent = rpc('eth_sendRawTransaction', ['0x'+signed.raw_transaction.hex()])
        assert rpc('eth_getTransactionReceipt', [sent])['status'] == '0x1'
        final = read_executor_state(rpc, meta['owner'], nonce, 500, now=datetime.now(timezone.utc))
        assert final.operator == '0x'+'00'*20 and final.policy_id == trade.snapshot.policy_id+1
        assert not session.status()['connected']
    vault.lock()
