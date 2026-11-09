"""Policy renewal uses confirmed owned fills, not PAPER capital or reset counters."""
from contextlib import contextmanager,nullcontext
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import pytest

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_journal as bots,dex_operator_journal as operator,dex_controls as controls,dex_bot_bindings as bindings
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_operator import write
from tests.test_dex_deployments import PASSWORD


@contextmanager
def rollover_case(directory,monkeypatch,*,policy_changes=None):
    with running_case(directory,policy_changes={'max_exit_notional':str(7*10**15),**(policy_changes or {})}) as case:
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,
            wallet_address=meta['owner'],market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine()
            assert worker.run_once()['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[9950,False])
            market.bid,market.ask='1.2','1.21';clock[0]+=timedelta(seconds=1);mine()
            assert worker.run_once()['last_action']=='SELL'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            worker.stop()
            def snapshot():
                return read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,
                    candidate_operator=draft['operator_address'],now=clock[0],block_tag='finalized')
            state=snapshot();assert 0<state.owned_wbnb<state.acquired_wbnb and state.realized_loss>0
            yield case,worker,snapshot
        finally:worker.stop()


def confirm_plan(case,plan,snapshot):
    values,started,claim,connection,clock,mine,market=case
    vault,account,manager,draft,meta,rpc,deployment,binding=values;state=snapshot()
    with vault.session() as db:
        assert controls.claim_control(db,plan['id'],account_id=account,wallet_address=meta['owner'],
            operator=draft['operator_address'] if plan['action']=='CONFIGURE' else None,
            plan_digest=plan['plan_digest'],snapshot=state,now=clock[0])['claim_granted']
    tx_hash=rpc('eth_sendTransaction',[plan['transaction']]);receipt=rpc('eth_getTransactionReceipt',[tx_hash])
    assert receipt['status']=='0x1'
    block=rpc('eth_getBlockByNumber',[receipt['blockNumber'],False])
    evidence={'chain_id':'0x38','tx_hash':tx_hash,'transaction':rpc('eth_getTransactionByHash',[tx_hash]),
        'receipt':receipt,'block':block,'finalized':block,'pool':state.pool,
        'runtime':rpc('eth_getCode',[state.executor_address,block['number']])}
    with vault.session() as db:controls.report_control(db,plan['id'],account_id=account,tx_hash=tx_hash,now=clock[0])
    with vault.session() as db:return controls.check_control(db,plan['id'],account_id=account,evidence=evidence,now=clock[0])


def test_expired_policy_renewal_prepares_only_unspent_limits_and_confirms_the_exact_owner_call(tmp_path,monkeypatch):
    with rollover_case(tmp_path,monkeypatch) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        before=snapshot();root=before.policy_limits
        with vault.session() as db:budget=operator.grant_view(db,binding['grant_id'],now=clock[0])
        clock[0]=datetime.fromtimestamp(root.expires_at+1,timezone.utc);mine()
        from app.portfolio.dex_bot_rollover import prepare_rollover
        state=snapshot()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],
                wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=state,
                expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',
                max_gas_bnb='0.002',now=clock[0])
        limits=plan['limits'];summary=plan['rollover']
        assert limits['buy_budget']==str(root.buy_budget-before.buy_spent)
        assert limits['max_entries']==root.max_entries-before.entries
        assert limits['max_realized_loss']==str(root.max_realized_loss-before.realized_loss)
        assert limits['max_wbnb_acquired']==str(before.owned_wbnb+root.max_wbnb_acquired-before.acquired_wbnb)
        assert limits['max_buy_per_order']==str(min(root.max_buy_per_order,root.buy_budget-before.buy_spent))
        assert summary['gas_remaining_wei']==budget['remaining_gas_wei']
        assert summary['root_binding_id']==binding['id'] and summary['position']['owned_wbnb']==str(before.owned_wbnb)
        assert summary['consumed']['realized_loss']==str(before.realized_loss)
        assert not plan['automatic_execution']
        confirmed=confirm_plan(case,plan,snapshot)
        assert confirmed['status']=='FINALIZED' and confirmed['limits']==limits
        after=snapshot()
        assert after.policy_id==before.policy_id+1 and after.owned_wbnb==before.owned_wbnb and after.cost_usdt==before.cost_usdt
        assert after.buy_spent==after.entries==after.realized_loss==after.last_entry_at==0


