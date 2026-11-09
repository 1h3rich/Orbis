"""Recovered economic outcomes survive owner policy reset and explicit rearm."""
from datetime import timedelta
from uuid import uuid4

import pytest

from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_versions import (RECOVERED_CONTROL, RECOVERED_BINDING,
    RECOVERED_LONG_ROLLOVER, RECOVERED_DCA_ROLLOVER, LONG_POLICY_OUTCOME, DCA_POLICY_OUTCOME)
from app.portfolio import dex_bot_journal as bots, dex_bot_bindings as bindings, dex_controls as controls
from app.portfolio import dex_operator_journal as operators
from app.portfolio.dex_bot_rollover import prepare_rollover, apply_rollover
from app.portfolio.dex_operator_outcomes import outcome_witnesses
from app.live_execution.dex_executions import read_executions
from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.dex_bot_rollover import review_material
from app.portfolio.dex_controls import _anchor
from app.portfolio.models import RealDexControlIntent,RealDexBotBinding,RealDexBotRun
from app.portfolio.wallet_funding import _decoded,_encoded
from app.portfolio.vault import VaultError
from tests.dex_recovered_renewal_fixture import recovered_case, make_worker
from tests.test_dex_bot_rollover import confirm_plan
from tests.test_dex_deployments import PASSWORD


def renew(context, previous_id):
    case, snapshot = context['case'], context['snapshot']
    values, _, _, connection, clock, mine, _ = case
    vault, account, _, draft, meta, rpc, deployment, _ = values
    clock[0] += timedelta(seconds=1)
    mine()
    state = snapshot()
    with vault.session() as db:
        old = bots.run_record(db, previous_id)
        seed = [bar.json() for bar in old[3]['bars']]
        plan = prepare_rollover(db, identifier=str(uuid4()), account_id=account, previous_run_id=previous_id,
            wallet_address=meta['owner'], operator_address=draft['operator_address'], snapshot=state,
            expires_at=int(clock[0].timestamp())+3600, gas='1000000', gas_price='1000000000',
            max_gas_bnb='0.002', now=clock[0])
        assert controls.control_record(db, plan['id'])[1]['schema']==RECOVERED_CONTROL
    confirm_plan(case, plan, snapshot)
    after = snapshot()
    assert after.owned_wbnb==state.owned_wbnb and after.cost_usdt==state.cost_usdt
    assert after.buy_spent==after.entries==after.realized_loss==after.last_entry_at==after.next_nonce==0
    with vault.session() as db:
        successor = apply_rollover(db, identifier=plan['id'], account_id=account,
            wallet_address=meta['owner'], snapshot=after, now=clock[0])
        assert bindings.binding_record(db, successor['id'])[1]['schema']==RECOVERED_BINDING
        assert not successor['execution_enabled']
    connection.connect(request_id=str(uuid4()), account_id=account, binding_id=successor['id'], draft_id=draft['id'],
        wallet_address=meta['owner'], snapshot=read_funding_state(rpc, meta['owner'], int(deployment['nonce']),500,now=clock[0]),
        ttl_seconds=900, now=clock[0])
    with vault.session() as db:
        run, claim = bots.start_run(db, identifier=str(uuid4()), account_id=account, binding_id=successor['id'],
            wallet_address=meta['owner'], connection=connection, snapshot=after, seed=seed, now=clock[0],
            permission_mode='SESSION', requested_seconds=900, previous_run_id=previous_id)
    return plan, run, claim


