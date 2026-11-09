"""Tested DCA origin and disposable EVM, ready just before a daily close."""
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from uuid import uuid4

from app.live_execution.dex_allowance import allowance_cap,approval_transaction
from app.live_execution.dex_bot_origin import prepare_origin
from app.live_execution.dex_bot_source import DexBotSources
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_operator import DexOperatorSession
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from app.live_execution.dex_policy import configure_transaction
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_bindings as bindings,dex_bot_journal as bots
from tests.dex_bot_source_fixture import DcaSourceCase
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup
from tests.test_dex_allowance_journal import ready
from tests.test_dex_operator_drafts import BACKUP_PASSWORD
from tests.test_dex_operator_connection import funded
from tests.test_dex_operator import write


@contextmanager
def dca_running_case(directory,*,activate=True,evm_backend=None,policy_changes=None,research=False,average_rule=None,volume=False):
    def now():return datetime.now(timezone.utc)
    source_path=directory/'source';source_path.mkdir()
    source=DcaSourceCase(source_path,research=research or volume,average_rule=average_rule,volume=volume)
    vault,account,start=setup(directory);manager=DexOperatorDrafts(vault)
    connection=signer=None
    try:
        with evm(directory/'evm',backend=evm_backend) as (meta,rpc):
            draft=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],ttl_seconds=86400,now=now())
            args=dict(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],now=now())
            backup=manager.export_backup(**args,password=BACKUP_PASSWORD)
            manager.verify_backup(**args,password=BACKUP_PASSWORD,backup=backup)
            deployment=ready(vault,account,start,meta,rpc);nonce=int(deployment['nonce'])
            state=read_executor_state(rpc,meta['owner'],nonce,500,now=now())
            limits=state.policy_limits.as_dict()|{'expires_at':int(now().timestamp())+86400,'allow_sell':False,
                'max_buy_per_order':str(10**16),'buy_budget':str(6*10**16),'max_exposure':str(25*10**15),'max_realized_loss':str(5*10**15)}
            if policy_changes:limits.update(policy_changes)
            if evm_backend=='anvil':
                # Anvil deliberately starts one second behind wall time. The
                # contract's <=24h cap is relative to its mined block clock.
                limits['expires_at']=min(limits['expires_at'],int(state.block_at.timestamp())+86400)
            from app.live_execution.dex_policy import PolicyLimits
            tx=configure_transaction(meta['owner'],nonce,draft['operator_address'],state.policy_id,PolicyLimits.parse(limits),
                nonce=state.pending_nonce,gas=1000000,gas_price=10**9,now=int(now().timestamp()),owned_wbnb=0)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
            sources=DexBotSources(dca_paper=source.paper)
            origin=prepare_origin(sources,kind='DCA',session_id=source.session['id'],
                **({'research_validation_id':source.validation['id']} if research or volume else {}),
                entry_usdt='0.01',budget_usdt='0.06',exposure_usdt='0.025',loss_usdt='0.005',wbnb_quota='1',monthly_usdt='0.04',now=now())
            with vault.session() as db:
                binding=bindings.create_binding(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],prepared=origin,snapshot=read_executor_state(rpc,meta['owner'],nonce,500,now=now(),block_tag='finalized'),
                    gas_budget_wei=2*10**15,now=now())
            values=(vault,account,manager,draft,meta,rpc,deployment,binding);funded(values)
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
            for token,recipient in [('usdt',meta['owner']),('wbnb',meta['router']),('wbnb',meta['owner'])]:
                write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**20])
            state=read_executor_state(rpc,meta['owner'],nonce,500,now=now());cap=allowance_cap(state,'USDT',now=now())
            tx=approval_transaction(meta['owner'],nonce,'USDT',cap,cap=cap,nonce=state.pending_nonce,gas=100000,gas_price=10**9)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
            clock=[datetime.fromtimestamp((int(now().timestamp())//86400+1)*86400-.001,timezone.utc)]
            def mine():
                # Sending may mine ahead of our controlled local clock. Never
                # rewind the owned chain behind a receipt already finalized.
                head=rpc('eth_getBlockByNumber',['latest',False])
                stamp=max(int(clock[0].timestamp()*1000),(int(head['timestamp'],16)+1)*1000)
                if evm_backend=='anvil':rpc('evm_setNextBlockTimestamp',[stamp//1000])
                else:rpc('evm_setTime',[stamp])
                rpc('evm_mine',[])
            mine();signer=DexOperatorSession(vault);connection=DexOperatorConnection(vault,manager,signer)
            connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],wallet_address=meta['owner'],
                snapshot=read_funding_state(rpc,meta['owner'],nonce,500,now=clock[0]),ttl_seconds=900,now=clock[0])
            with vault.session() as db:
                seed=bindings.binding_record(db,binding['id'])[1]['origin']['seed']
            started=claim=None
            if activate:
                state=read_executor_state(rpc,meta['owner'],nonce,500,now=clock[0],block_tag='finalized')
                with vault.session() as db:
                    started,claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                        connection=connection,snapshot=state,seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=900,
                        research_check=origin.research if volume else None)
            class Market:
                rows=[*seed,{'t':seed[-1]['t']+86400,'o':'97','h':'98','l':'79','c':'80','v':'20' if volume else '10'}]
                requests=[]
                def get_candles(self,symbol,*,since,limit,**_):
                    self.requests.append((symbol,since,limit))
                    return [dict(row) for row in self.rows if row['t']>=since][:limit]
                def get_paper_quote(self,symbol):return {'symbol':symbol,'source':'mexc-spot-public','event_time_ns':int(clock[0].timestamp()*10**9),'bid':'1','ask':'1.01'}
            market=Market();market.research_source=source
            yield values,started,claim,connection,clock,mine,market
    finally:
        if connection:connection.revoke()
        if signer:signer.revoke()
        manager.revoke();source.close();vault.lock()