def test_pending_owner_configuration_blocks_old_policy_signing_and_rearm_until_rejected(tmp_path,monkeypatch):
    from app.live_execution.dex_artifact import DexExecutorError
    from app.live_execution.dex_gas_funding import read_funding_state
    from app.portfolio.dex_bot_rollover import prepare_rollover
    with rollover_case(tmp_path,monkeypatch) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        clock[0]+=timedelta(seconds=1);mine();state=snapshot()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],
                wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=state,
                expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        args=dict(account_id=account,binding_id=binding['id'],draft_id=draft['id'],wallet_address=meta['owner'],
            snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),ttl_seconds=900,now=clock[0])
        connection.connect(request_id=str(uuid4()),**args)
        with vault.session() as db:
            assert controls.claim_control(db,plan['id'],account_id=account,wallet_address=meta['owner'],
                operator=draft['operator_address'],plan_digest=plan['plan_digest'],snapshot=state,now=clock[0])['claim_granted']
        with vault.session() as db:seed=[bar.json() for bar in bots.run_record(db,started['id'])[3]['bars']]
        with pytest.raises(DexExecutorError,match='política|renovación'):
            with vault.session() as db:
                bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                    connection=connection,snapshot=state,seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=900,
                    previous_run_id=started['id'])
        connection.revoke();request_id=str(uuid4())
        with pytest.raises(DexExecutorError,match='política|renovación'):connection.connect(request_id=request_id,**args)
        assert not connection.status(account_id=account,wallet_address=meta['owner'],now=clock[0])['connected']
        with vault.session() as db:
            rejected=controls.report_control(db,plan['id'],account_id=account,rejected=True,now=clock[0])
        assert rejected['status']=='WALLET_REJECTED'
        clock[0]+=timedelta(seconds=1);mine()
        args.update(now=clock[0],snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]))
        assert connection.connect(request_id=request_id,**args)['connected']


def test_confirmed_policy_renewal_registers_one_successor_with_remaining_gas_and_restores_it(tmp_path,monkeypatch):
    from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
    with rollover_case(tmp_path,monkeypatch) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        before=snapshot();clock[0]=datetime.fromtimestamp(before.policy_limits.expires_at+1,timezone.utc);mine()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],
                wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=snapshot(),
                expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        confirm_plan(case,plan,snapshot);state=snapshot()
        with vault.session() as db:
            renewed=apply_rollover(db,identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=state,now=clock[0])
        assert renewed['id']!=binding['id'] and renewed['root_binding_id']==binding['id']
        assert renewed['renewed_from_binding_id']==binding['id'] and renewed['status']=='READY'
        assert renewed['effective_limits']==plan['limits']
        assert renewed['gas']['gas_budget_wei']==plan['rollover']['gas_remaining_wei']
        assert not renewed['automatic_execution'] and not renewed['execution_enabled']
        with vault.session() as db:
            assert bindings.binding_view(db,binding['id'],now=clock[0])['status']=='STOPPED'
            assert operator.grant_view(db,binding['grant_id'],now=clock[0])['status']=='STOPPED'
        with vault.session() as db:
            replay=apply_rollover(db,identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=None,now=clock[0])
        assert replay['id']==renewed['id'] and replay['replayed']
        vault.backup(tmp_path/'renewed.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:recovered=bindings.binding_view(db,renewed['id'],now=clock[0])
        assert recovered['effective_limits']==plan['limits'] and recovered['root_binding_id']==binding['id']
        assert recovered['gas']==renewed['gas'] and not recovered['automatic_execution']


def test_loss_above_original_ceiling_renews_only_to_close_the_remaining_position(tmp_path,monkeypatch):
    from app.portfolio.dex_bot_rollover import prepare_rollover
    with rollover_case(tmp_path,monkeypatch,policy_changes={'max_realized_loss':'1'}) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        before=snapshot();assert before.realized_loss>before.policy_limits.max_realized_loss
        clock[0]+=timedelta(seconds=1);mine()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],
                wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=snapshot(),
                expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        assert plan['limits']['allow_sell'] and not plan['limits']['allow_buy']
        assert all(int(plan['limits'][key])==0 for key in ('buy_budget','max_buy_per_order','max_exposure','max_realized_loss','max_entries','cooldown_seconds'))
        assert int(plan['rollover']['consumed']['realized_loss'])==before.realized_loss
        assert confirm_plan(case,plan,snapshot)['status']=='FINALIZED'
        assert snapshot().policy_limits.allow_buy is False and snapshot().owned_wbnb==before.owned_wbnb


