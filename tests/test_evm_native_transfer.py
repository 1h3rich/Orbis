"""Native BNB signatures remain disjoint from zero-value executor calls."""
from copy import deepcopy
import pytest
from app.live_execution.evm_transaction import recover_wallet_transfer,recover_wallet_call
from tests.dex_evm_fixture import evm


@pytest.mark.parametrize('typed',[False,True],ids=['legacy','eip1559'])
def test_native_transfer_recovers_exact_value_recipient_sender_and_chain(tmp_path,typed):
    with evm(tmp_path/'evm') as (meta,rpc):
        value=2*10**15
        tx={'from':meta['owner'],'to':meta['operator'],'value':hex(value),'gas':'0x5208','data':'0x'}
        if typed:tx.update(type='0x2',maxFeePerGas=hex(1200000000),maxPriorityFeePerGas=hex(100000000),accessList=[])
        else:tx.update(gasPrice=hex(1000000000))
        original=rpc('eth_getTransactionByHash',[rpc('eth_sendTransaction',[tx])])
        sender,kind,gas,cap,priority=recover_wallet_transfer(original,expected_to=meta['operator'].lower(),expected_value=value)
        assert sender==meta['owner'].lower() and kind==(2 if typed else 0) and gas==21000
        assert cap==(1200000000 if typed else 1000000000)
        with pytest.raises(ValueError):recover_wallet_call(original,expected_to=meta['operator'].lower())
        for kwargs in ({'expected_value':value+1},{'expected_value':True},{'expected_to':meta['owner'].lower()}):
            with pytest.raises(ValueError):recover_wallet_transfer(original,**({'expected_to':meta['operator'].lower(),'expected_value':value}|kwargs))
        for key,changed in [('value',hex(value+1)),('input','0x00'),('chainId','0x1'),('nonce','0xffffff')]:
            wrong=deepcopy(original);wrong[key]=changed
            with pytest.raises(ValueError):recover_wallet_transfer(wrong,expected_to=meta['operator'].lower(),expected_value=value)
