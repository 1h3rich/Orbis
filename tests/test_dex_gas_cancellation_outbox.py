"""Real signed replacements on our EVM, with one durable POST authorization."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import copy,pickle
import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_cancellation_outbox import prepare_cancellation_execution
from app.live_execution.dex_native_resolution import collect_resolution
from app.portfolio import dex_gas_cancellation as journal
from tests.dex_gas_cancellation_fixture import pending_return,cancellation_review,cancellation_plan
from tests.test_dex_gas_funding import now
from tests.test_dex_deployments import PASSWORD


def execution(values,review,plan):
    vault,account,manager,draft,meta,_,_,_=values
    return prepare_cancellation_execution(vault,manager,identifier=review['id'],account_id=account,
        wallet_address=meta['owner'],draft_id=draft['id'],plan=plan,plan_digest=review['plan_digest'],now=now())


def test_cancellation_is_noncopyable_and_two_callers_get_exactly_one_post(tmp_path):
    with pending_return(tmp_path) as (values,original,original_hash,_):
        vault,account,_,_,meta,rpc,_,_=values
        review=cancellation_review(values,original);plan=cancellation_plan(values,review)
        packet=execution(values,review,plan);tx_hash=packet.transaction_hash
        assert not execution(values,review,plan)['new_order_post_attempted']
        for copier in (copy.copy,copy.deepcopy,pickle.dumps):
            with pytest.raises(TypeError):copier(packet)
        barrier=Barrier(2);sends=[]
        def send(raw):
            # A network callback must be able to lock independently: no DB,
            # vault or draft lock crosses the boundary.
            def read():
                with vault.session() as db:return journal.view_cancellation(db,review['id'],now=now())['status']
            with ThreadPoolExecutor(max_workers=1) as pool:assert pool.submit(read).result(timeout=2)=='BROADCAST_ATTEMPTED'
            sends.append(raw);return rpc('eth_sendRawTransaction',['0x'+raw.hex()])
        def contender(_):
            barrier.wait()
            try:return packet.broadcast_once(send,now=now())
            except DexExecutorError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(contender,range(2)))
        assert len(sends)==1 and sum(result is not None for result in results)==1
        assert next(result for result in results if result)['send_result']=='HASH_MATCHED'
        rpc('miner_start',[1]);rpc('evm_mine',[])
        with vault.session() as db:
            settled=journal.observe_cancellation(db,review['id'],account_id=account,wallet_address=meta['owner'],evidence=collect_resolution(rpc,tx_hash),now=now())
            assert settled['winner']=='CANCELLATION'
        assert rpc('eth_getTransactionReceipt',[original_hash]) is None


@pytest.mark.parametrize('arrived',[False,True])
def test_lost_reply_never_reposts_and_reopen_only_observes_winning_receipt(tmp_path,arrived):
    with pending_return(tmp_path) as (values,original,original_hash,_):
        vault,account,_,_,meta,rpc,_,_=values
        review=cancellation_review(values,original);plan=cancellation_plan(values,review)
        packet=execution(values,review,plan);tx_hash=packet.transaction_hash;sends=[]
        def lost(raw):
            sends.append(1)
            if arrived:rpc('eth_sendRawTransaction',['0x'+raw.hex()])
            raise TimeoutError('controlled lost response')
        assert packet.broadcast_once(lost,now=now())['send_result']=='UNKNOWN'
        with pytest.raises(DexExecutorError):packet.broadcast_once(lost,now=now())
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.view_cancellation(db,review['id'],now=now())['status']=='BROADCAST_ATTEMPTED'
        rpc('miner_start',[1]);rpc('evm_mine',[])
        evidence=collect_resolution(rpc,tx_hash if arrived else original_hash)
        with vault.session() as db:
            settled=journal.observe_cancellation(db,review['id'],account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
            assert settled['winner']==('CANCELLATION' if arrived else 'ORIGINAL')
        assert sends==[1];vault.backup(tmp_path/'recovered.db')


@pytest.mark.parametrize('action',['draft','lock','abandon','expiry','access'])
def test_late_cancellation_bytes_never_post_after_revocation_or_abandonment(tmp_path,action):
    with pending_return(tmp_path) as (values,original,_,_):
        vault,account,manager,_,_,_,_,_=values
        review=cancellation_review(values,original);packet=execution(values,review,cancellation_plan(values,review));sends=[]
        if action=='draft':manager.revoke()
        elif action=='lock':vault.lock()
        elif action=='abandon':
            with vault.session() as db:journal.abandon_unsent(db,review['id'],account_id=account,now=now())
        def active():
            if action=='access':raise DexExecutorError('revoked session')
        with pytest.raises(DexExecutorError):
            packet.broadcast_once(lambda raw:sends.append(raw),now=now()+timedelta(seconds=121) if action=='expiry' else now(),require_active=active)
        assert not sends and packet.transaction_hash is None
        if not vault.unlocked:vault.unlock(PASSWORD)
        with vault.session() as db:assert journal.abandon_unsent(db,review['id'],account_id=account,now=now())['status']=='ABANDONED_UNSENT'
        vault.backup(tmp_path/'unsent.db')