def test_an_old_review_cannot_be_claimed_after_another_permission_but_remains_valid_history(tmp_path,monkeypatch):
    from app.live_execution.dex_artifact import DexExecutorError
    from app.live_execution.dex_gas_funding import read_funding_state
    from app.portfolio.dex_bot_rollover import prepare_rollover
    with rollover_case(tmp_path,monkeypatch) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        clock[0]+=timedelta(seconds=1);mine()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],
                wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=snapshot(),
                expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        clock[0]+=timedelta(seconds=1);mine()
        connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
            wallet_address=meta['owner'],snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),ttl_seconds=900,now=clock[0])
        with vault.session() as db:
            seed=[bar.json() for bar in bots.run_record(db,started['id'])[3]['bars']]
            resumed,_=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                connection=connection,snapshot=snapshot(),seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=900,previous_run_id=started['id'])
        with vault.session() as db:bots.stop_run(db,resumed['id'],account_id=account,now=clock[0])
        connection.revoke();clock[0]+=timedelta(seconds=1);mine()
        with pytest.raises(DexExecutorError,match='último|cambi'):
            with vault.session() as db:
                controls.claim_control(db,plan['id'],account_id=account,wallet_address=meta['owner'],operator=draft['operator_address'],
                    plan_digest=plan['plan_digest'],snapshot=snapshot(),now=clock[0])
        vault.backup(tmp_path/'old-review-history.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert controls.control_record(db,plan['id'])[0].status=='PREPARED'


@pytest.mark.parametrize('stops',[1,2])
def test_owner_chain_stops_require_original_receipts_before_renewing_the_same_position(tmp_path,monkeypatch,stops):
    from app.live_execution.dex_artifact import DexExecutorError
    from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
    with rollover_case(tmp_path,monkeypatch) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        before=snapshot();stop_ids=[]
        for _ in range(stops):
            clock[0]+=timedelta(seconds=1);mine()
            with vault.session() as db:
                stop=controls.prepare_control(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],action='STOP',operator=None,limits=None,snapshot=snapshot(),
                    gas='100000',gas_price='1000000000',max_gas_bnb='0.001',now=clock[0])
            confirm_plan(case,stop,snapshot);stop_ids.append(stop['id'])
        assert snapshot().policy_id==before.policy_id+stops and int(snapshot().operator,16)==0
        clock[0]+=timedelta(seconds=1);mine()
        args=dict(identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],wallet_address=meta['owner'],
            operator_address=draft['operator_address'],snapshot=snapshot(),expires_at=int(clock[0].timestamp())+3600,
            gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        # Even rehashed state with the same observed effect cannot replace the
        # final original STOP receipt.
        from app.portfolio.models import RealDexControlIntent
        from app.portfolio.wallet_funding import _encoded,_decoded
        with vault.session() as db:
            row=db.get(RealDexControlIntent,stop_ids[-1]);raw=(row.state_json,row.state_digest,row.status)
            state=_decoded(row.state_json,row.state_digest);state['receipt']=None;state['history']=state['history'][:-1];state['status']='HASH_REPORTED'
            row.state_json,row.state_digest=_encoded(state);row.status='HASH_REPORTED';db.commit()
        with pytest.raises(DexExecutorError):
            with vault.session() as db:prepare_rollover(db,**args)
        with vault.session() as db:
            row=db.get(RealDexControlIntent,stop_ids[-1]);row.state_json,row.state_digest,row.status=raw;db.commit()
        with vault.session() as db:plan=prepare_rollover(db,**args)
        assert [item['id'] for item in plan['rollover']['policy_stops']]==stop_ids
        assert plan['rollover']['consumed']['buy_spent']==str(before.buy_spent)
        assert plan['rollover']['position']=={'owned_wbnb':str(before.owned_wbnb),'cost_usdt':str(before.cost_usdt)}
        confirm_plan(case,plan,snapshot)
        with vault.session() as db:
            renewed=apply_rollover(db,identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=snapshot(),now=clock[0])
        assert renewed['effective_limits']==plan['limits']
        vault.backup(tmp_path/'stopped-chain-renewal.db')


