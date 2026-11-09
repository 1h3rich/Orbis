"""Tested source → exact on-chain policy → encrypted durable DEX provenance."""

from app.portfolio.vault import SCHEMA_VERSION
from datetime import datetime,timezone
from uuid import uuid4
import pytest
from app.live_execution.dex_bot_source import DexBotSources
from app.live_execution.dex_bot_origin import prepare_origin
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_bindings as bindings
from tests.dex_bot_source_fixture import SourceCase
from tests.dex_evm_fixture import evm
from tests.test_dex_allowance_journal import ready
from tests.test_dex_deployments import setup,PASSWORD


def test_source_and_limits_survive_reopen_without_paper_or_wallet_addresses(tmp_path):
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory)
    vault,account,now=setup(tmp_path)
    try:
        origin=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
            entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,
            now=datetime.now(timezone.utc))
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc)
            snapshot=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc),block_tag="finalized")
            identifier=str(uuid4())
            with vault.session() as db:
                saved=bindings.create_binding(db,identifier=identifier,account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],prepared=origin,snapshot=snapshot,gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
            assert saved['status']=='READY' and saved['source']['version_id']==source.version['version_id']
            assert saved['source']['source_run_id']==source.run['id']
            assert saved['execution_enabled'] is False and saved['automatic_execution'] is False
            assert saved['effective_limits']['max_buy_per_order']=='1000000000000000001'
            with vault.session() as db:
                row,terms,_=bindings.binding_record(db,identifier)
                for address in (meta['owner'],meta['operator'],snapshot.executor_address):
                    assert address.lower() not in (row.payload_json+row.state_json+str(saved)).lower()
            source.close();source.lab.path.unlink()
            vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                reopened=bindings.binding_view(db,identifier,now=datetime.now(timezone.utc))
                assert reopened['source']==saved['source'] and reopened['plan_digest']==saved['plan_digest']
            vault.backup(tmp_path/'backup.db')
    finally:
        source.close();vault.lock()


def test_atomic_binding_rejects_unbounded_policy_and_cannot_replenish_or_duplicate_grants(tmp_path):
    import pytest
    from sqlalchemy import select,func
    from app.portfolio.models import RealDexOperatorGrant,RealDexBotBinding
    from app.live_execution.dex_artifact import DexExecutorError
    from app.live_execution.dex_policy import configure_transaction
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory)
    vault,account,now=setup(tmp_path)
    try:
        prepared=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
            entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=datetime.now(timezone.utc))
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc)
            def current():return read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc),block_tag="finalized")
            def bind(identifier,review=prepared):
                with vault.session() as db:return bindings.create_binding(db,identifier=identifier,account_id=account,
                    deployment_id=deployment['id'],wallet_address=meta['owner'],prepared=review,snapshot=current(),
                    gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
            narrow=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
                entry_usdt='0.1',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=datetime.now(timezone.utc))
            with pytest.raises(DexExecutorError,match='política en cadena'):bind(str(uuid4()),narrow)
            with vault.session() as db:assert db.scalar(select(func.count()).select_from(RealDexOperatorGrant))==0
            identifier=str(uuid4());first=bind(identifier)
            assert bind(identifier)['replayed']
            # Owner changes policy on the controlled chain. A new gas grant may
            # not escape the rollback when its binding conflicts with the first.
            state=current()
            tx=configure_transaction(meta['owner'],int(deployment['nonce']),meta['operator'],state.policy_id,state.policy_limits,
                nonce=state.pending_nonce,gas=1000000,gas_price=10**9,now=int(datetime.now(timezone.utc).timestamp()),owned_wbnb=0)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
            with pytest.raises(DexExecutorError,match='ya tiene un vínculo'):bind(str(uuid4()))
            with vault.session() as db:
                assert db.scalar(select(func.count()).select_from(RealDexOperatorGrant))==1
                assert db.scalar(select(func.count()).select_from(RealDexBotBinding))==1
                assert bindings.binding_view(db,identifier,now=datetime.now(timezone.utc))['grant_id']==first['grant_id']
    finally:source.close();vault.lock()


