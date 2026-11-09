"""Actual policy renewal after a manually reviewed owned-chain replacement."""
from contextlib import nullcontext
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_journal as bots,dex_bot_bindings as bindings,dex_controls as controls,dex_operator_journal as operators
from app.portfolio.dex_bot_rollover import prepare_rollover,apply_rollover
from app.portfolio.dex_operator_outcomes import operator_outcome
from app.portfolio.models import RealDexControlIntent,RealDexExternalAcceptanceLink
from app.portfolio.wallet_funding import _decoded
from app.portfolio.vault import RealVault,VaultError
from tests.dex_external_bot_fixture import external_bot_case
from tests.test_dex_deployments import PASSWORD


@pytest.mark.parametrize('kind',['LONG','DCA'])
def test_external_reviewed_policy_renews_with_lifetime_gas_keys_and_new_permission(tmp_path,monkeypatch,kind):
    with external_bot_case(tmp_path,monkeypatch,kind)as c:
        def sync():
            head=c.rpc('eth_getBlockByNumber',['latest',False])
            c.clock[0]=max(c.clock[0],datetime.fromtimestamp(int(head['timestamp'],16),timezone.utc))+timedelta(seconds=1)
            return c.clock[0]
        def snapshot():
            return read_executor_state(c.rpc,c.meta['owner'],int(c.deployment['nonce']),500,
                candidate_operator=c.draft['operator_address'],now=sync(),block_tag='finalized')
        def prepare():
            state=snapshot()
            with c.vault.session()as db:
                return prepare_rollover(db,identifier=str(uuid4()),account_id=c.account,previous_run_id=c.started['id'],
                    wallet_address=c.meta['owner'],operator_address=c.draft['operator_address'],snapshot=state,
                    expires_at=int(c.clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=c.clock[0])
        with pytest.raises(DexExecutorError):prepare()
        saved=c.accept();sync();c.mine()
        plan=prepare()
        with c.vault.session()as db:
            fact=operator_outcome(db,c.source.id,include_external=True)
            control=_decoded(db.get(RealDexControlIntent,plan['id']).payload_json,db.get(RealDexControlIntent,plan['id']).payload_digest)
            original=operators.grant_record(db,c.binding['grant_id'])[1]
        assert control['schema']=='orbis.dex.control.v5'
        material=plan['rollover']
        assert material['schema']==('orbis.dex.bot.dca.policy-rollover.v3'if kind=='DCA'else'orbis.dex.bot.policy-rollover.v3')
        assert material['resolved_calls']==[fact.witness()]
        assert material['consumed']['gas_spent']==str(fact.gas_fee_wei)
        assert all(material['consumed'][key]=='0'for key in('buy_spent','entries','realized_loss','acquired_wbnb'))
        assert int(material['gas_remaining_wei'])==int(original['gas_budget_wei'])-fact.gas_fee_wei
        assert not plan['automatic_execution']
        state=snapshot()
        with c.vault.session()as db:
            assert controls.claim_control(db,plan['id'],account_id=c.account,wallet_address=c.meta['owner'],
                operator=c.draft['operator_address'],plan_digest=plan['plan_digest'],snapshot=state,now=c.clock[0])['claim_granted']
        # Keep this owned chain's next mined timestamp aligned with its fixture
        # clock even while a complete financial replay takes real CPU time.
        c.rpc('evm_setNextBlockTimestamp',[int(c.clock[0].timestamp())+1])
        tx=c.rpc('eth_sendTransaction',[plan['transaction']]);receipt=c.rpc('eth_getTransactionReceipt',[tx])
        assert receipt['status']=='0x1'
        block=c.rpc('eth_getBlockByNumber',[receipt['blockNumber'],False]);sync()
        proof={'chain_id':'0x38','tx_hash':tx,'transaction':c.rpc('eth_getTransactionByHash',[tx]),'receipt':receipt,
            'block':block,'finalized':block,'pool':state.pool,'runtime':c.rpc('eth_getCode',[state.executor_address,block['number']])}
        with c.vault.session()as db:controls.report_control(db,plan['id'],account_id=c.account,tx_hash=tx,now=c.clock[0])
        with c.vault.session()as db:assert controls.check_control(db,plan['id'],account_id=c.account,evidence=proof,now=c.clock[0])['status']=='FINALIZED'
        state=snapshot()
        with c.vault.session()as db:
            renewed=apply_rollover(db,identifier=plan['id'],account_id=c.account,wallet_address=c.meta['owner'],snapshot=state,now=c.clock[0])
            payload=bindings.binding_record(db,renewed['id'])[1]
            assert payload['schema']=='orbis.dex.bot.binding.v4'
            assert operators.grant_view(db,renewed['grant_id'],now=c.clock[0])['gas_budget_wei']==material['gas_remaining_wei']
            assert bindings.binding_record(db,c.binding['id'])[0].status=='STOPPED'
        assert renewed['root_binding_id']==c.binding['id']and not renewed['execution_enabled']
        sync();c.mine()
        connected=c.connection.connect(request_id=str(uuid4()),account_id=c.account,binding_id=renewed['id'],draft_id=c.draft['id'],
            wallet_address=c.meta['owner'],snapshot=read_funding_state(c.rpc,c.meta['owner'],int(c.deployment['nonce']),500,now=c.clock[0]),ttl_seconds=600,now=c.clock[0])
        assert connected['connected']
        with c.vault.session()as db:seed=[bar.json()for bar in bots.run_record(db,c.started['id'])[3]['bars']]
        state=snapshot()
        with c.vault.session()as db:
            resumed,claim=bots.start_run(db,identifier=str(uuid4()),account_id=c.account,binding_id=renewed['id'],wallet_address=c.meta['owner'],
                connection=c.connection,snapshot=state,seed=seed,now=c.clock[0],permission_mode='SESSION',requested_seconds=600,previous_run_id=c.started['id'])
            current=bots.run_record(db,resumed['id'])
            assert current[1]['schema']==('orbis.dex.bot.dca.v5'if kind=='DCA'else'orbis.dex.bot.long.v8')
            assert current[1]['resume']['resolved_calls']==[fact.witness()]
            if kind=='DCA':
                projection=current[3]['dca_projection']
                assert projection.total_spent_units==0 and len(projection.consumed_keys)==1
                assert projection.gas_spent_wei==fact.gas_fee_wei
        sync();c.mine()
        worker=DexBotWorker(c.vault,c.connection,run_id=resumed['id'],account_id=c.account,claim=claim,
            wallet_address=c.meta['owner'],market=c.market,rpc_factory=lambda:nullcontext(c.rpc),clock=lambda:c.clock[0])
        try:
            step=worker.run_once();assert step['last_action']in('WAIT','BUY'),step
            if step['last_action']=='BUY':
                assert kind=='DCA'
                sync();assert worker.run_once()['last_receipt_status']=='FINALIZED'
                with c.vault.session()as db:
                    result=bots.run_record(db,resumed['id'])[3]['dca_projection']
                    assert result.total_spent_units==10**16
                    assert result.gas_spent_wei>fact.gas_fee_wei
            worker.stop()
        finally:worker.stop()
        sync();c.mine();state=snapshot()
        with c.vault.session()as db:
            latest=operators.grant_view(db,renewed['grant_id'],now=c.clock[0])
        with c.vault.session()as db:
            second=prepare_rollover(db,identifier=str(uuid4()),account_id=c.account,previous_run_id=resumed['id'],
                wallet_address=c.meta['owner'],operator_address=c.draft['operator_address'],snapshot=state,
                expires_at=int(c.clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=c.clock[0])
        assert second['rollover']['schema']==material['schema']
        assert second['rollover']['consumed']['gas_spent']==str(fact.gas_fee_wei+int(latest['gas_spent_wei']))
        assert second['rollover']['gas_remaining_wei']==latest['remaining_gas_wei']
        assert second['rollover']['resolved_calls']==[]  # The external proof remains in its authenticated ancestor.
        c.connection.revoke()
        copied=c.vault.backup(tmp_path/'external-renewal.db');cold=RealVault(copied);cold.unlock(PASSWORD)
        try:
            with cold.session()as db:
                assert bots.run_record(db,resumed['id'])[0].status=='STOPPED'
                assert bindings.binding_record(db,renewed['id'])[1]['schema']=='orbis.dex.bot.binding.v4'
        finally:cold.lock()
        # A cached run/binding cannot hide corruption of its financial authority.
        with c.vault.session()as db:
            db.delete(db.get(RealDexExternalAcceptanceLink,saved['booking_id']));db.commit()
        with c.vault.session()as db:
            with pytest.raises(ValueError):bots.run_record(db,resumed['id'])
            with pytest.raises(ValueError):bindings.binding_record(db,renewed['id'])
        with pytest.raises(VaultError):c.vault.backup(tmp_path/'corrupt-external-renewal.db')