def test_operator_chain_stop_gas_is_consumed_and_does_not_require_a_fake_bot_decision(tmp_path,monkeypatch):
    from app.live_execution.dex_gas_funding import read_funding_state
    from app.live_execution.dex_trade import read_stop_plan
    from app.live_execution.dex_operator_outbox import prepare_execution
    from app.live_execution.dex_operator_transport import collect_operator_receipt
    from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
    with rollover_case(tmp_path,monkeypatch) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        clock[0]+=timedelta(seconds=1);mine()
        connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
            wallet_address=meta['owner'],snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),ttl_seconds=900,now=clock[0])
        stop=read_stop_plan(rpc,meta['owner'],int(deployment['nonce']),500,gas=100000,gas_price=10**9,max_gas_wei=10**14,now=clock[0])
        sent=prepare_execution(vault,connection.session,identifier=str(uuid4()),account_id=account,grant_id=binding['grant_id'],
            plan=stop,decision_ref='cd'*32,now=clock[0]);hashes=[]
        def broadcast(raw):
            tx=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(tx);return tx
        assert sent.broadcast_once(broadcast,now=clock[0])['send_result']=='HASH_MATCHED'
        proof=collect_operator_receipt(rpc,meta['owner'],int(deployment['nonce']),hashes[0])
        with vault.session() as db:
            assert operator.check_intent(db,sent.identifier,account_id=account,evidence=proof,now=clock[0])['status']=='FINALIZED'
            budget=operator.grant_view(db,binding['grant_id'],now=clock[0]);assert budget['status']=='STOPPED'
        connection.revoke();clock[0]+=timedelta(seconds=1);mine()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],wallet_address=meta['owner'],
                operator_address=draft['operator_address'],snapshot=snapshot(),expires_at=int(clock[0].timestamp())+3600,
                gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        assert plan['rollover']['policy_stops'][0]['kind']=='OPERATOR'
        assert plan['rollover']['policy_stops'][0]['id']==sent.identifier
        assert plan['rollover']['gas_remaining_wei']==budget['remaining_gas_wei']
        confirm_plan(case,plan,snapshot)
        with vault.session() as db:
            apply_rollover(db,identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=snapshot(),now=clock[0])
        vault.backup(tmp_path/'operator-stop-renewal.db')


