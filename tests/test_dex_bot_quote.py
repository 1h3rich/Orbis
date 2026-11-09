"""One coherent snapshot fits the worker RPC budget and sizes partial exits."""
from dataclasses import replace
from datetime import datetime,timezone
import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_policy import configure_transaction,PolicyLimits
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_trade import read_trade_plan,read_exit_cap
from tests.test_dex_operator_journal import case


def now():return datetime.now(timezone.utc)


def test_simulation_reuses_the_verified_snapshot_without_repeating_all_state_calls(tmp_path):
    with case(tmp_path) as (_,_,meta,rpc,deployment,_,_):
        nonce=int(deployment['nonce']);snapshot=read_executor_state(rpc,meta['owner'],nonce,500,now=now());methods=[]
        def read(method,params):methods.append(method);return rpc(method,params)
        args=dict(buy=True,amount=10**16,slippage_bps=25,gas=500000,gas_price=10**9,max_gas_wei=10**15,now=now())
        result=read_trade_plan(read,meta['owner'],nonce,500,snapshot=snapshot,**args)
        assert result.snapshot is snapshot and len(methods)<12 and 'eth_getCode' not in methods
        for bad in (replace(snapshot,fee=3000),replace(snapshot,owner=meta['operator'].lower())):
            with pytest.raises(DexExecutorError):read_trade_plan(read,meta['owner'],nonce,500,snapshot=bad,**args)


def test_exit_cap_is_exact_and_conservative_when_the_position_exceeds_the_notional_limit(tmp_path):
    with case(tmp_path) as (_,_,meta,rpc,deployment,_,trade):
        assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[trade.transaction()])])['status']=='0x1'
        nonce=int(deployment['nonce']);snapshot=read_executor_state(rpc,meta['owner'],nonce,500,now=now())
        limits=PolicyLimits.parse({**snapshot.policy_limits.as_dict(),'max_exit_notional':str(4*10**15)})
        tx=configure_transaction(meta['owner'],nonce,meta['operator'],snapshot.policy_id,limits,nonce=snapshot.pending_nonce,
            gas=1000000,gas_price=10**9,now=int(now().timestamp()),owned_wbnb=snapshot.owned_wbnb)
        assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
        snapshot=read_executor_state(rpc,meta['owner'],nonce,500,now=now())
        cap=read_exit_cap(rpc,snapshot,now=now())
        assert cap==4*10**15<snapshot.owned_wbnb
        plan=read_trade_plan(rpc,meta['owner'],nonce,500,snapshot=snapshot,buy=False,amount=cap,slippage_bps=25,
            gas=500000,gas_price=10**9,max_gas_wei=10**15,now=now())
        assert plan.twap_out<=limits.max_exit_notional
