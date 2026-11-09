"""No inferred fills; actual causal bot replacements, explicit rearm and replay."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_executions import read_executions
from app.live_execution.dex_versions import LONG_EXTERNAL_OUTCOME,DCA_EXTERNAL_OUTCOME
from app.portfolio import dex_bot_journal as bots,dex_operator_journal as operator
from app.portfolio.dex_operator_outcomes import operator_outcome,outcome_witnesses,fold_operator_position
from app.portfolio.dex_external_outcomes import external_outcome
from app.portfolio.wallet_funding import _decoded,_encoded
from app.portfolio.models import RealDexBotRun,RealDexBotDecision,RealDexExternalBooking,RealDexExternalAcceptanceLink
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from app.portfolio.vault import RealVault,VaultError
from tests.dex_external_bot_fixture import external_bot_case
from tests.test_economic_external_acceptance import prepared_case
from tests.test_dex_dca_plan import attempt,rule,NOW
from app.live_execution.dex_dca_plan import fold_attempts,ENGINE,OUTCOME_ENGINE,POLICY_OUTCOME_ENGINE
from tests.test_dex_deployments import PASSWORD


def test_external_dca_fact_is_not_a_purchase_and_never_releases_its_activation_key():
    reviewed=replace(attempt(status='REVERTED'),status='EXTERNAL_REVIEWED')
    for version in (ENGINE,OUTCOME_ENGINE,POLICY_OUTCOME_ENGINE):
        with pytest.raises(DexExecutorError):fold_attempts((reviewed,),definition=rule(),now=NOW,engine=version)
    actual=fold_attempts((reviewed,),definition=rule(),now=NOW,engine=DCA_EXTERNAL_OUTCOME)
    assert actual.total_spent_units==0 and not actual.monthly_spent_units and not actual.pending
    assert actual.gas_spent_wei==reviewed.gas_fee_wei and reviewed.key in actual.consumed_keys


def test_wallet_nonce_stays_consumed_but_new_nonce_requires_no_duplicate_ledger_write(tmp_path):
    with prepared_case(tmp_path)as c:
        with c.vault.session()as db:
            with pytest.raises(ValueError):assert_wallet_nonce_available(db,c.meta['owner'],str(c.source.nonce+1))
        c.confirm()
        with c.vault.session()as db:
            fact=external_outcome(db,'WALLET_SWAP',c.source.id);assert fact is not None
            with pytest.raises(ValueError):assert_wallet_nonce_available(db,c.meta['owner'],str(c.source.nonce))
            assert_wallet_nonce_available(db,c.meta['owner'],str(c.source.nonce+1))
            from app.portfolio.wallet_swap import list_swaps
            public=next(item for item in list_swaps(db,c.account,now=c.stamp+timedelta(seconds=3))if item['id']==c.source.id)
            assert public['effective_outcome']=='EXTERNAL_REVIEWED'and public['booked']
            assert public['booking_source_kind']=='EXTERNAL_OBSERVATION'and not public['external_review']['strategy_executed']


@pytest.mark.parametrize('when', ['AFTER_ACCEPTANCE', 'BEFORE_ACCEPTANCE', 'USED_NONCE', 'UNREVIEWED'])
def test_new_manual_claim_after_external_review_uses_the_normal_order_entry(tmp_path, when):
    from app.portfolio.wallet_swap import claim_swap, WalletSwapError, swap_record
    from tests.test_wallet_swap import prepare
    with prepared_case(tmp_path) as c:
        stamp = c.stamp + timedelta(seconds=2)
        if when != 'UNREVIEWED':
            saved = c.confirm(now=stamp)
        else:
            saved = None
        order_at = stamp - timedelta(seconds=1) if when == 'BEFORE_ACCEPTANCE' else stamp + timedelta(seconds=1)
        nonce = c.source.nonce if when == 'USED_NONCE' else c.source.nonce + 1
        plan = prepare(c.vault, c.account, order_at, wallet_address=c.meta['owner'], nonce=str(nonce))
        arguments = dict(account_id=c.account, wallet_address=c.meta['owner'], plan_digest=plan['plan_digest'], now=order_at)
        with c.vault.session() as db:
            if when == 'AFTER_ACCEPTANCE':
                assert claim_swap(db, plan['id'], **arguments)['claim_granted']
            else:
                with pytest.raises(WalletSwapError):
                    claim_swap(db, plan['id'], **arguments)
                db.rollback()
        with c.vault.session() as db:
            original, _, _ = swap_record(db, c.source.id)
            assert original.status in {'WALLET_REQUESTED', 'HASH_REPORTED'}
            if saved:
                assert external_outcome(db, 'WALLET_SWAP', c.source.id).acceptance_id == saved['id']


def test_missing_accepted_link_never_releases_actor_or_hides_as_pending(tmp_path):
    with prepared_case(tmp_path)as c:
        saved=c.confirm()
        with c.vault.session()as db:
            db.delete(db.get(RealDexExternalAcceptanceLink,saved['booking_id']));db.commit()
        with c.vault.session()as db:
            with pytest.raises(ValueError):external_outcome(db,'WALLET_SWAP',c.source.id)
            with pytest.raises(ValueError):assert_wallet_nonce_available(db,c.meta['owner'],str(c.source.nonce+1))


@pytest.mark.parametrize('kind',['LONG','DCA'])
def test_causal_bot_external_receipt_requires_acceptance_then_fresh_permission_without_fake_fill(tmp_path,monkeypatch,kind):
    with external_bot_case(tmp_path,monkeypatch,kind)as c:
        before=c.clock[0]
        def connect():
            return c.connection.connect(request_id=str(uuid4()),account_id=c.account,binding_id=c.binding['id'],draft_id=c.draft['id'],
                wallet_address=c.meta['owner'],snapshot=read_funding_state(c.rpc,c.meta['owner'],int(c.deployment['nonce']),500,now=c.clock[0]),
                ttl_seconds=600,now=c.clock[0])
        with pytest.raises(DexExecutorError):connect()
        saved=c.accept()
        with c.vault.session()as db:
            legacy=operator_outcome(db,c.source.id);past=operator_outcome(db,c.source.id,include_external=True,as_of=before)
            fact=operator_outcome(db,c.source.id,include_external=True)
            assert legacy.outcome==past.outcome=='PENDING'
            assert fact.outcome=='EXTERNAL_REVIEWED'and fact.settled and not fact.filled and fact.effect is None
            assert fact.resolution_id is None and fact.receipt_kind=='EXTERNAL_REVIEW'
            assert outcome_witnesses(db,[c.source.id],include_external=True)==[fact.witness()]
            projected=fold_operator_position(c.source.terms['anchor']['counters'],((fact,None),))
            assert dict(projected.counters)=={key:int(value)for key,value in c.source.terms['anchor']['counters'].items()}
            assert projected.entry_candle_t is None and projected.gas_spent_wei==fact.gas_fee_wei
            row,_,_=operator.intent_record(db,c.source.id);assert(row.payload_json,row.state_json,row.updated_at)==c.before
            grant=operator.grant_view(db,c.binding['grant_id'],now=c.clock[0])
            assert grant['external_outcomes_reviewed']==1 and not grant['unresolved_outcomes']and grant['gas_spent_wei']==str(fact.gas_fee_wei)
            assert bots.run_record(db,c.started['id'])[0].status=='STOPPED'
            fills=read_executions(db,account_id=c.account,run_id=c.started['id'])
            assert fills['total']==0 and fills['pending_count']==0 and fills['external_reviewed_count']==1
            assert fills['external_gas_wei']==str(fact.gas_fee_wei)and fills['external_reviews_are_fills']is False
        c.clock[0]+=timedelta(seconds=1);c.mine()
        connected=connect();assert connected['connected']
        with c.vault.session()as db:seed=[item.json()for item in bots.run_record(db,c.started['id'])[3]['bars']]
        snapshot=read_executor_state(c.rpc,c.meta['owner'],int(c.deployment['nonce']),500,now=c.clock[0],block_tag='finalized')
        with c.vault.session()as db:
            resumed,claim=bots.start_run(db,identifier=str(uuid4()),account_id=c.account,binding_id=c.binding['id'],
                wallet_address=c.meta['owner'],connection=c.connection,snapshot=snapshot,seed=seed,now=c.clock[0],
                permission_mode='SESSION',requested_seconds=600,previous_run_id=c.started['id'])
        assert resumed['engine']==(DCA_EXTERNAL_OUTCOME if kind=='DCA'else LONG_EXTERNAL_OUTCOME)
        with c.vault.session()as db:
            current=bots.run_record(db,resumed['id']);assert current[1]['resume']['resolved_calls']==[fact.witness()]
            assert operator.grant_view(db,c.binding['grant_id'],now=c.clock[0])['gas_spent_wei']==grant['gas_spent_wei']
            if kind=='DCA':
                projection=current[3]['dca_projection'];assert projection.total_spent_units==0 and not projection.pending
                assert len(projection.consumed_keys)==1 and projection.gas_spent_wei==fact.gas_fee_wei
        supervisor=DexBotSupervisor(c.vault,c.connection,clock=lambda:c.clock[0])
        detail=supervisor.view(resumed['id'],account_id=c.account)
        assert detail['position']['owned_wbnb']=='0'and detail['position']['entries']=='0'and not detail['execution_enabled']
        assert detail['policy_renewal_supported']
        worker=DexBotWorker(c.vault,c.connection,run_id=resumed['id'],account_id=c.account,claim=claim,
            wallet_address=c.meta['owner'],market=c.market,rpc_factory=lambda:nullcontext(c.rpc),clock=lambda:c.clock[0])
        try:
            after=worker.run_once();assert after['last_action']in('WAIT','BUY'),after
            if after['last_action']=='BUY':
                assert kind=='DCA'  # Same candle; the consumed base cannot fire again.
                # Anvil advances with wall time while the financial replay runs;
                # this fixture's clock is otherwise frozen. Observe the actual
                # mined time, as a live worker's clock would, before its receipt.
                head=c.rpc('eth_getBlockByNumber',['latest',False])
                from datetime import datetime,timezone
                c.clock[0]=max(c.clock[0],datetime.fromtimestamp(int(head['timestamp'],16),timezone.utc))
                assert worker.run_once()['last_receipt_status']=='FINALIZED'
                with c.vault.session()as db:
                    current=bots.run_record(db,resumed['id'])
                    assert current[3]['dca_projection'].total_spent_units==10**16
                    assert current[3]['dca_projection'].gas_spent_wei>fact.gas_fee_wei
                    decisions=[_decoded(row.payload_json,row.payload_digest)for row in db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==resumed['id']))]
                    assert [item['plan']['dca']['kind']for item in decisions if item['plan']['action']=='BUY']==['DIP']
            worker.stop()
        finally:worker.stop()
        c.connection.revoke()
        copy=c.vault.backup(tmp_path/'reviewed-bot.db');cold=RealVault(copy);cold.unlock(PASSWORD)
        try:
            with cold.session()as db:
                assert bots.run_record(db,resumed['id'])[0].status=='STOPPED'
                assert operator_outcome(db,c.source.id,include_external=True).witness()==fact.witness()
        finally:cold.lock()
        # Rehash the whole child run: a valid public row digest cannot replace
        # the separately authenticated external acceptance witness.
        with c.vault.session()as db:
            row=db.get(RealDexBotRun,resumed['id']);terms=_decoded(row.payload_json,row.payload_digest)
            state=_decoded(row.state_json,row.state_digest)
            terms['resume']['resolved_calls'][0]['external_review']['acceptance_digest']='0'*64
            row.payload_json,row.payload_digest=_encoded(terms)
            previous=row.payload_digest
            for decision in db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==row.id).order_by(RealDexBotDecision.sequence_no)):
                event=_decoded(decision.payload_json,decision.payload_digest)
                decision.previous_digest=event['previous_digest']=previous
                decision.payload_json,decision.payload_digest=_encoded(event)
                previous=decision.payload_digest
            state['head']['digest']=previous;row.state_json,row.state_digest=_encoded(state)
            db.commit()
        with c.vault.session()as db:
            with pytest.raises(ValueError):bots.run_record(db,resumed['id'])
        with pytest.raises(VaultError):c.vault.backup(tmp_path/'invalid-child.db')