def test_locked_paper_revokes_a_prepared_binding_before_any_grant(tmp_path):
    import pytest
    from sqlalchemy import select,func
    from app.portfolio.models import RealDexOperatorGrant
    from app.live_execution.dex_artifact import DexExecutorError
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory)
    vault,account,now=setup(tmp_path)
    try:
        prepared=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
            entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=datetime.now(timezone.utc))
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc);source.close()
            snapshot=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc),block_tag="finalized")
            with pytest.raises(DexExecutorError,match='Desbloquea'):
                with vault.session() as db:bindings.create_binding(db,identifier=str(uuid4()),account_id=account,
                    deployment_id=deployment['id'],wallet_address=meta['owner'],prepared=prepared,snapshot=snapshot,
                    gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
            with vault.session() as db:assert db.scalar(select(func.count()).select_from(RealDexOperatorGrant))==0
    finally:source.close();vault.lock()


def test_a_prepared_binding_does_not_authorize_swaps_even_through_the_internal_outbox(tmp_path):
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    from app.live_execution.dex_operator import DexOperatorSession
    from app.live_execution.dex_operator_outbox import prepare_execution
    from app.live_execution.dex_allowance import allowance_cap,approval_transaction
    from app.live_execution.dex_trade import read_trade_plan
    from tests.test_dex_operator import KEY,write
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory)
    vault,account,now=setup(tmp_path)
    try:
        prepared=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
            entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=datetime.now(timezone.utc))
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc);nonce=int(deployment['nonce'])
            state=read_executor_state(rpc,meta['owner'],nonce,500,now=datetime.now(timezone.utc),block_tag='finalized')
            with vault.session() as db:
                saved=bindings.create_binding(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],prepared=prepared,snapshot=state,gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
            for token,recipient in [('usdt',meta['owner']),('wbnb',meta['router'])]:
                write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**18])
            state=read_executor_state(rpc,meta['owner'],nonce,500,now=datetime.now(timezone.utc))
            cap=allowance_cap(state,'USDT',now=datetime.now(timezone.utc))
            tx=approval_transaction(meta['owner'],nonce,'USDT',cap,cap=cap,nonce=state.pending_nonce,gas=100000,gas_price=10**9)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
            plan=read_trade_plan(rpc,meta['owner'],nonce,500,buy=True,amount=10**16,slippage_bps=25,
                gas=500000,gas_price=10**9,max_gas_wei=10**15,now=datetime.now(timezone.utc))
            session=DexOperatorSession(vault)
            session.connect(private_key=KEY,snapshot=plan.snapshot,ttl_seconds=60,gas_budget_wei=2*10**15,
                backup_confirmed=True,now=datetime.now(timezone.utc))
            try:
                with pytest.raises(DexExecutorError,match='activación'):
                    prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=saved['grant_id'],
                        plan=plan,decision_ref='a'*64,now=datetime.now(timezone.utc))
                from app.live_execution.dex_trade import read_stop_plan
                from app.live_execution.dex_operator_transport import collect_operator_receipt
                from app.portfolio import dex_operator_journal as journal
                stop=read_stop_plan(rpc,meta['owner'],nonce,500,gas=100000,gas_price=10**9,max_gas_wei=10**15,now=datetime.now(timezone.utc))
                prepared_stop=prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=saved['grant_id'],
                    plan=stop,decision_ref='b'*64,now=datetime.now(timezone.utc))
                hashes=[]
                def send(raw):
                    tx_hash=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(tx_hash);return tx_hash
                assert prepared_stop.broadcast_once(send,now=datetime.now(timezone.utc))['send_result']=='HASH_MATCHED'
                proof=collect_operator_receipt(rpc,meta['owner'],nonce,hashes[0])
                with vault.session() as db:
                    assert journal.check_intent(db,prepared_stop.identifier,account_id=account,evidence=proof,now=datetime.now(timezone.utc))['status']=='FINALIZED'
                    assert bindings.binding_view(db,saved['id'],now=datetime.now(timezone.utc))['grant_status']=='STOPPED'
            finally:session.revoke()
    finally:source.close();vault.lock()


def test_v61_migration_keeps_operator_and_control_bytes_and_adds_no_bot_authority(tmp_path):
    from sqlalchemy import select
    from app.portfolio import dex_operator_journal as journal
    from app.portfolio.models import RealDexOperatorGrant,RealDexControlIntent
    vault,account,now=setup(tmp_path)
    try:
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc)
            state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc))
            with vault.session() as db:
                journal.create_grant(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],snapshot=state,gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
            with vault.session() as db:
                before=[(row.id,row.payload_json,row.state_json) for model in (RealDexOperatorGrant,RealDexControlIntent) for row in db.scalars(select(model))]
                db.connection().exec_driver_sql('DROP TABLE real_dex_bot_bindings')
                db.connection().exec_driver_sql('PRAGMA user_version=61');db.commit()
            vault.lock();vault.unlock(PASSWORD)
            assert vault.last_migration_backup.is_file()
            with vault.session() as db:
                assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
                after=[(row.id,row.payload_json,row.state_json) for model in (RealDexOperatorGrant,RealDexControlIntent) for row in db.scalars(select(model))]
                assert after==before
                assert bindings.list_bindings(db,account,now=datetime.now(timezone.utc))==[]
            vault.backup(tmp_path/'migrated.db')
    finally:vault.lock()


