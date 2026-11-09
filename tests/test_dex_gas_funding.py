"""Owner-funded native BNB, observed on an owned EVM with disposable keys."""

from app.portfolio.vault import SCHEMA_VERSION
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_origin import prepare_origin
from app.live_execution.dex_bot_source import DexBotSources
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from app.live_execution.dex_gas_funding import read_funding_state,collect_funding_receipt
from app.live_execution.dex_policy import configure_transaction
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_bindings as bindings,dex_gas_funding as funding
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.dex_bot_source_fixture import SourceCase
from tests.dex_evm_fixture import evm
from tests.test_dex_allowance_journal import ready
from tests.test_dex_deployments import setup,PASSWORD
from tests.test_dex_operator_drafts import BACKUP_PASSWORD


def now():return datetime.now(timezone.utc)


@contextmanager
def case(directory,*,timeframe='1h',policy_changes=None,draft_ttl_seconds=900,evm_backend=None,research=False):
    (directory/'source').mkdir();source=SourceCase(directory/'source',timeframe=timeframe,research=research)
    vault,account,start=setup(directory);manager=DexOperatorDrafts(vault)
    vault._test_dex_source=source
    try:
        with evm(directory/'evm',backend=evm_backend) as (meta,rpc):
            draft=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],now=now(),ttl_seconds=draft_ttl_seconds)
            args={'identifier':draft['id'],'account_id':account,'wallet_address':meta['owner']}
            backup=manager.export_backup(**args,password=BACKUP_PASSWORD,now=now())
            manager.verify_backup(**args,backup=backup,password=BACKUP_PASSWORD,now=now())
            deployment=ready(vault,account,start,meta,rpc);nonce=int(deployment['nonce'])
            state=read_executor_state(rpc,meta['owner'],nonce,500,now=now())
            from app.live_execution.dex_policy import PolicyLimits
            limits=PolicyLimits.parse({**state.policy_limits.as_dict(),**(policy_changes or {})})
            tx=configure_transaction(meta['owner'],nonce,draft['operator_address'],state.policy_id,limits,
                nonce=state.pending_nonce,gas=1000000,gas_price=10**9,now=int(now().timestamp()),owned_wbnb=0)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
            from app.lab.engine import INTERVALS
            step=INTERVALS[timeframe]
            if int(now().timestamp())//step>int(source.now.timestamp())//step:
                # A fixture crossing a candle boundary still supplies the same
                # <=30s protective observation coverage required in production.
                target=now()
                while source.now<target:
                    source.now=min(target,source.now+timedelta(seconds=25))
                    observed=source.paper.poll(source.session['id'])
                    assert observed['status']=='ACTIVE'
            origin=prepare_origin(DexBotSources(source.paper),kind='STRATEGY',session_id=source.session['id'],
                **({'research_validation_id':source.validation['id']} if research else {}),
                entry_usdt='12',budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=now())
            if evm_backend=='anvil':
                # Setup can be CPU-bound under the full suite. Refresh only our
                # owned fixture head; the production <=30s freshness gate stays.
                head=rpc('eth_getBlockByNumber',['latest',False])
                if now().timestamp()-int(head['timestamp'],16)>15:
                    rpc('evm_setTime',[int(now().timestamp())*1000]);rpc('evm_mine',[])
            state=read_funding_state(rpc,meta['owner'],nonce,500,now=now())
            assert state.operator_balance==0
            with vault.session() as db:
                binding=bindings.create_binding(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                    wallet_address=meta['owner'],prepared=origin,snapshot=state.executor,gas_budget_wei=2*10**15,now=now())
            yield vault,account,manager,draft,meta,rpc,deployment,binding
    finally:manager.revoke();source.close();vault.lock()


def prepare(values,**changes):
    vault,account,manager,draft,meta,rpc,deployment,binding=values
    args={'identifier':str(uuid4()),'account_id':account,'binding_id':binding['id'],'draft_id':draft['id'],
        'wallet_address':meta['owner'],'drafts':manager,'amount_bnb':'0.001','gas':'21000','gas_price':'1000000000',
        'max_gas_bnb':'0.00003','snapshot':read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now()),'now':now()}
    args.update(changes)
    with vault.session() as db:return funding.prepare_funding(db,**args)


def claim(values,plan,**changes):
    vault,account,manager,draft,meta,rpc,deployment,binding=values
    args={'account_id':account,'wallet_address':meta['owner'],'drafts':manager,'plan_digest':plan['plan_digest'],
        'snapshot':read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now()),'now':now()}
    args.update(changes)
    with vault.session() as db:return funding.claim_funding(db,plan['id'],**args)


