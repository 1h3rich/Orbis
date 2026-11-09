"""A deployment request is durable, private and never creates trading authority."""

from app.portfolio.vault import SCHEMA_VERSION
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest

from app.portfolio.vault import RealVault
from app.portfolio.repository import create_account
from app.portfolio.dex_deployments import prepare_deployment,claim_deployment,list_deployments,deployment_record
from app.portfolio import dex_deployments as journal
from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.domain import PortfolioError

PASSWORD='isolated DEX deployment vault password'
OWNER='0x'+'12'*20


def setup(directory):
    vault=RealVault(directory/'real.db');vault.unlock(PASSWORD,create=True)
    with vault.session() as db:account=create_account(db,'Wallet BNB','USDT').id
    return vault,account,datetime.now(timezone.utc)


def prepare(vault,account,now,**changes):
    values={'identifier':str(uuid4()),'account_id':account,'wallet_address':OWNER,'fee':500,'nonce':'3',
        'gas':'3500000','gas_price':'1000000000','max_gas_bnb':'0.005','now':now}
    values.update(changes)
    with vault.session() as db:return prepare_deployment(db,**values)


def claim(vault,account,now,plan,**changes):
    values={'account_id':account,'wallet_address':OWNER,'plan_digest':plan['plan_digest'],'now':now}
    values.update(changes)
    with vault.session() as db:return claim_deployment(db,plan['id'],**values)


def test_request_survives_reopen_with_a_single_claim_and_no_private_addresses_or_false_trading_permission(tmp_path):
    vault,account,now=setup(tmp_path);identifier=str(uuid4())
    plan=prepare(vault,account,now,identifier=identifier)
    replay=prepare(vault,account,now+timedelta(seconds=1),identifier=identifier)
    assert replay['plan_digest']==plan['plan_digest'] and replay['expires_at']==plan['expires_at']
    assert plan['transaction']['value']=='0x0' and plan['transaction'].get('to') is None
    assert plan['status']=='PREPARED' and not plan['executor_verified'] and not plan['automatic_execution']
    assert claim(vault,account,now,plan)['claim_granted']
    assert not claim(vault,account,now,plan)['claim_granted']
    vault.lock();vault.unlock(PASSWORD)
    assert not claim(vault,account,now,plan)['claim_granted']
    with vault.session() as db:
        history=list_deployments(db,account,now=now)
        row,terms,state=deployment_record(db,identifier)
        assert OWNER not in row.payload_json+row.state_json+str(history)
        assert 'transaction' not in str(history) and state['status']=='WALLET_REQUESTED'
        assert not history[0]['automatic_execution']
    vault.lock()


def test_original_hash_is_private_immutable_and_reporting_never_confirms_a_deployment(tmp_path):
    vault,account,now=setup(tmp_path);plan=prepare(vault,account,now);claim(vault,account,now,plan)
    original='0x'+'ab'*32
    with vault.session() as db:report=journal.report_deployment(db,plan['id'],account_id=account,tx_hash=original,now=now)
    assert report['status']=='HASH_REPORTED' and not report['executor_verified'] and report['receipt'] is None
    with vault.session() as db:
        assert journal.report_deployment(db,plan['id'],account_id=account,tx_hash=original,now=now)==report
    with vault.session() as db:
        with pytest.raises(DexExecutorError):journal.report_deployment(db,plan['id'],account_id=account,tx_hash='0x'+'cd'*32,now=now)
    vault.lock();vault.unlock(PASSWORD)
    with vault.session() as db:
        row,_,state=deployment_record(db,plan['id']);assert original not in row.state_json and state['tx_ref'] is not None
    vault.backup(tmp_path/'hash-reported.db');vault.lock()


@pytest.mark.parametrize('other_module', ['swap', 'funding'])
def test_concurrent_wallet_claims_share_one_nonce_across_deployment_swap_and_mexc(tmp_path, other_module):
    from tests import test_wallet_swap as swap, test_wallet_funding as funding
    vault, account, now = funding.setup(tmp_path)
    deployment = prepare(vault, account, now, wallet_address=funding.WALLET)
    other = funding.prepare(vault, account, now) if other_module == 'funding' else swap.prepare(vault, account, now, wallet_address=funding.WALLET)
    barrier = Barrier(2)
    def attempt(deploy):
        barrier.wait(timeout=5)
        try:
            if deploy: return claim(vault, account, now, deployment, wallet_address=funding.WALLET)['claim_granted']
            if other_module == 'funding':return funding.claim(vault, account, now, other)['claim_granted']
            return swap.claim(vault, account, now, other, wallet_address=funding.WALLET)['claim_granted']
        except PortfolioError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(attempt, [True, False])) == [False, True]
    vault.lock();vault.unlock(funding.PASSWORD)
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    with vault.session() as db:
        with pytest.raises(PortfolioError):assert_wallet_nonce_available(db, funding.WALLET, '4')
    vault.backup(tmp_path/'single-wallet-claim.db');vault.lock()


def test_v57_migration_preserves_pending_wallet_request_and_creates_encrypted_deployment_journal(tmp_path):
    from tests import test_wallet_swap as swap
    vault, account, now = setup(tmp_path)
    pending = swap.prepare(vault, account, now)
    assert swap.claim(vault, account, now, pending)['claim_granted']
    with vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE real_dex_deployment_intents')
        db.connection().exec_driver_sql('PRAGMA user_version=57');db.commit()
    vault.lock();vault.unlock(PASSWORD)
    assert vault.last_migration_backup.is_file()
    assert vault.last_migration_backup.read_bytes()[:16] != b'SQLite format 3\x00'
    with vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() ==SCHEMA_VERSION
        assert list_deployments(db, account, now=now) == []
        assert swap.list_swaps(db, account, now=now)[0]['status'] == 'WALLET_REQUESTED'
    plan = prepare(vault, account, now)
    with pytest.raises(PortfolioError):claim(vault, account, now, plan)
    vault.backup(tmp_path/'migration-verified.db');vault.lock()
