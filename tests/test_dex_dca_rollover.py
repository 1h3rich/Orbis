"""A new on-chain policy preserves DCA lifetime authority and causal keys."""
from contextlib import nullcontext
from datetime import timedelta
from uuid import uuid4

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_journal as bots
from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
from tests.dex_dca_worker_fixture import dca_running_case
from tests.test_dex_bot_rollover import confirm_plan
from tests.test_dex_deployments import PASSWORD


def test_two_dca_policy_renewals_preserve_root_budget_month_keys_reference_and_cooldown(tmp_path,monkeypatch):
    with dca_running_case(tmp_path) as case:
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def snapshot():return read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,
            candidate_operator=draft['operator_address'],now=clock[0],block_tag='finalized')
        def make_worker(run,proof):return DexBotWorker(vault,connection,run_id=run['id'],account_id=account,claim=proof,
            wallet_address=meta['owner'],market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        worker=make_worker(started,claim)
        try:
            clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            current=started;root_limits=None
            for revision in (1,2):
                worker.stop();clock[0]+=timedelta(seconds=1);mine();before=snapshot()
                with vault.session() as db:
                    old=bots.run_record(db,current['id']);seed=[bar.json() for bar in old[3]['bars']]
                    if root_limits is None:root_limits=old[1]['dca']['limits']
                    plan=prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=current['id'],
                        wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=before,
                        expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',
                        max_gas_bnb='0.002',now=clock[0])
                assert plan['limits']['allow_buy'] and not plan['limits']['allow_sell']
                assert plan['limits']['buy_budget']==str(6*10**16-revision*10**16)
                assert plan['rollover']['consumed']['buy_spent']==str(revision*10**16)
                assert plan['rollover']['root_binding_id']==binding['id']
                assert plan['rollover']['cursor']['reference']=='97'
                confirm_plan(case,plan,snapshot);after=snapshot()
                assert after.buy_spent==after.entries==after.last_entry_at==0
                assert after.owned_wbnb==revision*10**16
                with vault.session() as db:
                    renewed=apply_rollover(db,identifier=plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=after,now=clock[0])
                funding=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
                connection.connect(request_id=str(uuid4()),account_id=account,binding_id=renewed['id'],draft_id=draft['id'],
                    wallet_address=meta['owner'],snapshot=funding,ttl_seconds=900,now=clock[0])
                with vault.session() as db:
                    current,proof=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=renewed['id'],
                        wallet_address=meta['owner'],connection=connection,snapshot=after,seed=seed,now=clock[0],
                        permission_mode='SESSION',requested_seconds=900,previous_run_id=current['id'])
                    recovered=bots.run_record(db,current['id'])
                    assert recovered[1]['dca']['limits']==root_limits
                    assert recovered[3]['dca_projection'].total_spent_units==revision*10**16
                    assert recovered[3]['dca_projection'].monthly_spent_units=={clock[0].strftime('%Y-%m'):revision*10**16}
                    assert len(recovered[3]['dca_projection'].consumed_keys)==revision
                worker=make_worker(current,proof)
                assert worker.run_once()['last_action']=='WAIT'  # CONFIGURE must not erase the last fill's cooldown.
                if revision==1:
                    for _ in range(7):
                        clock[0]+=timedelta(seconds=10);mine();status=worker.run_once()
                        if status['last_action']=='BUY':break
                    assert status['last_action']=='BUY'
                    assert worker.run_once()['last_receipt_status']=='FINALIZED'
                    assert worker.run_once()['last_action']=='WAIT'
            for _ in range(7):clock[0]+=timedelta(seconds=10);mine();assert worker.run_once()['last_action']=='WAIT'
            from app.live_execution.dex_executions import read_executions
            with vault.session() as db:
                root_fills=read_executions(db,account_id=account,run_id=started['id'])
                continued_fills=read_executions(db,account_id=account,run_id=current['id'])
            assert root_fills['total']==1 and continued_fills['total']==2
            assert continued_fills['root_binding_id']==binding['id']
            assert len(continued_fills['family_run_ids'])==3
            assert continued_fills['executions'][0]==root_fills['executions'][0]
            assert len({fill['fill_id'] for fill in continued_fills['executions']})==2
            worker.stop();vault.backup(tmp_path/'twice-renewed-dca.db');vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                recovered=bots.run_record(db,current['id'])
                assert recovered[3]['dca_projection'].total_spent_units==2*10**16
                assert len(recovered[3]['dca_projection'].consumed_keys)==2
                assert not bots._public(*recovered[:3])['automatic_execution']
        finally:worker.stop()