def test_binding_cannot_hide_in_v61_backup_or_forge_a_backtest_after_rehashing(tmp_path):
    import pytest
    from app.portfolio.vault import VaultError
    from app.portfolio.wallet_funding import _encoded
    from app.lab.store import content_hash
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory)
    vault,account,now=setup(tmp_path)
    try:
        prepared=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
            entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=datetime.now(timezone.utc))
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc)
            state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc),block_tag='finalized')
            with vault.session() as db:
                saved=bindings.create_binding(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],prepared=prepared,snapshot=state,gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
                db.connection().exec_driver_sql('PRAGMA user_version=61');db.commit()
            with pytest.raises(VaultError,match='bot binding'):vault.backup(tmp_path/'mistamped.db')
            with vault.session() as db:
                db.connection().exec_driver_sql('PRAGMA user_version=62');db.commit()
            vault.backup(tmp_path/'correct.db')
            with vault.session() as db:
                row,terms,_=bindings.binding_record(db,saved['id'])
                run=terms['origin']['backtest'];run['metrics']['net_profit']='999999'
                metadata={'id','dataset_id','version_id','created_at','engine_version','dataset_hash','dataset_source','strategy_hash','result_hash'}
                run['result_hash']=content_hash({k:v for k,v in run.items() if k not in metadata})
                row.payload_json,row.payload_digest=_encoded(terms);db.commit()
            with pytest.raises(VaultError,match='bot binding'):vault.backup(tmp_path/'fabricated.db')
    finally:source.close();vault.lock()


@pytest.mark.parametrize('same_id',[False,True])
def test_independent_vault_instances_cannot_duplicate_the_binding_or_gas_budget(tmp_path,same_id):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import select,func
    from app.portfolio.vault import RealVault
    from app.portfolio.models import RealDexOperatorGrant,RealDexBotBinding
    from app.live_execution.dex_artifact import DexExecutorError
    directory=tmp_path/'source';directory.mkdir();source=SourceCase(directory)
    vault,account,now=setup(tmp_path)
    try:
        prepared=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
            entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=datetime.now(timezone.utc))
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc)
            state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc),block_tag='finalized')
            barrier=Barrier(2)
            def bind(identifier):
                other=RealVault(vault.path);other.unlock(PASSWORD)
                try:
                    barrier.wait(timeout=10)
                    with other.session() as db:return bindings.create_binding(db,identifier=identifier,account_id=account,
                        deployment_id=deployment['id'],wallet_address=meta['owner'],prepared=prepared,snapshot=state,
                        gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
                except DexExecutorError:return None
                finally:other.lock()
            with ThreadPoolExecutor(max_workers=2) as pool:
                identifier=str(uuid4())
                results=list(pool.map(bind,[identifier,identifier if same_id else str(uuid4())]))
            if same_id:assert sorted(item['replayed'] for item in results)==[False,True]
            else:assert sum(item is not None for item in results)==1
            with vault.session() as db:
                assert db.scalar(select(func.count()).select_from(RealDexOperatorGrant))==1
                assert db.scalar(select(func.count()).select_from(RealDexBotBinding))==1
            vault.backup(tmp_path/'single-binding.db')
    finally:source.close();vault.lock()


def test_dca_binding_requires_buy_only_policy_and_preserves_monthly_real_limit(tmp_path):
    from dataclasses import replace
    from tests.dex_bot_source_fixture import DcaSourceCase
    from app.live_execution.dex_policy import configure_transaction
    from app.live_execution.dex_artifact import DexExecutorError
    directory=tmp_path/'source';directory.mkdir();source=DcaSourceCase(directory)
    vault,account,now=setup(tmp_path)
    try:
        prepared=prepare_origin(DexBotSources(dca_paper=source.paper),kind='DCA',session_id=source.session['id'],
            entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt='40',now=datetime.now(timezone.utc))
        with evm(tmp_path/'evm') as (meta,rpc):
            deployment=ready(vault,account,now,meta,rpc)
            state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc),block_tag='finalized')
            with pytest.raises(DexExecutorError,match='permisos'):
                with vault.session() as db:bindings.create_binding(db,identifier=str(uuid4()),account_id=account,
                    deployment_id=deployment['id'],wallet_address=meta['owner'],prepared=prepared,snapshot=state,
                    gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
            tx=configure_transaction(meta['owner'],int(deployment['nonce']),meta['operator'],state.policy_id,
                replace(state.policy_limits,allow_sell=False,max_exit_notional=0),nonce=state.pending_nonce,
                gas=1000000,gas_price=10**9,now=int(datetime.now(timezone.utc).timestamp()),owned_wbnb=0)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
            state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=datetime.now(timezone.utc),block_tag='finalized')
            with vault.session() as db:
                saved=bindings.create_binding(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],prepared=prepared,snapshot=state,gas_budget_wei=2*10**15,now=datetime.now(timezone.utc))
            assert saved['source_kind']=='DCA' and saved['effective_limits']['allow_sell'] is False
            assert saved['dca']['effective_monthly_units']==str(40*10**18)
            source.close();source.lab.path.unlink()
            vault.backup(tmp_path/'dca-binding.db')
    finally:source.close();vault.lock()