def test_new_policy_zero_timestamp_does_not_bypass_the_original_entry_cooldown(tmp_path,monkeypatch):
    from app.live_execution.dex_gas_funding import read_funding_state
    from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
    with running_case(tmp_path,policy_changes={'cooldown_seconds':300}) as case:
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def snapshot():return read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,
            candidate_operator=draft['operator_address'],now=clock[0],block_tag='finalized')
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            last_entry=snapshot().last_entry_at;market.bid,market.ask='1.2','1.21'
            clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='SELL'
            assert worker.run_once()['last_receipt_status']=='FINALIZED' and snapshot().owned_wbnb==0
            worker.stop();clock[0]+=timedelta(seconds=1);mine()
            with vault.session() as db:
                plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],wallet_address=meta['owner'],
                    operator_address=draft['operator_address'],snapshot=snapshot(),expires_at=int(clock[0].timestamp())+3600,
                    gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
            confirm_plan(case,plan,snapshot)
            with vault.session() as db:
                renewed=apply_rollover(db,identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=snapshot(),now=clock[0])
            assert snapshot().last_entry_at==0
            connection.connect(request_id=str(uuid4()),account_id=account,binding_id=renewed['id'],draft_id=draft['id'],
                wallet_address=meta['owner'],snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),ttl_seconds=900,now=clock[0])
            with vault.session() as db:
                seed=[dict(bar) for bar in market.rows if bar['t']<=int(clock[0].timestamp())//60*60-60]
                current,new_claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=renewed['id'],wallet_address=meta['owner'],
                    connection=connection,snapshot=snapshot(),seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=900,previous_run_id=started['id'])
            worker=DexBotWorker(vault,connection,run_id=current['id'],account_id=account,claim=new_claim,wallet_address=meta['owner'],
                market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
            target=(int(clock[0].timestamp())//60+1)*60+1
            while market.rows[-1]['t']<target//60*60-60:market.rows.append({**market.rows[-1],'t':market.rows[-1]['t']+60})
            while clock[0].timestamp()<target:
                clock[0]=datetime.fromtimestamp(min(target,clock[0].timestamp()+10),timezone.utc);mine()
                assert worker.run_once()['last_action']=='WAIT'
            assert clock[0].timestamp()<last_entry+300
            with vault.session() as db:
                from app.portfolio.models import RealDexBotDecision
                from app.portfolio.wallet_funding import _decoded
                from sqlalchemy import select
                decision=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.run_id==current['id']).order_by(RealDexBotDecision.sequence_no.desc()))
                last=_decoded(decision.payload_json,decision.payload_digest)['plan']
                assert last['reason']=='ENTRY_COOLDOWN' and last['evidence']['entry_match']
                assert last['evidence']['physical_last_entry_at']=='0' and last['evidence']['effective_last_entry_at']==str(last_entry)
            assert snapshot().buy_spent==snapshot().entries==snapshot().owned_wbnb==0
            worker.stop();vault.backup(tmp_path/'renewed-cooldown.db')
        finally:worker.stop()


def test_successor_failure_rolls_back_retirement_and_each_read_rechecks_rehashed_provenance(tmp_path,monkeypatch):
    from app.live_execution.dex_artifact import DexExecutorError
    from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
    from app.portfolio.models import RealDexControlIntent
    from app.portfolio.wallet_funding import _decoded,_encoded
    with rollover_case(tmp_path,monkeypatch) as (case,worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        clock[0]+=timedelta(seconds=1);mine()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],wallet_address=meta['owner'],
                operator_address=draft['operator_address'],snapshot=snapshot(),expires_at=int(clock[0].timestamp())+3600,
                gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        confirm_plan(case,plan,snapshot)
        args=dict(identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=snapshot(),now=clock[0])
        def fail(*args,**kwargs):raise DexExecutorError('controlled failure after retirement')
        with monkeypatch.context() as patch:
            patch.setattr(operator,'create_grant',fail)
            with pytest.raises(DexExecutorError,match='controlled failure'):
                with vault.session() as db:apply_rollover(db,**args)
        with vault.session() as db:
            assert bindings.binding_record(db,binding['id'])[0].status=='READY'
            assert operator.grant_record(db,binding['grant_id'])[0].status=='ACTIVE'
            renewed=apply_rollover(db,**args)
        with vault.session() as db:
            bindings.binding_record(db,renewed['id'])
            row=db.get(RealDexControlIntent,plan['id']);terms=_decoded(row.payload_json,row.payload_digest)
            original=(row.payload_json,row.payload_digest)
            terms['bot_rollover']['gas_remaining_wei']=str(int(terms['bot_rollover']['gas_remaining_wei'])+1)
            row.payload_json,row.payload_digest=_encoded(terms)
            # No commit, new session or cache invalidator: a second read must
            # inspect the changed rows rather than trust the earlier result.
            with pytest.raises(DexExecutorError):bindings.binding_record(db,renewed['id'])
            row.payload_json,row.payload_digest=original
        vault.backup(tmp_path/'rollback-and-recheck.db')


