"""Registry liveness is RAM authority, never reconstructed from saved ACTIVE."""
from contextlib import nullcontext
from datetime import timedelta
from threading import Event
from uuid import uuid4
import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from tests.dex_bot_worker_fixture import running_case


def test_registry_starts_only_explicitly_scopes_stop_and_reopen_does_not_autorun(tmp_path):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        entered,release=Event(),Event();quote=market.get_paper_quote
        def blocked(symbol):entered.set();assert release.wait(5);return quote(symbol)
        market.get_paper_quote=blocked
        registry=DexBotSupervisor(vault,connection,clock=lambda:clock[0],worker_kwargs={'market':market,'rpc_factory':lambda:nullcontext(rpc)})
        before=registry.view(started['id'],account_id=account)
        assert before['runtime_status']=='REARM_REQUIRED' and not before['execution_enabled']
        with pytest.raises(DexExecutorError):
            registry.launch(started['id'],account_id=str(uuid4()),claim=claim,wallet_address=meta['owner'])
        assert registry.view(started['id'],account_id=account)['runtime_status']=='REARM_REQUIRED'
        clock[0]+=timedelta(seconds=1);mine()
        try:
            active=registry.launch(started['id'],account_id=account,claim=claim,wallet_address=meta['owner'])
            assert entered.wait(5) and active['execution_enabled']
            with pytest.raises(DexExecutorError):registry.stop(started['id'],account_id=str(uuid4()))
            assert registry.view(started['id'],account_id=account)['execution_enabled']
            with pytest.raises(DexExecutorError):registry.launch(started['id'],account_id=account,claim=claim,wallet_address=meta['owner'])
            stopped=registry.stop(started['id'],account_id=account)
            assert not stopped['execution_enabled'] and stopped['reason']=='MANUAL'
        finally:
            release.set();registry.shutdown();registry.join(timeout=5)
        reopened=DexBotSupervisor(vault,connection,clock=lambda:clock[0])
        assert reopened.view(started['id'],account_id=account)['runtime_status']=='STOPPED'
        assert not reopened.list_runs(account_id=account)[0]['execution_enabled']


def test_stop_acknowledges_key_revocation_when_the_bot_archive_cannot_be_read(tmp_path,monkeypatch):
    from app.portfolio import dex_bot_journal as bots
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values;entered,release=Event(),Event();quote=market.get_paper_quote
        def blocked(symbol):entered.set();assert release.wait(5);return quote(symbol)
        market.get_paper_quote=blocked
        registry=DexBotSupervisor(vault,connection,clock=lambda:clock[0],worker_kwargs={'market':market,'rpc_factory':lambda:nullcontext(rpc)})
        clock[0]+=timedelta(seconds=1);mine()
        try:
            registry.launch(started['id'],account_id=account,claim=claim,wallet_address=meta['owner']);assert entered.wait(5)
            def failure(*args,**kwargs):raise DexExecutorError('Archivo causal no verificable')
            monkeypatch.setattr(bots,'run_record',failure)
            stopped=registry.stop(started['id'],account_id=account)
            assert stopped['runtime_status']=='REVIEW_REQUIRED' and stopped['stored_status'] is None
            assert not stopped['execution_enabled'] and stopped['review_required'] and not connection.session.status()['connected']
        finally:release.set();registry.shutdown();registry.join(timeout=5)