@pytest.mark.parametrize('typed',[False,True])
def test_native_funding_receipt_survives_restart_without_inventing_profit_or_gas_spend(tmp_path,typed):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        plan=prepare(values)
        assert plan['transaction']['to']==draft['operator_address']
        assert plan['transaction']['data']=='0x' and int(plan['transaction']['value'],16)==10**15
        assert claim(values,plan)['claim_granted']
        assert not claim(values,plan)['claim_granted']
        tx=dict(plan['transaction'])
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(1200000000),maxPriorityFeePerGas=hex(100000000),accessList=[])
        sent=rpc('eth_sendTransaction',[tx])
        with vault.session() as db:
            funding.report_funding(db,plan['id'],account_id=account,tx_hash=sent,now=now())
            with pytest.raises(Exception,match='nonce'):assert_wallet_nonce_available(db,meta['owner'],plan['nonce'])
        evidence=collect_funding_receipt(rpc,sent)
        with vault.session() as db:
            result=funding.check_funding(db,plan['id'],account_id=account,evidence=evidence,now=now())
            assert result['status']=='FINALIZED' and result['receipt']['transferred_wei']==str(10**15)
            receipt=evidence['receipt']
            assert result['receipt']['gas_fee_wei']==str(int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16))
            row,terms,state=funding.funding_record(db,plan['id'])
            for address in (meta['owner'],draft['operator_address'],sent):assert address.lower() not in (row.payload_json+row.state_json).lower()
            grant=bindings.binding_view(db,binding['id'],now=now())['gas']
            assert grant['gas_spent_wei']=='0' and grant['remaining_gas_wei']==str(2*10**15)
        vault.lock();vault.unlock(PASSWORD);manager.revoke()
        with vault.session() as db:assert funding.list_funding(db,account,now=now())[0]['receipt']==result['receipt']
        vault.backup(tmp_path/'funded.db')
        fresh=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())
        assert fresh.operator_balance==10**15


def test_response_lost_blocks_other_owner_requests_and_can_be_archived_after_draft_is_gone(tmp_path):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        plan=prepare(values);assert claim(values,plan)['claim_granted']
        sent=rpc('eth_sendTransaction',[plan['transaction']]);manager.revoke()
        with vault.session() as db:
            with pytest.raises(Exception,match='pendiente'):assert_wallet_nonce_available(db,meta['owner'],str(int(plan['nonce'])+1))
        current=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())
        with vault.session() as db:
            recovered=funding.recover_funding(db,plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=current,now=now())
            assert recovered['status']=='NONCE_OBSERVED' and recovered['receipt'] is None
            assert_wallet_nonce_available(db,meta['owner'],str(int(plan['nonce'])+1))
        proof=collect_funding_receipt(rpc,sent)
        with vault.session() as db:
            accepted=funding.check_funding(db,plan['id'],account_id=account,evidence=proof,now=now())
            assert accepted['status']=='FINALIZED'
        vault.backup(tmp_path/'recovered.db')


def test_funding_rechecks_balance_policy_and_verified_backup_before_any_wallet_request(tmp_path):
    from dataclasses import replace
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        with pytest.raises(DexExecutorError):prepare(values,amount_bnb='0.002000000000000001')
        plan=prepare(values)
        assert prepare(values,identifier=plan['id'])['plan_digest']==plan['plan_digest']
        with pytest.raises(DexExecutorError):prepare(values,identifier=plan['id'],amount_bnb='0.0009')
        snap=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())
        with pytest.raises(DexExecutorError):claim(values,plan,snapshot=replace(snap,operator_balance=2*10**15))
        with pytest.raises(DexExecutorError):claim(values,plan,snapshot=replace(snap,executor=replace(snap.executor,native_balance=1)))
        manager.revoke()
        with pytest.raises(DexExecutorError):claim(values,plan)
        with vault.session() as db:assert funding.funding_record(db,plan['id'])[0].status=='PREPARED'


def test_rejection_is_terminal_and_bad_receipt_cannot_poison_the_original_hash(tmp_path):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        rejected=prepare(values);claim(values,rejected)
        with vault.session() as db:
            assert funding.report_funding(db,rejected['id'],account_id=account,rejected=True,now=now())['status']=='WALLET_REJECTED'
        plan=prepare(values);claim(values,plan);sent=rpc('eth_sendTransaction',[plan['transaction']])
        proof=collect_funding_receipt(rpc,sent)
        for key,value in [('value','0x1'),('to',meta['owner']),('input','0x00'),('nonce','0x0')]:
            wrong=deepcopy(proof);wrong['transaction'][key]=value
            with pytest.raises(DexExecutorError):
                with vault.session() as db:funding.check_funding(db,plan['id'],account_id=account,evidence=wrong,now=now())
            with vault.session() as db:assert funding.funding_record(db,plan['id'])[2]['tx_ref'] is None
        with vault.session() as db:assert funding.check_funding(db,plan['id'],account_id=account,evidence=proof,now=now())['status']=='FINALIZED'


