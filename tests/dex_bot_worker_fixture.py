"""A tested PAPER origin and disposable EVM operator; never user funds."""
from contextlib import contextmanager
from datetime import datetime,timezone
from uuid import uuid4

from app.live_execution.dex_allowance import allowance_cap,approval_transaction
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_operator import DexOperatorSession
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_bindings as bindings,dex_bot_journal as bots
from tests.test_dex_gas_funding import case,now
from tests.test_dex_operator_connection import funded
from tests.test_dex_operator import write


@contextmanager
def running_case(directory,*,activate=True,draft_ttl_seconds=900,policy_changes=None,funding_bnb="0.001",evm_backend=None,research=False):
    with case(directory,timeframe='1m',policy_changes={'max_buy_per_order':str(10**16+1),**(policy_changes or {})},draft_ttl_seconds=draft_ttl_seconds,evm_backend=evm_backend,research=research) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        funded(values,amount_bnb=funding_bnb);nonce=int(deployment['nonce']);clock=[now()]
        def snapshot():return read_executor_state(rpc,meta['owner'],nonce,500,now=clock[0],block_tag='finalized')
        write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
        for token,recipient in [('usdt',meta['owner']),('wbnb',meta['router'])]:
            write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**20])
        for token in ('USDT','WBNB'):
            state=snapshot();cap=allowance_cap(state,token,now=clock[0])
            tx=approval_transaction(meta['owner'],nonce,token,cap,cap=cap,nonce=state.pending_nonce,gas=100000,gas_price=10**9)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
        clock[0]=datetime.fromtimestamp((int(now().timestamp())//60+1)*60-.001,timezone.utc)
        def mine():
            rpc('evm_setTime',[int(clock[0].timestamp())*1000]);rpc('evm_mine',[])
        mine();signer=DexOperatorSession(vault);connection=DexOperatorConnection(vault,manager,signer)
        try:
            if activate:
                connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],wallet_address=meta['owner'],
                    snapshot=read_funding_state(rpc,meta['owner'],nonce,500,now=clock[0]),ttl_seconds=900,now=clock[0])
            with vault.session() as db:
                seed=bindings.binding_record(db,binding['id'])[1]['origin']['seed']
                while seed[-1]['t']<int(clock[0].timestamp())//60*60-60:seed.append({**seed[-1],'t':seed[-1]['t']+60})
                if activate:
                    started,claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                        connection=connection,snapshot=snapshot(),seed=seed,now=clock[0])
                else:started,claim={'seed':seed},None
            class Market:
                rows=[*seed,{'t':seed[-1]['t']+60,'o':'97','h':'98','l':'95','c':'96','v':'10'}]
                bid,ask='1','1.01'
                def get_candles(self,symbol,*,since,before,limit,**_):
                    assert symbol=='BNB/USDT'
                    return [dict(row) for row in self.rows if since<=row['t']<before][:limit]
                def get_paper_quote(self,symbol):
                    return {'symbol':symbol,'source':'mexc-spot-public','event_time_ns':int(clock[0].timestamp())*10**9,'bid':self.bid,'ask':self.ask}
            market=Market();market.research_source=vault._test_dex_source
            yield values,started,claim,connection,clock,mine,market
        finally:connection.revoke();signer.revoke()