def test_renewed_worker_resumes_the_partial_exit_without_rebuying_or_forgetting_its_cost(tmp_path,monkeypatch):
    from app.live_execution.dex_gas_funding import read_funding_state
    from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
    from tests.test_dex_operator_drafts import BACKUP_PASSWORD
    with rollover_case(tmp_path,monkeypatch) as (case,old_worker,snapshot):
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],
            password=BACKUP_PASSWORD,now=clock[0])
        before=snapshot();clock[0]=datetime.fromtimestamp(before.policy_limits.expires_at+1,timezone.utc);mine()
        with vault.session() as db:
            plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],
                wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=snapshot(),
                expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
        confirm_plan(case,plan,snapshot)
        with vault.session() as db:
            renewed=apply_rollover(db,identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=snapshot(),now=clock[0])
        manager.revoke()
        restored=manager.restore_backup(identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],
            backup=backup,password=BACKUP_PASSWORD,ttl_seconds=3600,now=clock[0])
        connection.connect(request_id=str(uuid4()),account_id=account,binding_id=renewed['id'],draft_id=restored['id'],
            wallet_address=meta['owner'],snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),
            ttl_seconds=900,now=clock[0])
        target=int(clock[0].timestamp())//60*60-60
        while market.rows[-1]['t']<target:market.rows.append({**market.rows[-1],'t':market.rows[-1]['t']+60})
        seed=[dict(bar) for bar in market.rows if bar['t']<=target]
        with vault.session() as db:
            resumed,new_claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=renewed['id'],
                wallet_address=meta['owner'],connection=connection,snapshot=snapshot(),seed=seed,now=clock[0],
                permission_mode='SESSION',requested_seconds=900,previous_run_id=started['id'])
        assert resumed['engine']=='orbis.dex.bot.long.v4' and resumed['previous_run_id']==started['id']
        assert resumed['head']['cursor']['entry_candle_t']==plan['rollover']['cursor']['entry_candle_t']
        assert resumed['head']['cursor']['closing_reason']=='TAKE_PROFIT'
        worker=DexBotWorker(vault,connection,run_id=resumed['id'],account_id=account,claim=new_claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:
            assert worker.run_once()['last_action']=='SELL'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            assert worker.run_once()['last_action']=='WAIT'
            after=snapshot();assert after.owned_wbnb==after.cost_usdt==after.buy_spent==after.entries==0
            assert after.realized_loss>0 and after.acquired_wbnb==before.owned_wbnb
            worker.stop()
            vault.backup(tmp_path/'resumed-policy.db')
            vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                recovered=bots.run_record(db,resumed['id'])
                assert recovered[0].status=='STOPPED' and recovered[1]['policy_rollover_id']==plan['id']
                assert not bots._public(*recovered[:3])['automatic_execution']
            # A second policy must subtract lifetime consumption, including the
            # carried position and the loss/gas of its final exit.
            clock[0]+=timedelta(seconds=1);mine()
            with vault.session() as db:
                gas=operator.grant_view(db,renewed['grant_id'],now=clock[0])
                again=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=resumed['id'],
                    wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=snapshot(),
                    expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])
            consumed=again['rollover']['consumed'];original=before.policy_limits
            assert again['rollover']['root_binding_id']==binding['id']
            assert again['rollover']['previous_binding_id']==renewed['id']
            assert consumed['buy_spent']==str(before.buy_spent)
            assert consumed['entries']==str(before.entries)
            assert consumed['acquired_wbnb']==str(before.acquired_wbnb)
            assert consumed['realized_loss']==str(before.realized_loss+after.realized_loss)
            assert again['rollover']['gas_remaining_wei']==gas['remaining_gas_wei']
            assert int(consumed['gas_spent'])>int(plan['rollover']['consumed']['gas_spent'])
            assert consumed['last_entry_at']==str(before.last_entry_at)
            assert again['limits']['buy_budget']==str(original.buy_budget-before.buy_spent)
            assert again['limits']['max_entries']==original.max_entries-before.entries
            assert again['limits']['max_wbnb_acquired']==str(original.max_wbnb_acquired-before.acquired_wbnb)
            assert again['limits']['max_realized_loss']==str(original.max_realized_loss-before.realized_loss-after.realized_loss)
            confirm_plan(case,again,snapshot)
            with vault.session() as db:
                third=apply_rollover(db,identifier=again['id'],account_id=account,wallet_address=meta['owner'],snapshot=snapshot(),now=clock[0])
            assert third['root_binding_id']==binding['id'] and third['renewed_from_binding_id']==renewed['id']
            vault.backup(tmp_path/'twice-renewed.db');vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                assert bindings.binding_view(db,third['id'],now=clock[0])['effective_limits']==again['limits']
        finally:worker.stop()
