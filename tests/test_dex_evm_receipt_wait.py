"""The owned fixture handles a genuinely pending receipt without another send."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tests.dex_evm_fixture import evm, wait_receipt
from tests.test_dex_operator import write


def test_preparation_waits_for_actual_mining_and_never_resends_or_mines_on_its_own(tmp_path):
    with evm(tmp_path/'evm',backend='anvil')as(meta,rpc):
        rpc('miner_stop',[]);height=rpc('eth_blockNumber',[])
        pending=Event();calls=[]
        def tracked(method,params):
            result=rpc(method,params);calls.append(method)
            if method=='eth_getTransactionReceipt'and result is None:pending.set()
            return result
        with ThreadPoolExecutor(max_workers=1)as pool:
            future=pool.submit(write,tracked,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
            assert pending.wait(5)
            assert not future.done()and rpc('eth_blockNumber',[])==height
            assert calls.count('eth_sendTransaction')==1
            rpc('evm_mine',[])
            assert future.result(timeout=5)is None
        assert set(calls)=={'eth_sendTransaction','eth_getTransactionReceipt'}
        assert calls.count('eth_sendTransaction')==1


def test_fixture_timeout_keeps_the_unknown_receipt_unknown_without_resending():
    calls=[]
    def pending(method,params):calls.append((method,params));return None
    with pytest.raises(AssertionError,match='deadline'):
        wait_receipt(pending,'0x'+'00'*32,timeout=0)
    assert calls==[('eth_getTransactionReceipt',['0x'+'00'*32])]
