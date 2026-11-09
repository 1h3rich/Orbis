"""Revocation is immediate; the worker finalizer persists after a busy read."""
from contextlib import nullcontext
from threading import Event,Thread
from time import monotonic

from app.live_execution.dex_bot_worker import DexBotWorker
from app.portfolio import dex_bot_journal as bots
from tests.dex_bot_worker_fixture import running_case


def test_stopped_thread_records_revocation_after_temporary_vault_contention(tmp_path,monkeypatch):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,
            wallet_address=meta['owner'],market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        completed,entered,release=Event(),Event(),Event();generation=vault.session_generation
        revision=connection.session.permission_revision;original=worker.run_once
        def once():
            result=original();completed.set();return result
        worker.run_once=once
        def hold():
            with vault.guard_session_generation(generation):entered.set();assert release.wait(10)
        holder=Thread(target=hold)
        try:
            worker.start(interval_seconds=10);assert completed.wait(5)
            holder.start();assert entered.wait(3)
            at=monotonic();stopped=worker.stop()
            assert monotonic()-at<1 and not stopped['execution_enabled']
            assert connection.session.permission_revision!=revision and not stopped['stop_recorded']
            worker.join(timeout=.1)
        finally:
            release.set();holder.join(3);worker.join(timeout=5)
        assert not worker.alive
        with vault.session() as db:assert bots.run_record(db,started['id'])[0].status=='STOPPED'
        assert worker.status()['stop_recorded'] and not worker.status()['review_required']
