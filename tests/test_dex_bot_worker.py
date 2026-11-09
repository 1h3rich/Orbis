"""Continuous decision / exact EVM send / receipt / next decision."""
from contextlib import nullcontext
from datetime import timedelta
import pytest

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_operator_transport import BnbOperatorSender
from app.portfolio import dex_bot_journal as bots
from app.portfolio import dex_operator_journal as journal
from tests.dex_bot_worker_fixture import running_case


def test_worker_buys_then_waits_for_actual_receipt_before_selling_and_stops(tmp_path,monkeypatch):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,deployment,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc']);posts=[]
        def sender(plan):
            actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
            def send(raw):posts.append(plan.buy);return original(raw)
            actual.send=send;return actual
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,sender_factory=sender,clock=lambda:clock[0])
        assert not worker.status()['execution_enabled'] and not worker.status()['running']
        clock[0]+=timedelta(seconds=1);mine()
        bought=worker.run_once()
        assert bought['last_action']=='BUY' and bought['pending_receipt'] and posts==[True]
        market.bid,market.ask='1.2','1.21'
        received=worker.run_once()
        assert received['last_receipt_status']=='FINALIZED' and not received['pending_receipt'] and posts==[True]
        sold=worker.run_once()
        assert sold['last_action']=='SELL' and sold['pending_receipt'] and posts==[True,False]
        assert worker.run_once()['last_receipt_status']=='FINALIZED'
        assert worker.run_once()['last_action']=='WAIT' and posts==[True,False]
        worker.stop()
        assert worker.run_once()['reason']=='MANUAL' and not connection.session.status()['connected']
        with vault.session() as db:assert bots.run_record(db,started['id'])[0].status=='STOPPED'


@pytest.mark.parametrize('posted',[True,False])
def test_lost_response_never_repeats_an_order_and_only_a_receipt_changes_the_position(tmp_path,monkeypatch,posted):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc']);posts=[]
        def sender(plan):
            actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
            def send(raw):
                posts.append(raw)
                if posted:original(raw)
                raise TimeoutError('private provider failure must not escape')
            actual.send=send;return actual
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=sender,clock=lambda:clock[0])
        clock[0]+=timedelta(seconds=1);mine()
        result=worker.run_once()
        assert result['reason']=='OUTCOME_UNKNOWN' and result['pending_receipt'] and len(posts)==1
        assert not connection.session.status()['connected'] and 'private provider' not in str(result)
        seen=worker.run_once()
        assert seen['pending_receipt'] is not posted
        assert seen['last_receipt_status']==('FINALIZED' if posted else None)
        worker.run_once();assert len(posts)==1
        with vault.session() as db:
            from app.portfolio.models import RealDexOperatorIntent
            from sqlalchemy import select
            saved=db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==values[-1]['grant_id']))
            assert journal.intent_record(db,saved.id)[0].status==('FINALIZED' if posted else 'BROADCAST_ATTEMPTED')
            assert bots.run_record(db,started['id'])[0].status=='STOPPED'


@pytest.mark.parametrize('stage',['during_quote','after_reservation','before_post','after_claim'])
def test_stop_blocks_unclaimed_sends_but_preserves_an_already_claimed_inflight_order(tmp_path,monkeypatch,stage):
    from app.portfolio.models import RealDexOperatorIntent
    from sqlalchemy import select
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc']);posts=[]
        def sender(plan):
            actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
            if stage=='before_post':worker.stop()
            def send(raw):
                if stage=='after_claim':worker.stop()
                posts.append(True);return original(raw)
            actual.send=send;return actual
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=sender,clock=lambda:clock[0])
        if stage=='during_quote':
            original_quote=market.get_paper_quote
            def quote(symbol):worker.stop();return original_quote(symbol)
            market.get_paper_quote=quote
        if stage=='after_reservation':
            original_sign=connection.session.sign_swap
            def sign(plan,**kwargs):worker.stop();return original_sign(plan,**kwargs)
            monkeypatch.setattr(connection.session,'sign_swap',sign)
        clock[0]+=timedelta(seconds=1);mine()
        stopped=worker.run_once()
        assert stopped['reason']=='MANUAL' and len(posts)==int(stage=='after_claim')
        with vault.session() as db:
            rows=list(db.scalars(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==values[-1]['grant_id'])))
            if stage=='during_quote':assert rows==[]
            else:assert rows[0].status==('BROADCAST_ATTEMPTED' if stage=='after_claim' else 'ABANDONED_UNSENT')
        worker.run_once()
        assert len(posts)==int(stage=='after_claim')
        if stage=='after_claim':assert worker.status()['last_receipt_status']=='FINALIZED'


