"""Verified operator recovery → existing grant → bounded signer, without swaps."""
from dataclasses import replace
from uuid import uuid4

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_operator import DexOperatorSession
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_gas_funding import read_funding_state,collect_funding_receipt
from app.portfolio import dex_gas_funding as funding,dex_operator_journal as journal
from tests.test_dex_gas_funding import case,prepare,claim,now


def funded(values,*,amount_bnb="0.001"):
    vault,account,manager,draft,meta,rpc,deployment,binding=values
    plan=prepare(values,amount_bnb=amount_bnb);claim(values,plan);tx_hash=rpc('eth_sendTransaction',[plan['transaction']])
    with vault.session() as db:
        funding.check_funding(db,plan['id'],account_id=account,evidence=collect_funding_receipt(rpc,tx_hash),now=now())
    return read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())


def arguments(values,snapshot):
    _,account,_,draft,meta,_,_,binding=values
    return dict(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
        wallet_address=meta['owner'],snapshot=snapshot,ttl_seconds=60,now=now())


def test_verified_operator_connects_to_the_existing_grant_without_resetting_it(tmp_path):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        snapshot=funded(values);session=DexOperatorSession(vault)
        connection=DexOperatorConnection(vault,manager,session)
        try:
            args=arguments(values,snapshot);linked=connection.connect(**args)
            assert linked['connected'] and linked['binding_id']==binding['id'] and linked['grant_id']==binding['grant_id']
            assert not linked['automatic_execution'] and linked['gas_budget_wei']==str(10**15)
            revision=session.permission_revision
            replay=connection.connect(**{**args,'now':now()})
            assert replay['expires_at']==linked['expires_at'] and session.permission_revision==revision
            with vault.session() as db:
                grant=journal.grant_view(db,binding['grant_id'],now=now())
                assert grant['gas_budget_wei']==str(2*10**15) and grant['gas_spent_wei']=='0'
            manager.revoke()
            assert not session.status()['connected']
            assert not connection.status(account_id=account,wallet_address=meta['owner'],now=now())['connected']
        finally:connection.revoke();session.revoke()


def test_a_funding_balance_is_not_enough_without_the_current_policy_and_verified_draft(tmp_path):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        snapshot=funded(values);session=DexOperatorSession(vault);connection=DexOperatorConnection(vault,manager,session)
        try:
            args=arguments(values,snapshot)
            for change in ({'binding_id':str(uuid4())},{'draft_id':str(uuid4())},{'account_id':str(uuid4())},
                    {'ttl_seconds':True},{'ttl_seconds':86401},
                    {'snapshot':replace(snapshot,operator_balance=0)},
                    {'snapshot':replace(snapshot,executor=replace(snapshot.executor,policy_id=snapshot.executor.policy_id+1))}):
                with pytest.raises(DexExecutorError):connection.connect(**{**args,**change})
                assert not session.status()['connected']
            manager.revoke()
            with pytest.raises(DexExecutorError):connection.connect(**args)
        finally:connection.revoke();session.revoke()


