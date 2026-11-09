"""Current chain state, not historical deployment evidence, gates a policy."""
from datetime import datetime, timezone
from copy import deepcopy
import pytest
import httpx

from app.live_execution.dex_artifact import creation_transaction
from app.live_execution.dex_policy import PolicyLimits, configure_transaction
from app.live_execution import dex_state
from tests.dex_evm_fixture import evm
from tests.test_dex_policy import policy


def test_pinned_executor_state_is_read_at_one_block_with_no_writes_or_persisted_addresses(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = int(rpc('eth_getTransactionCount', [meta['owner'], 'pending']), 16)
        tx = rpc('eth_sendTransaction', [creation_transaction(meta['owner'], 500, nonce=nonce, gas=3500000, gas_price=1000000000)])
        assert rpc('eth_getTransactionReceipt', [tx])['status'] == '0x1'
        reads = []
        def observed(method, params):
            reads.append((method, params));return rpc(method, params)
        first = dex_state.read_executor_state(observed, meta['owner'], nonce, 500,
            candidate_operator=meta['operator'], now=datetime.now(timezone.utc))
        assert first.policy_id == 0 and first.policy_limits is None and first.pending_nonce == nonce+1
        assert first.owned_wbnb == first.cost_usdt == first.allowance_usdt == first.allowance_wbnb == 0
        assert first.position_covered and first.native_balance > 0
        now = int(datetime.now(timezone.utc).timestamp()); limits = PolicyLimits.parse(policy(now))
        configured = rpc('eth_sendTransaction', [configure_transaction(meta['owner'], nonce, meta['operator'], first.policy_id, limits,
            nonce=first.pending_nonce, gas=1000000, gas_price=1000000000, now=now, owned_wbnb=first.owned_wbnb)])
        assert rpc('eth_getTransactionReceipt', [configured])['status'] == '0x1'
        second = dex_state.read_executor_state(observed, meta['owner'], nonce, 500,
            candidate_operator=meta['operator'], now=datetime.now(timezone.utc))
        assert second.policy_id == 1 and second.policy_limits == limits and second.operator == meta['operator'].lower()
        assert first.block_number < second.block_number
        assert all(method in {'eth_chainId', 'eth_getBlockByNumber', 'eth_getCode', 'eth_call', 'eth_getTransactionCount', 'eth_getBalance'}
            for method, _ in reads)
        for address in (meta['owner'], meta['operator'], first.executor_address):assert address.lower() not in repr(second).lower()


def test_missing_wrong_or_reorganized_chain_data_never_becomes_a_zero_balance_or_a_permission(tmp_path):
    with evm(tmp_path/'evm') as (meta, rpc):
        nonce = int(rpc('eth_getTransactionCount', [meta['owner'], 'pending']), 16)
        receipt = rpc('eth_getTransactionReceipt', [rpc('eth_sendTransaction', [creation_transaction(meta['owner'], 500,
            nonce=nonce, gas=3500000, gas_price=1000000000)])])
        for fault in ('chain', 'stale', 'program', 'operator-code', 'nonce', 'reorg', 'missing'):
            def corrupted(method, params):
                value = rpc(method, params)
                if fault == 'chain' and method == 'eth_chainId':return '0x1'
                if fault == 'stale' and method == 'eth_getBlockByNumber':return {**value, 'timestamp': '0x1'}
                if fault == 'program' and method == 'eth_getCode' and params[0] == receipt['contractAddress']:return '0x00'
                if fault == 'operator-code' and method == 'eth_getCode' and params[0] == meta['operator'].lower():return '0x6000'
                if fault == 'nonce' and method == 'eth_getTransactionCount':return '0x0'
                if fault == 'reorg' and method == 'eth_getBlockByNumber' and params[0] != 'latest':return {**value, 'hash': '0x'+'ab'*32}
                if fault == 'missing' and method == 'eth_getBalance':return None
                return value
            with pytest.raises(dex_state.DexExecutorError):
                dex_state.read_executor_state(corrupted, meta['owner'], nonce, 500,
                    candidate_operator=meta['operator'], now=datetime.now(timezone.utc))


def test_public_rpc_transport_is_read_only_bounded_and_redacts_provider_errors(monkeypatch):
    monkeypatch.setenv('ORBIS_BSC_RPC_URL', 'https://rpc.invalid/private-endpoint-secret')
    requests = []
    def respond(request):
        import json
        body = json.loads(request.content);requests.append(body)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': '0x38'})
    with dex_state.BnbReadClient(transport=httpx.MockTransport(respond)) as client:
        assert client('eth_chainId', []) == '0x38'
        with pytest.raises(dex_state.DexExecutorError):client('eth_sendTransaction', [])
    assert len(requests) == 1
    def fail(request):raise httpx.ReadTimeout('private-endpoint-secret')
    with dex_state.BnbReadClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(dex_state.DexExecutorError) as caught:client('eth_chainId', [])
        assert 'secret' not in str(caught.value) and 'rpc.invalid' not in str(caught.value)
    clock = [0.0]
    with dex_state.BnbReadClient(transport=httpx.MockTransport(respond), monotonic=lambda: clock[0]) as client:
        clock[0] = 20
        with pytest.raises(dex_state.DexExecutorError):client('eth_chainId', [])


def test_rpc_accepts_larger_full_blocks_with_an_independent_size_cap():
    import json

    def response(request):
        body = json.loads(request.content)
        result = '0'*(4_000_000 if body['params'][0]=='0x2' else 600_000)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})

    with dex_state.BnbReadClient(transport=httpx.MockTransport(response)) as client:
        assert len(client('eth_getBlockByNumber',['0x1',True])) == 600_000
        for params in (['0x1',False],['0x2',True]):
            with pytest.raises(dex_state.DexExecutorError,match='lectura pública verificable'):
                client('eth_getBlockByNumber',params)
