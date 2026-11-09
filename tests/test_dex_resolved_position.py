"""A causal bot decision and real pending replacement fold into owned position."""
from contextlib import nullcontext
from datetime import timedelta
from copy import deepcopy
from uuid import uuid4
from sqlalchemy import select
import pytest
from eth_account import Account
from eth_utils import to_checksum_address

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_journal as bots,dex_call_resolution as resolutions,dex_operator_journal as operator
from app.portfolio.dex_operator_outcomes import operator_outcome,outcome_witnesses
from app.portfolio.dex_controls import _anchor
from app.portfolio.models import RealDexOperatorIntent
from tests.dex_bot_worker_fixture import running_case


@pytest.mark.parametrize('cancel',[False,True])
def test_causal_position_uses_verified_witness_and_keeps_the_original_grant_for_rearm(tmp_path,monkeypatch,cancel):
    with running_case(tmp_path,evm_backend='anvil',policy_changes={'max_gas_price':str(2*10**9)}) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
        result=worker.run_once();assert result['pending_receipt'] and result['last_action']=='BUY'
        with vault.session() as db:
            raw=db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==binding['grant_id']))
            parent,terms,state,source=resolutions.source_record(db,'OPERATOR',raw.id,owner=meta['owner'])
            before=(parent.payload_json,parent.state_json,parent.updated_at)
        tx=source.expected_call()
        fields={'to':to_checksum_address(draft['operator_address'] if cancel else tx['to']),
            'data':'0x' if cancel else tx['data'],'value':0,'chainId':56,'nonce':source.nonce,
            'gas':21000 if cancel else int(terms['gas'])+1000,'gasPrice':1200000000}
        with manager._copy(draft['id'],account,meta['owner'],clock[0]) as (_,key):signed=Account.sign_transaction(fields,bytes(key))
        replacement=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
        clock[0]+=timedelta(seconds=2);mine();rpc('miner_start',[1])
        evidence=collect_call_resolution(rpc,source,replacement)
        scope=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,accept_changes=True,now=clock[0])
        with pytest.raises(ValueError,match='Detén'):
            with vault.session() as db:resolutions.preview_resolution(db,'OPERATOR',parent.id,**scope)
        worker.stop();clock[0]+=timedelta(seconds=1)
        scope['now']=clock[0]
        with vault.session() as db:
            assert operator.grant_record(db,binding['grant_id'])[0].status=='ACTIVE'
            preview=resolutions.preview_resolution(db,'OPERATOR',parent.id,**scope)
        with vault.session() as db:
            done=resolutions.confirm_resolution(db,'OPERATOR',parent.id,preview_digest=preview['preview_digest'],
                source_state_digest=preview['source_state_digest'],**scope)
        observed=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
        with vault.session() as db:
            row,definition,_,_=bots.run_record(db,started['id']);witnesses=outcome_witnesses(db,[parent.id],as_of=clock[0])
            with pytest.raises(ValueError,match='witness'):
                bots._position(db,row,definition,[parent.id],clock[0])
            current,entry=bots._position(db,row,definition,[parent.id],clock[0],resolved_calls=witnesses)
            assert current=={key:int(value) for key,value in _anchor(db,observed)['counters'].items()}
            assert current['entries']==(0 if cancel else 1) and (entry is None) is cancel
            assert done['outcome']==('CANCELLED' if cancel else 'EXECUTED')
            wrong=deepcopy(witnesses);wrong[0]['payload_digest']='a'*64
            with pytest.raises(ValueError):bots._position(db,row,definition,[parent.id],clock[0],resolved_calls=wrong)
            original,_,_=operator.intent_record(db,parent.id);assert (original.payload_json,original.state_json,original.updated_at)==before
            budget=operator.grant_view(db,binding['grant_id'],now=clock[0]);assert budget['status']=='ACTIVE'
        # Explicit rearm must reuse the original lifetime budget. No new
        # transaction is allowed merely because its previous hash was replaced.
        from app.live_execution.dex_gas_funding import read_funding_state
        clock[0]+=timedelta(seconds=1);mine()
        funding=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
        connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
            wallet_address=meta['owner'],snapshot=funding,ttl_seconds=120,now=clock[0])
        with vault.session() as db:
            seed=[bar.json() for bar in bots.run_record(db,started['id'])[3]['bars']]
            resumed,next_claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                connection=connection,snapshot=funding.executor,seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=120,previous_run_id=started['id'])
            assert resumed['engine']==bots.OUTCOME_ENGINE
            child,child_terms,_,archive=bots.run_record(db,resumed['id'])
            assert child_terms['resume']['resolved_calls']==witnesses
            assert child_terms['baseline']['counters']=={key:str(value) for key,value in current.items()}
            assert operator.grant_view(db,binding['grant_id'],now=clock[0])==budget
        called=[]
        def unexpected(plan):called.append(plan);raise AssertionError('Rearm replayed an old signal')
        resumed_worker=DexBotWorker(vault,connection,run_id=resumed['id'],account_id=account,claim=next_claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=unexpected,clock=lambda:clock[0])
        assert resumed_worker.run_once()['last_action']=='WAIT' and not called
        resumed_worker.stop()
        from tests.test_dex_deployments import PASSWORD
        vault.backup(tmp_path/'resolved-position.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            assert operator_outcome(db,parent.id).outcome==done['outcome']
            assert bots.run_record(db,started['id'])[0].status=='STOPPED'
            assert bots.run_record(db,resumed['id'])[0].status=='STOPPED'
