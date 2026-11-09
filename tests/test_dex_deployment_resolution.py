"""Signed same-nonce replacement proofs on an owned disposable EVM."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError,executor_address
from app.live_execution.dex_deployment_resolution import collect_resolution
from app.portfolio import dex_deployment_resolution as journal
from app.portfolio import dex_deployments as original
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from app.portfolio.vault import VaultError
from app.portfolio.models import RealDexDeploymentResolution
from app.portfolio.wallet_funding import _decoded,_encoded
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup,prepare,claim,PASSWORD


def now():return datetime.now(timezone.utc)


def replacement_case(vault,account,meta,rpc,*,cancel=True,typed=False,report=False,price=10**9,gas=None):
    nonce=int(rpc('eth_getTransactionCount',[meta['owner'],'pending']),16)
    plan=prepare(vault,account,now(),wallet_address=meta['owner'],nonce=str(nonce));claim(vault,account,now(),plan,wallet_address=meta['owner'])
    if report:
        with vault.session() as db:original.report_deployment(db,plan['id'],account_id=account,tx_hash='0x'+'ab'*32,now=now())
    tx=deepcopy(plan['transaction'])
    tx['gasPrice']=hex(price)
    if gas is not None:tx['gas']=hex(gas)
    if cancel:tx.update(to=meta['owner'],data='0x',gas='0x5208',value='0x0')
    if typed:
        tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(price),maxPriorityFeePerGas=hex(100000000),accessList=[])
    tx_hash=rpc('eth_sendTransaction',[tx])
    evidence=collect_resolution(rpc,meta['owner'],nonce,500,tx_hash)
    return plan,tx_hash,evidence


def review(vault,account,meta,plan,evidence,*,accept=False):
    with vault.session() as db:return journal.preview_resolution(db,plan['id'],account_id=account,wallet_address=meta['owner'],
        evidence=evidence,accept_fee_overrun=accept,now=now())


def commit(vault,account,meta,plan,evidence,preview,*,accept=False):
    with vault.session() as db:return journal.confirm_resolution(db,plan['id'],account_id=account,wallet_address=meta['owner'],
        evidence=evidence,preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],
        accept_fee_overrun=accept,now=now())


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('report',[False,True])
def test_verified_cancellation_preserves_original_frees_only_later_nonce_books_fee_once_and_reopens(tmp_path,typed,report):
    vault,account,start=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,tx_hash,evidence=replacement_case(vault,account,meta,rpc,typed=typed,report=report)
        with vault.session() as db:
            row,_,_=original.deployment_record(db,plan['id']);before=(row.payload_json,row.payload_digest,row.state_json,row.state_digest,row.status,row.updated_at)
            if not report:
                with pytest.raises(Exception):assert_wallet_nonce_available(db,meta['owner'],str(int(plan['nonce'])+1))
        preview=review(vault,account,meta,plan,evidence)
        assert preview['outcome']=='CANCELLED' and preview['available'] and not preview['executor_verified']
        rpc('evm_mine',[])
        evidence=collect_resolution(rpc,meta['owner'],int(plan['nonce']),500,tx_hash)
        assert review(vault,account,meta,plan,evidence)['preview_digest']==preview['preview_digest']

        assert commit(vault,account,meta,plan,evidence,preview)['outcome']=='CANCELLED'
        assert commit(vault,account,meta,plan,evidence,preview)['resolved']
        with vault.session() as db:
            row,_,_=original.deployment_record(db,plan['id'])
            assert before==(row.payload_json,row.payload_digest,row.state_json,row.state_digest,row.status,row.updated_at)
            assert_wallet_nonce_available(db,meta['owner'],str(int(plan['nonce'])+1))
            with pytest.raises(Exception):assert_wallet_nonce_available(db,meta['owner'],plan['nonce'])
            stored=db.get(RealDexDeploymentResolution,plan['id'])
            for private in (meta['owner'].lower(),tx_hash.lower(),evidence['transaction']['r'].lower()):assert private not in stored.payload_json.lower()
        with pytest.raises(DexExecutorError):
            with vault.session() as db:original.report_deployment(db,plan['id'],account_id=account,tx_hash=tx_hash,now=now())
        from app.portfolio.wallet_links import save_wallet_link
        from app.portfolio.repository import append_event,account_snapshot,list_events
        from app.portfolio.domain import EventInput
        from app.portfolio import dex_receipt_booking as accounting
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',start-timedelta(days=1),amount=Decimal('1')))
        with vault.session() as db:p=accounting.preview_booking(db,'DEPLOYMENT_RESOLUTION',plan['id'],context_account_id=account,wallet_address=meta['owner'])
        assert p['trade'] is None and p['contract']=='DEX_RECEIPT_BOOKING_V2'
        with vault.session() as db:
            accounting.book_receipt(db,'DEPLOYMENT_RESOLUTION',plan['id'],context_account_id=account,wallet_address=meta['owner'],
                expected_revision=p['revision'],preview_digest=p['preview_digest'],now=now())
            assert len(list_events(db,account))==2 and account_snapshot(db,account).cash['BNB']==Decimal('1')-Decimal(p['gas_amount'])
        vault.backup(tmp_path/'cancelled.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.resolution_record(db,plan['id'])[1]['proof']['outcome']=='CANCELLED'


def test_replacement_creation_enables_existing_owner_setup_without_rebinding_original_hash(tmp_path):
    vault,account,_=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,_,evidence=replacement_case(vault,account,meta,rpc,cancel=False,report=True)
        preview=review(vault,account,meta,plan,evidence);assert preview['outcome']=='DEPLOYED'
        commit(vault,account,meta,plan,evidence,preview)
        with vault.session() as db:
            row,terms,state=original.deployment_record(db,plan['id'])
            assert row.status=='CODE_OBSERVED' and state['tx_ref']==original._identity(db,'tx','0x'+'ab'*32)
            assert state['receipt'] is None and state['code_observation'] is not None
            assert original.list_deployments(db,account,now=now())[0]['executor_verified']
        vault.backup(tmp_path/'replaced-create.db')


def test_wrong_scope_proof_tampering_and_fee_overrun_cannot_release_a_pending_request(tmp_path):
    vault,account,_=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,_,evidence=replacement_case(vault,account,meta,rpc,cancel=False,price=2*10**9)
        p=review(vault,account,meta,plan,evidence);assert p['fee_overrun'] and not p['available']
        with pytest.raises(DexExecutorError):commit(vault,account,meta,plan,evidence,p)
        for mutate in ('nonce','sender','block','chain','receipt'):
            proof=deepcopy(evidence)
            if mutate=='nonce':proof['transaction']['nonce']='0x99'
            elif mutate=='sender':proof['transaction']['from']=meta['operator']
            elif mutate=='block':proof['finalized']['number']='0x0'
            elif mutate=='chain':proof['chain_id']='0x1'
            else:proof['receipt']['transactionHash']='0x'+'ab'*32
            with pytest.raises(DexExecutorError):review(vault,account,meta,plan,proof,accept=True)
        accepted=review(vault,account,meta,plan,evidence,accept=True)
        commit(vault,account,meta,plan,evidence,accepted,accept=True)
        with vault.session() as db:
            row=db.get(RealDexDeploymentResolution,plan['id']);payload=_decoded(row.payload_json,row.payload_digest)
            payload['proof']['receipt']['gas_fee_wei']='1';row.payload_json,row.payload_digest=_encoded(payload);db.commit()
        with pytest.raises(DexExecutorError):
            with vault.session() as db:journal.resolution_record(db,plan['id'])
        with pytest.raises(VaultError):vault.backup(tmp_path/'forged.db')


def test_migration_66_preserves_existing_wallet_bookings_and_rejects_hidden_resolution(tmp_path):
    from tests.test_wallet_swap_booking import ready,preview,book
    from tests.test_wallet_swap import PASSWORD as swap_password
    from app.portfolio.models import RealWalletSwapBooking
    from app.portfolio.vault import SCHEMA_VERSION
    vault,account,at,plan=ready(tmp_path)
    proposed=preview(vault,account,plan);book(vault,account,at,plan,proposed)
    with vault.session() as db:
        before=[(row.action_id,row.payload_json,row.payload_digest) for row in db.scalars(select(RealWalletSwapBooking))]
        db.connection().exec_driver_sql('DROP TABLE real_dex_deployment_resolutions')
        db.connection().exec_driver_sql('PRAGMA user_version=66');db.commit()
    vault.lock();vault.unlock(swap_password)
    assert vault.last_migration_backup.is_file() and not vault.last_migration_backup.read_bytes().startswith(b'SQLite format')
    with vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
        assert before==[(row.action_id,row.payload_json,row.payload_digest) for row in db.scalars(select(RealWalletSwapBooking))]
        assert list(db.scalars(select(RealDexDeploymentResolution)))==[]
    vault.backup(tmp_path/'migrated-replacements.db')


def test_reverted_replacement_resolves_without_creating_a_contract_or_a_successful_trade(tmp_path):
    vault,account,_=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,_,evidence=replacement_case(vault,account,meta,rpc,cancel=False,gas=300000)
        p=review(vault,account,meta,plan,evidence);assert p['outcome']=='REVERTED' and not p['executor_verified']
        result=commit(vault,account,meta,plan,evidence,p)
        assert result['receipt']['status']=='REVERTED' and int(result['receipt']['gas_fee_wei'])>0
        with vault.session() as db:
            _,_,state=original.deployment_record(db,plan['id'])
            assert state['code_observation'] is None
            assert_wallet_nonce_available(db,meta['owner'],str(int(plan['nonce'])+1))
        vault.backup(tmp_path/'reverted-replacement.db')