@pytest.mark.parametrize('dca',[False,True],ids=['LONG','DCA'])
@pytest.mark.parametrize('outcome',['EXECUTED','CANCELLED','REVERTED'])
def test_recovered_trade_renews_without_resetting_spend_position_gas_or_activation(tmp_path,monkeypatch,dca,outcome):
    with recovered_case(tmp_path,monkeypatch,dca=dca,outcome=outcome) as context:
        case=context['case'];values,started,_,connection,clock,mine,market=case
        vault,account,_,_,meta,_,_,binding=values
        original_state=context['snapshot']()
        with vault.session() as db:
            original_budget=operators.grant_view(db,binding['grant_id'],now=clock[0])
            references=outcome_witnesses(db,[context['source_id']],as_of=clock[0])
        plan,run,claim=renew(context,started['id'])
        assert plan['rollover']['schema']==(RECOVERED_DCA_ROLLOVER if dca else RECOVERED_LONG_ROLLOVER)
        assert plan['rollover']['resolved_calls']==references
        assert plan['rollover']['gas_remaining_wei']==original_budget['remaining_gas_wei']
        assert plan['rollover']['consumed']['gas_spent']==context['result']['receipt']['gas_fee_wei']
        assert plan['rollover']['consumed']['buy_spent']==str(original_state.buy_spent)
        assert run['engine']==(DCA_POLICY_OUTCOME if dca else LONG_POLICY_OUTCOME)
        with vault.session() as db:
            row,terms,_,archive=bots.run_record(db,run['id'],verify_full=True)
            assert terms['resume']['resolved_calls']==references
            assert int(terms['baseline']['counters']['owned_wbnb'])==original_state.owned_wbnb
            original=operators.intent_record(db,context['source_id'])[0]
            assert (original.payload_json,original.state_json,original.updated_at)==context['original']
            if dca:
                assert archive['dca_projection'].consumed_keys==frozenset({'BASE:'+clock[0].strftime('%Y-%m')})
                assert archive['dca_projection'].total_spent_units==(10**16 if outcome=='EXECUTED' else 0)
                assert archive['dca_projection'].gas_spent_wei==int(context['result']['receipt']['gas_fee_wei'])
                assert archive['cursor'].reference=='97'
        current=make_worker(case,run['id'],claim)
        try:
            if not dca and outcome=='EXECUTED':
                market.bid,market.ask='1.2','1.21'
                clock[0]+=timedelta(seconds=1);mine()
                assert current.run_once()['last_action']=='SELL'
                assert current.run_once()['last_receipt_status']=='FINALIZED'
            elif not dca:
                assert current.run_once()['last_action']=='WAIT'
                market.rows.append({**market.rows[-1],'t':market.rows[-1]['t']+60,'o':'96','h':'97','l':'94','c':'95'})
                for _ in range(3):
                    clock[0]+=timedelta(seconds=20);mine()
                    tick=current.run_once()
                    if tick['last_action']=='BUY':break
                    assert tick['last_action']=='WAIT',tick
                assert tick['last_action']=='BUY',tick
                assert current.run_once()['last_receipt_status']=='FINALIZED'
            else:
                for _ in range(8):
                    clock[0]+=timedelta(seconds=10);mine()
                    result=current.run_once()
                    if result['last_action']=='BUY':break
                    assert result['last_action']=='WAIT',result
                assert result['last_action']=='BUY',result
                assert current.run_once()['last_receipt_status']=='FINALIZED'
                with vault.session() as db:
                    latest=bots.run_record(db,run['id'])[3]['dca_projection']
                    assert len(latest.consumed_keys)==2 and 'DIP:0.1' in latest.consumed_keys
                    assert latest.total_spent_units==(2 if outcome=='EXECUTED' else 1)*10**16
            current.stop()
            with vault.session() as db:
                original=operators.intent_record(db,context['source_id'])[0]
                assert (original.payload_json,original.state_json,original.updated_at)==context['original']
                current_row=bots.run_record(db,run['id'])[0]
                current_budget=operators.grant_view(db,current_row.grant_id,now=clock[0])
            second_plan,second_run,second_claim=renew(context,run['id'])
            assert second_plan['rollover']['consumed']['gas_spent']==str(
                int(context['result']['receipt']['gas_fee_wei'])+int(current_budget['gas_spent_wei']))
            assert second_plan['rollover']['resolved_calls']==[]
            assert second_run['engine']==run['engine']
            current=make_worker(case,second_run['id'],second_claim)
            assert current.run_once()['last_action']=='WAIT'
            current.stop()
            with vault.session() as db:
                fills=read_executions(db,account_id=account,run_id=second_run['id'])
                assert len(fills['family_run_ids'])==3
                assert fills['total']==(2 if outcome=='EXECUTED' else 1)
                if dca:
                    final=bots.run_record(db,second_run['id'])[3]['dca_projection']
                    assert len(final.consumed_keys)==2
                    assert final.total_spent_units==(2 if outcome=='EXECUTED' else 1)*10**16
            vault.backup(tmp_path/'recovered-renewal.db');connection.revoke();vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                recovered=bots.run_record(db,second_run['id'],verify_full=True)
                assert recovered[0].status=='STOPPED' and not bots._public(*recovered[:3])['automatic_execution']
        finally:current.stop()


