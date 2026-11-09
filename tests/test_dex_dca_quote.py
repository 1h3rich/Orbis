"""DCA checks an actual pinned-block executor simulation in our local EVM."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal

import pytest

from app.automation.dca import validate_dca
from app.lab.engine import validate_candles
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexReferenceQuote
from app.live_execution.dex_dca_plan import DexDcaCursor,DexDcaLimits,DexDcaExecutionQuote,plan_dca,validate_execution_quote
from app.live_execution.dex_policy import configure_transaction
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_trade import read_trade_plan
from tests.dex_evm_fixture import evm
from tests.test_dex_operator import ready

UNIT=10**18


@pytest.mark.parametrize('entry,accepted',[('0.005',True),('0.00605',False)])
def test_simulated_dex_output_must_fit_real_exposure_without_adopting_foreign_wallet_units(tmp_path,entry,accepted):
    with evm(tmp_path/'evm') as (meta,rpc):
        nonce=ready(meta,rpc);now=datetime.now(timezone.utc)
        original=read_executor_state(rpc,meta['owner'],nonce,500,now=now)
        limits=replace(original.policy_limits,allow_sell=False,expires_at=int(now.timestamp())+86400)
        tx=configure_transaction(meta['owner'],nonce,meta['operator'],original.policy_id,limits,
            nonce=original.pending_nonce,gas=1000000,gas_price=10**9,now=int(now.timestamp()),owned_wbnb=0)
        assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
        boundary=int(now.timestamp())//86400*86400
        # Enter a fresh daily window without depending on the host's UTC hour.
        at=(now+timedelta(seconds=2) if now.timestamp()-boundary<898
            else datetime.fromtimestamp(boundary+86401,timezone.utc))
        boundary=int(at.timestamp())//86400*86400
        rpc('evm_setTime',[int(at.timestamp()*1000)]);rpc('evm_mine',[])
        state=read_executor_state(rpc,meta['owner'],nonce,500,now=at)
        bars=validate_candles([{'t':boundary+(i-4)*86400,'o':'1','h':'1.1','l':'0.9','c':'1','v':'10'}
            for i in range(4)],'1d',now=int(at.timestamp()))[0]
        quote=DexReferenceQuote(Decimal('1'),Decimal('1.01'),at,at)
        rule=validate_dca({'frequency':'monthly','amount':'0.01','budget_total':'1000','monthly_limit':'0.025',
            'max_exposure':'0.1','fee_rate':'0.01','slippage_rate':'0.01'})
        parameters=dict(definition=rule,candles=bars,cursor=DexDcaCursor(bars[-2].t,at-timedelta(seconds=1),'1'),
            snapshot=state,quote=quote,limits=DexDcaLimits(int(Decimal(entry)*UNIT),6*10**16,25*10**15),
            attempts=(),skip_through_t=bars[-2].t,now=at)
        decision=plan_dca(**parameters);assert decision.action=='BUY'
        assert state.owned_wbnb==0 and state.balance_wbnb==UNIT//2
        calls=[]
        def observed(method,params):calls.append(method);return rpc(method,params)
        execution=read_trade_plan(observed,meta['owner'],nonce,500,buy=True,amount=decision.amount_units,
            slippage_bps=25,gas=500000,gas_price=10**9,max_gas_wei=10**15,now=at,snapshot=state)
        checked=DexDcaExecutionQuote(execution.buy,execution.amount,execution.quote_out,execution.deadline,execution.observed_at)
        if accepted:assert validate_execution_quote(checked,**parameters).json()==decision.json()
        else:
            with pytest.raises(DexExecutorError,match='cotización'):validate_execution_quote(checked,**parameters)
        after=read_executor_state(rpc,meta['owner'],nonce,500,now=at)
        assert after.next_nonce==after.entries==after.buy_spent==after.owned_wbnb==0
        assert all(method not in ('eth_sendTransaction','eth_sendRawTransaction') for method in calls)
