"""Single native send, lost-response recovery and draft revocation; own EVM."""
from contextlib import contextmanager,nullcontext
from datetime import timedelta
from uuid import uuid4
import copy,pickle

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_gas_return import read_return_state,plan_return
from app.live_execution.dex_gas_return_outbox import prepare_return_execution
from app.live_execution.dex_gas_funding import collect_funding_receipt
from app.portfolio import dex_gas_return as journal
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_deployments import PASSWORD


@contextmanager
def return_case(directory):
    with running_case(directory) as case:
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0]);worker.stop()
        clock[0]+=timedelta(seconds=1);mine()
        plan=plan_return(read_return_state(rpc,meta['owner'],draft['operator_address'],now=clock[0]),
            gas_price=10**9,max_gas_wei=21000*10**9,now=clock[0])
        with vault.session() as db:
            review=journal.prepare_return(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
                wallet_address=meta['owner'],drafts=manager,plan=plan,now=clock[0])
        def prepare():return prepare_return_execution(vault,manager,identifier=review['id'],account_id=account,
            wallet_address=meta['owner'],draft_id=draft['id'],plan=plan,plan_digest=review['plan_digest'],now=clock[0])
        yield case,plan,review,prepare


def test_return_outbox_claims_one_broadcast_under_competing_callers_and_cannot_be_copied(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with return_case(tmp_path) as (case,plan,review,prepare):
        values,_,_,_,clock,_,_=case;vault,account,manager,draft,meta,rpc,_,_=values
        prepared=prepare();tx_hash=prepared.transaction_hash;barrier=Barrier(2);sends=[]
        assert not prepare()['new_order_post_attempted']
        for copier in (copy.copy,copy.deepcopy,pickle.dumps):
            with pytest.raises(TypeError):copier(prepared)
        def send(raw):
            sends.append(raw);return rpc('eth_sendRawTransaction',['0x'+raw.hex()])
        def contender(_):
            barrier.wait()
            try:return prepared.broadcast_once(send,now=clock[0])
            except DexExecutorError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(contender,range(2)))
        assert len(sends)==1 and sum(item is not None for item in result)==1
        assert next(item for item in result if item is not None)['send_result']=='HASH_MATCHED'
        evidence=collect_funding_receipt(rpc,tx_hash)
        with vault.session() as db:
            assert journal.check_return(db,review['id'],account_id=account,evidence=evidence,now=clock[0])['status']=='FINALIZED'
        assert not prepare()['new_order_post_attempted'] and len(sends)==1


def test_lost_native_post_response_recovers_the_original_receipt_after_reopen_without_another_signature(tmp_path):
    with return_case(tmp_path) as (case,plan,review,prepare):
        values,_,_,_,clock,_,_=case;vault,account,manager,draft,meta,rpc,_,_=values
        prepared=prepare();original=prepared.transaction_hash;sends=[]
        def lost(raw):
            sends.append(rpc('eth_sendRawTransaction',['0x'+raw.hex()]));raise TimeoutError('Controlled lost response')
        result=prepared.broadcast_once(lost,now=clock[0]);assert result['send_result']=='UNKNOWN' and sends==[original]
        with pytest.raises(DexExecutorError):prepared.broadcast_once(lost,now=clock[0])
        vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            assert journal.view_return(db,review['id'],now=clock[0])['status']=='BROADCAST_ATTEMPTED'
        evidence=collect_funding_receipt(rpc,original)
        with vault.session() as db:
            assert journal.check_return(db,review['id'],account_id=account,evidence=evidence,now=clock[0])['receipt']['transferred_wei']==str(plan.amount_wei)
        assert len(sends)==1;vault.backup(tmp_path/'lost-return-recovered.db')


@pytest.mark.parametrize('action',['draft','lock'])
def test_revocation_before_native_claim_prevents_late_send_and_only_unsent_bytes_can_be_abandoned(tmp_path,action):
    with return_case(tmp_path) as (case,plan,review,prepare):
        values,_,_,_,clock,_,_=case;vault,account,manager,draft,meta,rpc,_,_=values
        prepared=prepare();sends=[]
        if action=='draft':manager.revoke()
        else:vault.lock()
        with pytest.raises(DexExecutorError):prepared.broadcast_once(lambda raw:sends.append(raw),now=clock[0])
        assert not sends
        if not vault.unlocked:vault.unlock(PASSWORD)
        with vault.session() as db:
            assert journal.abandon_unsent(db,review['id'],account_id=account,now=clock[0])['status']=='ABANDONED_UNSENT'
        assert int(rpc('eth_getBalance',[draft['operator_address'],'latest']),16)==plan.snapshot.operator_balance
        vault.backup(tmp_path/'revoked-return.db')