@pytest.mark.parametrize('dca',[False,True],ids=['LONG','DCA'])
def test_recovered_renewal_requires_known_outcomes_and_rejects_rehashed_downgrades(tmp_path,monkeypatch,dca):
    from copy import deepcopy
    with recovered_case(tmp_path,monkeypatch,dca=dca,outcome='CANCELLED') as context:
        case=context['case'];values,started,_,connection,clock,_,_=case
        vault,account,_,_,meta,_,_,_=values
        options=dict(anchor=None,expires_at=int(clock[0].timestamp())+3600,at=clock[0])
        with vault.session() as db:
            options['anchor']=_anchor(db,context['snapshot']())
            with pytest.raises(DexExecutorError,match='pendiente'):
                review_material(db,started['id'],**options)  # The frozen legacy grammar remains strict.
            with pytest.raises(DexExecutorError,match='pendiente'):
                review_material(db,started['id'],**{**options,'at':context['accepted_at']-timedelta(microseconds=1)},
                    include_resolutions=True)
        plan,run,_=renew(context,started['id'])
        connection.revoke()
        # The current request has no active RAM worker or usable signer. Damage
        # the owned archive only, then restore its exact bytes for each check.
        for kind in ('witness','binding_version','run_version'):
            model,identifier=(RealDexControlIntent,plan['id']) if kind=='witness' else (
                (RealDexBotBinding,run['binding_id']) if kind=='binding_version' else (RealDexBotRun,run['id']))
            with vault.session() as db:
                row=db.get(model,identifier)
                saved=(row.payload_json,row.payload_digest)
                state_saved=(row.state_json,row.state_digest)
                material=deepcopy(_decoded(*saved))
                if kind=='witness':material['bot_rollover']['resolved_calls'][0]['payload_digest']='f'*64
                elif kind=='binding_version':material['schema']='orbis.dex.bot.binding.v2'
                else:
                    material['schema']='orbis.dex.bot.dca.v2' if dca else 'orbis.dex.bot.long.v4'
                    if not dca:material['resume'].pop('resolved_calls')
                row.payload_json,row.payload_digest=_encoded(material)
                if kind=='run_version':
                    state=_decoded(*state_saved);state['head']['digest']=row.payload_digest
                    row.state_json,row.state_digest=_encoded(state)
                db.commit()
            try:
                with vault.session() as db,pytest.raises(DexExecutorError):
                    if kind=='witness':controls.control_record(db,identifier)
                    elif kind=='binding_version':bindings.binding_record(db,identifier)
                    else:bots.run_record(db,identifier,verify_full=True)
                with pytest.raises(VaultError):vault.backup(tmp_path/(kind+'-invalid.db'))
            finally:
                with vault.session() as db:
                    row=db.get(model,identifier);row.payload_json,row.payload_digest=saved
                    row.state_json,row.state_digest=state_saved;db.commit()
        with vault.session() as db:
            bots.stop_run(db,run['id'],account_id=account,now=clock[0])
            assert bots.run_record(db,run['id'],verify_full=True)[0].status=='STOPPED'
        vault.backup(tmp_path/'restored-valid.db')