def test_disconnect_and_reconnect_cannot_reuse_a_request_or_replenish_spent_gas(tmp_path):
    from app.live_execution.dex_trade import read_stop_plan
    from app.live_execution.dex_operator_outbox import prepare_execution
    from app.live_execution.dex_operator_transport import collect_operator_receipt
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        snapshot=funded(values);session=DexOperatorSession(vault);connection=DexOperatorConnection(vault,manager,session)
        try:
            args=arguments(values,snapshot);connection.connect(**args)
            connection.disconnect(account_id=account,wallet_address=meta['owner'])
            assert not session.status()['connected']
            with pytest.raises(DexExecutorError):connection.connect(**args)
            connection.connect(**{**args,'request_id':str(uuid4()),'now':now()})
            # Intrinsic gas is enough to mine but not execute STOP. The actual
            # reverted receipt consumes gas without ending the durable grant.
            plan=read_stop_plan(rpc,meta['owner'],int(deployment['nonce']),500,gas=21064,gas_price=10**9,max_gas_wei=10**15,now=now())
            prepared=prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=binding['grant_id'],
                plan=plan,decision_ref='ab'*32,now=now())
            hashes=[]
            def send(raw):
                result=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(result);return result
            assert prepared.broadcast_once(send,now=now())['send_result']=='HASH_MATCHED'
            proof=collect_operator_receipt(rpc,meta['owner'],int(deployment['nonce']),hashes[0])
            with vault.session() as db:
                assert journal.check_intent(db,prepared.identifier,account_id=account,evidence=proof,now=now())['status']=='REVERTED'
                view=journal.grant_view(db,binding['grant_id'],now=now())
                assert view['status']=='ACTIVE' and int(view['gas_spent_wei'])>0 and int(view['remaining_gas_wei'])<2*10**15
            assert not session.status()['connected']
            current=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())
            reconnected=connection.connect(**{**args,'request_id':str(uuid4()),'snapshot':current,'now':now()})
            assert int(reconnected['gas_budget_wei'])==min(current.operator_balance,int(view['remaining_gas_wei']))<10**15
        finally:connection.revoke();session.revoke()


def test_connection_never_outlives_its_verified_draft_and_will_not_resurrect_after_lock(tmp_path):
    from tests.test_dex_deployments import PASSWORD
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        snapshot=funded(values);clock=[manager._clock()];manager._clock=lambda:clock[0]
        session=DexOperatorSession(vault,monotonic=lambda:clock[0]);connection=DexOperatorConnection(vault,manager,session)
        try:
            remaining=manager.status(account_id=account,wallet_address=meta['owner'],now=now())['remaining_seconds']
            linked=connection.connect(**{**arguments(values,snapshot),'ttl_seconds':900})
            assert linked['remaining_seconds']<=remaining and linked['expires_at']<=draft['expires_at']
            clock[0]+=901
            assert not connection.status(account_id=account,wallet_address=meta['owner'],now=now())['connected']
            assert not session.status()['connected']
            vault.lock();vault.unlock(PASSWORD)
            assert not connection.status(account_id=account,wallet_address=meta['owner'],now=now())['connected']
        finally:connection.revoke();session.revoke()


def test_a_stale_connection_or_draft_cannot_erase_a_newer_signer_revision(tmp_path):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        snapshot=funded(values);session=DexOperatorSession(vault);connection=DexOperatorConnection(vault,manager,session)
        try:
            connection.connect(**arguments(values,snapshot));prior=session.permission_revision
            # Simulate a different internal owner of the shared signer. No
            # transaction is created or sent; this tests revocation ownership.
            session.connect(private_key=bytes.fromhex('22'*32),snapshot=replace(snapshot.executor,operator=meta['operator'].lower()),
                ttl_seconds=60,gas_budget_wei=10**15,backup_confirmed=True,now=now())
            assert session.permission_revision!=prior and session.status()['connected']
            connection.revoke();assert session.status()['connected']
            manager.revoke();assert session.status()['connected']
        finally:connection.revoke();session.revoke()


@pytest.mark.parametrize('action',['lock','protect_view','unlock'])
def test_real_access_lifecycle_wipes_a_connected_verified_operator(tmp_path,action):
    from app.portfolio.api import RealAccess
    from tests.test_dex_deployments import PASSWORD
    from tests.test_dex_operator_drafts import BACKUP_PASSWORD
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        funded(values)
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
        manager.revoke();vault.lock();access=RealAccess(vault.path)
        try:
            access.unlock(PASSWORD)
            restored=access._dex_operator_drafts.restore_backup(identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],
                backup=backup,password=BACKUP_PASSWORD,now=now())
            snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())
            args={**arguments(values,snapshot),'draft_id':restored['id']}
            access._dex_connection.connect(**args)
            key=access._dex_operator._lease.key
            assert any(key)
            if action=='unlock':access.unlock(PASSWORD)
            else:getattr(access,action)()
            assert not any(key) and not access._dex_operator.status()['connected']
            assert not access._dex_connection.status(account_id=account,wallet_address=meta['owner'],now=now())['connected']
        finally:access.lock()
