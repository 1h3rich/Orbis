"""Reviewed Python policy terms execute as the exact pinned Solidity policy."""
from datetime import datetime, timezone

from eth_abi import decode
from eth_utils import keccak
import pytest

from app.live_execution.dex_artifact import creation_transaction
from app.live_execution import dex_policy
from tests.dex_evm_fixture import evm


def policy(now):
    return {'expires_at': now+3600, 'cooldown_seconds': 60, 'max_entries': 4,
        'twap_seconds': 1800, 'max_deviation_bps': 50, 'max_buy_per_order': '1000000000000000001',
        'buy_budget': '10000000000000000001', 'max_exposure': '20000000000000000001',
        'max_exit_notional': '25000000000000000000', 'max_realized_loss': '1000000000000000000',
        'max_gas_price': '1000000000', 'max_wbnb_acquired': '100000000000000000',
        'allow_buy': True, 'allow_sell': True}


def call(rpc, target, signature, output):
    data = '0x'+keccak(text=signature)[:4].hex()
    return decode(output, bytes.fromhex(rpc('eth_call', [{'to': target, 'data': data}, 'latest'])[2:]))


def test_exact_reviewed_policy_configures_the_executor_and_stop_invalidates_a_queued_renewal(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        now = int(datetime.now(timezone.utc).timestamp())
        creation_nonce = int(rpc('eth_getTransactionCount', [meta['owner'], 'pending']), 16)
        deployed = rpc('eth_sendTransaction', [creation_transaction(meta['owner'], 500, nonce=creation_nonce, gas=3500000, gas_price=1000000000)])
        address = rpc('eth_getTransactionReceipt', [deployed])['contractAddress']
        limits = dex_policy.PolicyLimits.parse(policy(now))
        nonce = creation_nonce+1
        transaction = dex_policy.configure_transaction(meta['owner'], creation_nonce, meta['operator'], 0, limits,
            nonce=nonce, gas=1000000, gas_price=1000000000, now=now, owned_wbnb=0)
        assert transaction['to'] == address and transaction['value'] == '0x0'
        tx = rpc('eth_sendTransaction', [transaction])
        assert rpc('eth_getTransactionReceipt', [tx])['status'] == '0x1'
        stored = call(rpc, address, 'policy()', [dex_policy.POLICY_ABI])[0]
        assert stored == (meta['operator'].lower(), now+3600, 60, 4, 1800, 50, 1000000000000000001,
            10000000000000000001, 20000000000000000001, 25000000000000000000, 1000000000000000000,
            1000000000, 100000000000000000, True, True)
        queued = dex_policy.configure_transaction(meta['owner'], creation_nonce, meta['operator'], 1, limits,
            nonce=nonce+2, gas=1000000, gas_price=1000000000, now=now, owned_wbnb=0)
        stopped = dex_policy.stop_transaction(meta['owner'], creation_nonce, nonce=nonce+1, gas=1000000, gas_price=1000000000)
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [stopped])])['status'] == '0x1'
        assert call(rpc, address, 'policyId()', ['uint256']) == (2,)
        assert rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [queued])])['status'] == '0x0'
        assert call(rpc, address, 'policy()', [dex_policy.POLICY_ABI])[0][0] == '0x'+'00'*20


@pytest.mark.parametrize('changes', [
    {'max_buy_per_order': 1.0}, {'buy_budget': '1e18'}, {'buy_budget': '01'}, {'max_gas_price': True},
    {'max_entries': True}, {'allow_buy': 1}, {'max_buy_per_order': str(1001*10**18)},
    {'max_exposure': str(10001*10**18)}, {'buy_budget': '1'}, {'cooldown_seconds': 59},
    {'max_deviation_bps': 501}, {'twap_seconds': 299}, {'max_wbnb_acquired': str(2**128)},
    {'max_gas_price': '1000000000001'}, {'allow_buy': False}, {'unknown': 'value'},
])
def test_invalid_or_inexact_financial_policy_never_compiles(changes):
    now = int(datetime.now(timezone.utc).timestamp())
    with pytest.raises(dex_policy.DexExecutorError):dex_policy.PolicyLimits.parse({**policy(now), **changes})


def test_close_only_policy_preserves_existing_position_and_expiry_is_exclusive():
    now = int(datetime.now(timezone.utc).timestamp())
    raw = policy(now)
    raw.update(allow_buy=False, cooldown_seconds=0, max_entries=0,
        **{field: '0' for field in ('max_buy_per_order', 'buy_budget', 'max_exposure', 'max_realized_loss')})
    limits = dex_policy.PolicyLimits.parse(raw)
    assert limits.as_dict() == raw
    limits.validate_at(now, int(raw['max_wbnb_acquired']))
    for at, owned in [(limits.expires_at, 0), (now-86401, 0), (now, limits.max_wbnb_acquired+1)]:
        with pytest.raises(dex_policy.DexExecutorError):limits.validate_at(at, owned)