def test_actual_replacement_gas_can_exhaust_budget_despite_extra_native_balance(tmp_path,monkeypatch):
    with recovered_case(tmp_path,monkeypatch,outcome='CANCELLED',gas_price=120*10**9,
                        extra_native_wei=5*10**15) as context:
        case=context['case'];values,started,_,_,clock,mine,_=case
        vault,account,_,draft,meta,_,_,binding=values
        assert int(context['result']['receipt']['gas_fee_wei'])>2*10**15
        clock[0]+=timedelta(seconds=1);mine()
        with vault.session() as db:
            budget=operators.grant_view(db,binding['grant_id'],now=clock[0])
            assert budget['remaining_gas_wei']=='0'
            with pytest.raises(DexExecutorError,match='gas'):
                prepare_rollover(db,identifier=str(uuid4()),account_id=account,previous_run_id=started['id'],
                    wallet_address=meta['owner'],operator_address=draft['operator_address'],snapshot=context['snapshot'](),
                    expires_at=int(clock[0].timestamp())+3600,gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=clock[0])


def test_recovered_operator_stop_preserves_its_policy_revision_position_and_actual_gas(tmp_path,monkeypatch):
    from eth_account import Account
    from eth_utils import to_checksum_address
    from app.live_execution.dex_trade import read_stop_plan
    from app.live_execution.dex_operator_outbox import prepare_execution
    from app.live_execution.dex_call_resolution import collect_call_resolution
    from app.portfolio import dex_call_resolution as resolutions
    with recovered_case(tmp_path,monkeypatch) as context:
        case=context['case'];values,started,_,connection,clock,mine,_=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        clock[0]+=timedelta(seconds=1);mine()
        connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
            wallet_address=meta['owner'],snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),
            ttl_seconds=900,now=clock[0])
        plan=read_stop_plan(rpc,meta['owner'],int(deployment['nonce']),500,gas=100000,gas_price=10**9,max_gas_wei=10**14,now=clock[0])
        prepared=prepare_execution(vault,connection.session,identifier=str(uuid4()),account_id=account,
            grant_id=binding['grant_id'],plan=plan,decision_ref='a'*64,now=clock[0])
        rpc('miner_stop',[])
        prepared.broadcast_once(lambda raw:rpc('eth_sendRawTransaction',['0x'+raw.hex()]),now=clock[0])
        with vault.session() as db:
            original,terms,_,source=resolutions.source_record(db,'OPERATOR',prepared.identifier,owner=meta['owner'])
            before=(original.payload_json,original.state_json,original.updated_at)
        call=source.expected_call()
        fields={'to':to_checksum_address(call['to']),'data':call['data'],'value':0,'chainId':56,
            'nonce':source.nonce,'gas':100001,'gasPrice':1200000000}
        with manager._copy(draft['id'],account,meta['owner'],clock[0]) as (_,key):signed=Account.sign_transaction(fields,bytes(key))
        sent=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
        clock[0]+=timedelta(seconds=2);mine();rpc('miner_start',[1]);connection.revoke()
        clock[0]+=timedelta(seconds=1)
        scope=dict(account_id=account,wallet_address=meta['owner'],accept_changes=True,
            evidence=collect_call_resolution(rpc,source,sent),now=clock[0])
        with vault.session() as db:preview=resolutions.preview_resolution(db,'OPERATOR',source.id,**scope)
        with vault.session() as db:
            done=resolutions.confirm_resolution(db,'OPERATOR',source.id,preview_digest=preview['preview_digest'],
                source_state_digest=preview['source_state_digest'],**scope)
        assert done['outcome']=='EXECUTED' and done['effect']['amount_out_units'] is None
        new_plan,run,claim=renew(context,started['id'])
        material=new_plan['rollover']
        assert material['policy_stops'][0]['kind']=='OPERATOR' and material['policy_stops'][0]['id']==source.id
        assert len(material['resolved_calls'])==2 and material['position']['owned_wbnb']==str(10**16+1)
        assert material['consumed']['gas_spent']==str(int(done['receipt']['gas_fee_wei'])+
            int(context['result']['receipt']['gas_fee_wei']))
        with vault.session() as db:
            row=operators.intent_record(db,source.id)[0]
            assert (row.payload_json,row.state_json,row.updated_at)==before
        current=make_worker(case,run['id'],claim)
        try:
            assert current.run_once()['last_action']=='WAIT'
        finally:current.stop()
        vault.backup(tmp_path/'recovered-stop-renewal.db')