def test_reverted_native_transfer_records_zero_funding_and_only_the_actual_owner_fee(tmp_path):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        plan=prepare(values,gas='100000',max_gas_bnb='0.0002');claim(values,plan)
        # The separate, fixture-identified EVM changes recipient code AFTER the
        # grant. This is not an application RPC capability or production write.
        rpc('evm_setAccountCode',[draft['operator_address'],'0x60006000fd'])
        sent=rpc('eth_sendTransaction',[plan['transaction']]);proof=collect_funding_receipt(rpc,sent)
        assert proof['receipt']['status']=='0x0'
        with vault.session() as db:
            result=funding.check_funding(db,plan['id'],account_id=account,evidence=proof,now=now())
            assert result['status']=='REVERTED' and result['receipt']['transferred_wei']=='0'
            assert int(result['receipt']['gas_fee_wei'])>0
        vault.backup(tmp_path/'reverted.db')


def test_nonce_observation_rejects_a_later_receipt_before_binding_its_hash(tmp_path):
    from dataclasses import replace
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        plan=prepare(values);claim(values,plan)
        sent=rpc('eth_sendTransaction',[plan['transaction']]);proof=collect_funding_receipt(rpc,sent)
        current=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())
        # A plausible, independently supplied fork claims the nonce was already
        # consumed at the same height with another hash. It must never poison
        # the archived original hash, even if a caller catches the exception.
        fork=replace(current,executor=replace(current.executor,block_hash='0x'+'cd'*32))
        with vault.session() as db:funding.recover_funding(db,plan['id'],account_id=account,wallet_address=meta['owner'],snapshot=fork,now=now())
        with vault.session() as db:
            with pytest.raises(DexExecutorError):funding.check_funding(db,plan['id'],account_id=account,evidence=proof,now=now())
            db.commit()
        with vault.session() as db:
            row,_,saved=funding.funding_record(db,plan['id'])
            assert row.status=='NONCE_OBSERVED' and saved['tx_ref'] is None and saved['receipt'] is None


def test_v62_migration_preserves_journals_and_cannot_hide_native_funding_in_an_old_backup(tmp_path):
    from sqlalchemy import select
    from app.portfolio.models import RealDexBotBinding,RealDexControlIntent,RealDexGasFundingIntent
    from app.portfolio.vault import VaultError
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        backup=manager.export_backup(identifier=draft['id'],account_id=account,wallet_address=meta['owner'],password=BACKUP_PASSWORD,now=now())
        with vault.session() as db:
            before=[(r.id,r.payload_json,r.state_json) for model in (RealDexBotBinding,RealDexControlIntent) for r in db.scalars(select(model))]
            db.connection().exec_driver_sql('DROP TABLE real_dex_gas_funding_intents')
            db.connection().exec_driver_sql('PRAGMA user_version=62');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            assert [(r.id,r.payload_json,r.state_json) for model in (RealDexBotBinding,RealDexControlIntent) for r in db.scalars(select(model))]==before
            assert funding.list_funding(db,account,now=now())==[]
        manager.revoke()
        restored=manager.restore_backup(identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],
            backup=backup,password=BACKUP_PASSWORD,now=now())
        plan=prepare((vault,account,manager,restored,meta,rpc,deployment,binding))
        with vault.session() as db:db.connection().exec_driver_sql('PRAGMA user_version=62');db.commit()
        with pytest.raises(VaultError,match='gas funding'):vault.backup(tmp_path/'hidden.db')
        with vault.session() as db:
            with pytest.raises(DexExecutorError):assert_wallet_nonce_available(db,meta['owner'],plan['nonce'])
            db.connection().exec_driver_sql('PRAGMA user_version=63');db.commit()
        vault.backup(tmp_path/'migrated.db')
        with vault.session() as db:
            row=db.get(RealDexGasFundingIntent,plan['id']);terms=__import__('json').loads(row.payload_json)
            terms['amount_wei']=str(3*10**15)
            row.payload_json,row.payload_digest=funding._encoded(terms);db.commit()
        with pytest.raises(VaultError,match='gas funding'):vault.backup(tmp_path/'corrupt.db')


def test_two_native_claims_from_separate_vault_handles_get_only_one_wallet_slot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.portfolio.vault import RealVault
    from app.portfolio.domain import PortfolioError
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        plans=[prepare(values),prepare(values)]
        snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now());barrier=Barrier(2)
        def request(plan):
            other=RealVault(vault.path);other.unlock(PASSWORD)
            try:
                barrier.wait(timeout=10)
                with other.session() as db:return funding.claim_funding(db,plan['id'],account_id=account,wallet_address=meta['owner'],
                    drafts=manager,plan_digest=plan['plan_digest'],snapshot=snapshot,now=now())['claim_granted']
            except PortfolioError:return False
            finally:other.lock()
        with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(request,plans))==[False,True]
