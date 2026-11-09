"""Executor-specific finite approvals; no signer and no arbitrary spender."""
from datetime import datetime, timezone
import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from app.live_execution.dex_artifact import creation_transaction
from app.live_execution.dex_policy import configure_transaction, PolicyLimits
from app.live_execution.dex_state import read_executor_state
from app.live_execution import dex_allowance
from tests.dex_evm_fixture import evm
from tests.test_dex_policy import policy


def configured(meta, rpc):
    creation_nonce = int(rpc('eth_getTransactionCount', [meta['owner'], 'pending']), 16)
    sent = rpc('eth_sendTransaction', [creation_transaction(meta['owner'], 500, nonce=creation_nonce, gas=3500000, gas_price=10**9)])
    assert rpc('eth_getTransactionReceipt', [sent])['status'] == '0x1'
    now = int(datetime.now(timezone.utc).timestamp())
    tx = configure_transaction(meta['owner'], creation_nonce, meta['operator'], 0, PolicyLimits.parse(policy(now)),
        nonce=creation_nonce+1, gas=1000000, gas_price=10**9, now=now, owned_wbnb=0)
    assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
    return creation_nonce


def test_exact_allowances_target_the_executor_and_support_future_acquired_wbnb_and_revocation(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        creation_nonce = configured(meta, rpc)
        for token in ('USDT', 'WBNB'):
            now = datetime.now(timezone.utc)
            snapshot = read_executor_state(rpc, meta['owner'], creation_nonce, 500, now=now)
            cap = dex_allowance.allowance_cap(snapshot, token, now=now)
            assert cap == (snapshot.policy_limits.buy_budget if token == 'USDT' else snapshot.policy_limits.max_wbnb_acquired)
            if token == 'WBNB':assert snapshot.owned_wbnb == snapshot.balance_wbnb == 0 and cap > 0
            tx = dex_allowance.approval_transaction(meta['owner'], creation_nonce, token, cap, cap=cap,
                nonce=snapshot.pending_nonce, gas=100000, gas_price=10**9)
            spender, amount = decode(['address', 'uint256'], bytes.fromhex(tx['data'][10:]))
            assert spender == snapshot.executor_address and spender != meta['router'].lower() and amount == cap
            assert tx['to'] == meta[token.lower()].lower() and tx['value'] == '0x0'
            assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
            data = '0x'+(keccak(text='allowance(address,address)')[:4]+encode(['address', 'address'], [meta['owner'], spender])).hex()
            assert decode(['uint256'], bytes.fromhex(rpc('eth_call', [{'to': tx['to'], 'data': data}, 'latest'])[2:])) == (cap,)
            revoke = dex_allowance.approval_transaction(meta['owner'], creation_nonce, token, 0, cap=0,
                nonce=snapshot.pending_nonce+1, gas=100000, gas_price=10**9)
            assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [revoke])])['status'] == '0x1'
            assert decode(['uint256'], bytes.fromhex(rpc('eth_call', [{'to': tx['to'], 'data': data}, 'latest'])[2:])) == (0,)


@pytest.mark.parametrize('units,cap', [(True, 1), (1.0, 1), (2, 1), (2**256-1, 2**256-1), (-1, 1)])
def test_an_inexact_unlimited_or_over_cap_allowance_cannot_be_compiled(units, cap):
    with pytest.raises(dex_allowance.DexExecutorError):
        dex_allowance.approval_transaction('0x'+'12'*20, 3, 'USDT', units, cap=cap, nonce=4, gas=100000, gas_price=10**9)


def test_sales_do_not_replenish_buy_budget_or_the_acquisition_quota_and_foreign_units_stay_outside_the_cap(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        creation_nonce = configured(meta, rpc)
        def write(sender, target, signature, types, values):
            tx = {'from': sender, 'to': target, 'data': '0x'+(keccak(text=signature)[:4]+encode(types, values)).hex(),
                'gas': hex(1000000), 'gasPrice': hex(10**9), 'value': '0x0'}
            assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
        write(meta['operator'], meta['router'], 'set(uint256,bool)', ['uint256', 'bool'], [10000, False])
        for token, recipient, amount in [('usdt', meta['owner'], 10**18), ('wbnb', meta['router'], 10**18), ('wbnb', meta['owner'], 5*10**17)]:
            write(meta['operator'], meta[token], 'mint(address,uint256)', ['address', 'uint256'], [recipient, amount])
        initial = read_executor_state(rpc, meta['owner'], creation_nonce, 500, now=datetime.now(timezone.utc))
        for token in ('USDT', 'WBNB'):
            now = datetime.now(timezone.utc);state = read_executor_state(rpc, meta['owner'], creation_nonce, 500, now=now)
            cap = dex_allowance.allowance_cap(state, token, now=now)
            tx = dex_allowance.approval_transaction(meta['owner'], creation_nonce, token, cap, cap=cap,
                nonce=state.pending_nonce, gas=100000, gas_price=10**9)
            assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [tx])])['status'] == '0x1'
        deadline = int(datetime.now(timezone.utc).timestamp())+60
        write(meta['operator'], initial.executor_address, 'executeSwap(uint256,uint256,bool,uint256,uint256,uint256)',
            ['uint256', 'uint256', 'bool', 'uint256', 'uint256', 'uint256'], [1, 0, True, 10**16, 10**16, deadline])
        now = datetime.now(timezone.utc);bought = read_executor_state(rpc, meta['owner'], creation_nonce, 500, now=now)
        buy_cap = dex_allowance.allowance_cap(bought, 'USDT', now=now)
        assert buy_cap == initial.policy_limits.buy_budget-10**16
        assert dex_allowance.allowance_cap(bought, 'WBNB', now=now) == initial.policy_limits.max_wbnb_acquired
        write(meta['operator'], initial.executor_address, 'executeSwap(uint256,uint256,bool,uint256,uint256,uint256)',
            ['uint256', 'uint256', 'bool', 'uint256', 'uint256', 'uint256'], [1, 1, False, 5*10**15, 5*10**15, deadline])
        now = datetime.now(timezone.utc);sold = read_executor_state(rpc, meta['owner'], creation_nonce, 500, now=now)
        assert dex_allowance.allowance_cap(sold, 'USDT', now=now) == buy_cap
        assert dex_allowance.allowance_cap(sold, 'WBNB', now=now) == initial.policy_limits.max_wbnb_acquired-5*10**15
        assert sold.balance_wbnb-sold.owned_wbnb == 5*10**17