def test_background_worker_reports_actual_liveness_and_stop_does_not_wait_for_market_http(tmp_path):
    from threading import Event
    from app.live_execution.dex_artifact import DexExecutorError
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        entered,release=Event(),Event();posts=[];quote=market.get_paper_quote
        def blocked_quote(symbol):
            entered.set();assert release.wait(5);return quote(symbol)
        market.get_paper_quote=blocked_quote
        def sender(_):posts.append(True);raise AssertionError('STOP must precede any send')
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=sender,clock=lambda:clock[0])
        clock[0]+=timedelta(seconds=1);mine()
        try:
            worker.start(interval_seconds=.1)
            assert entered.wait(5)
            assert worker.status()['running'] and worker.status()['execution_enabled']
            stopped=worker.stop()
            assert stopped['reason']=='MANUAL' and not stopped['execution_enabled'] and not posts
        finally:
            release.set();worker.join(timeout=5)
        assert not worker.status()['running'] and not posts
        with pytest.raises(DexExecutorError):worker.start(interval_seconds=.1)


@pytest.mark.parametrize('fault,reason',[('expired','PERMISSION_EXPIRED'),('gap','RUN_PAUSED'),
    ('market','MARKET_UNAVAILABLE'),('reopened','SESSION_CHANGED'),('policy','STATE_CHANGED')])
def test_expiry_observation_gaps_bad_market_new_vault_and_policy_changes_cannot_send(tmp_path,fault,reason):
    from app.live_execution.dex_state import read_executor_state
    from app.live_execution.dex_policy import configure_transaction
    from tests.test_dex_deployments import PASSWORD
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,deployment,_=values;posts=[]
        def sender(_):posts.append(True);raise AssertionError('This fault cannot emit an order')
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=sender,clock=lambda:clock[0])
        clock[0]+=timedelta(seconds={'expired':901,'gap':31}.get(fault,1));mine()
        if fault=='market':market.get_paper_quote=lambda _:{'symbol':'BTC/USDT'}
        elif fault=='reopened':vault.lock();vault.unlock(PASSWORD)
        elif fault=='policy':
            state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
            tx=configure_transaction(meta['owner'],int(deployment['nonce']),state.operator,state.policy_id,state.policy_limits,
                nonce=state.pending_nonce,gas=1000000,gas_price=10**9,now=int(clock[0].timestamp()),owned_wbnb=0)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
        result=worker.run_once()
        assert result['reason']==reason and not result['pending_receipt'] and not posts
        assert not connection.session.status()['connected']
        with vault.session() as db:
            assert bots.run_record(db,started['id'])[0].status==('ACTIVE' if fault=='reopened' else 'STOPPED')


def test_receipt_must_be_finalized_before_another_market_decision(tmp_path,monkeypatch):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc']);lagging=[False];posts=[]
        def read(method,params):
            actual=rpc(method,params)
            if lagging[0] and method=='eth_getBlockByNumber' and params[0]=='finalized':
                return rpc('eth_getBlockByNumber',[hex(int(actual['number'],16)-1),False])
            return actual
        def sender(plan):
            posts.append(plan.buy);return BnbOperatorSender(plan,clock=lambda:clock[0])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(read),sender_factory=sender,clock=lambda:clock[0])
        clock[0]+=timedelta(seconds=1);mine();worker.run_once();lagging[0]=True
        market.bid,market.ask='1.2','1.21'
        waiting=worker.run_once()
        assert waiting['pending_receipt'] and waiting['last_receipt_status'] is None and posts==[True]
        lagging[0]=False
        assert not worker.run_once()['pending_receipt'] and posts==[True]
        worker.run_once();assert posts==[True,False]
        worker.stop();worker.run_once()


def test_stop_releases_signing_immediately_while_an_already_claimed_post_is_waiting(tmp_path,monkeypatch):
    from threading import Event,Thread
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        posting,release,stopped=Event(),Event(),Event();posts=[]
        def sender(plan):
            actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
            def send(raw):
                posting.set();assert release.wait(5);posts.append(True);return original(raw)
            actual.send=send;return actual
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=sender,clock=lambda:clock[0])
        clock[0]+=timedelta(seconds=1);mine();stopper=None
        try:
            worker.start(interval_seconds=.1);assert posting.wait(5)
            def stop():worker.stop();stopped.set()
            stopper=Thread(target=stop);stopper.start()
            assert stopped.wait(1) and not connection.session.status()['connected']
            assert not posts and not worker.status()['execution_enabled']
        finally:
            release.set();worker.join(timeout=5)
            if stopper is not None:stopper.join(5)
        assert posts==[True] and worker.status()['last_receipt_status']=='FINALIZED'
        assert not worker.status()['running']


def test_an_archive_failure_cannot_keep_the_key_connected_or_claim_a_recorded_stop(tmp_path,monkeypatch):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        def failure(*args,**kwargs):raise RuntimeError('sensitive diagnostic must not escape')
        monkeypatch.setattr(bots,'stop_run',failure)
        result=worker.stop()
        assert not connection.session.status()['connected'] and result['review_required'] and not result['stop_recorded']
        assert 'sensitive diagnostic' not in str(result)
