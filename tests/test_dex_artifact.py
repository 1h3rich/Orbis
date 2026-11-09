"""Canonical artifact ↔ actual creation and immutable runtime in a local EVM."""
from eth_abi import decode
from app.live_execution.dex_artifact import executor_artifact,creation_transaction,executor_address,expected_runtime
from tests.dex_evm_fixture import evm


def test_canonical_creation_and_runtime_match_the_wallet_transaction_on_an_evm(tmp_path):
    artifact=executor_artifact()
    with evm(tmp_path/'evm') as (meta,rpc):
        nonce=int(rpc('eth_getTransactionCount',[meta['owner'],'pending']),16)
        tx=creation_transaction(meta['owner'],500,nonce=nonce,gas=3500000,gas_price=1000000000)
        constructor=bytes.fromhex(tx['data'][2:])[len(artifact.creation):]
        assert decode(['address','uint24'],constructor)==(meta['owner'].lower(),500)
        sent=rpc('eth_sendTransaction',[tx]);receipt=rpc('eth_getTransactionReceipt',[sent])
        assert receipt['status']=='0x1'
        deployed=executor_address(meta['owner'],nonce)
        assert deployed==receipt['contractAddress'].lower()
        code=rpc('eth_getCode',[deployed,receipt['blockNumber']])
        assert code==expected_runtime(meta['owner'],meta['pool'],500)
        assert tx['value']=='0x0' and tx['chainId']=='0x38' and tx.get('to') is None
