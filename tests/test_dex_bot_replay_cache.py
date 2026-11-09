"""A verified immutable prefix is reused, but edits still require full replay."""
from decimal import Decimal
from datetime import timedelta
import pytest
from sqlalchemy import select

from app.live_execution.dex_artifact import DexExecutorError

from app.live_execution.dex_bot_plan import DexReferenceQuote
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_bot_journal as bots
from tests.dex_bot_worker_fixture import running_case


def test_unchanged_history_is_not_evaluated_again_and_backup_still_replays_it(tmp_path,monkeypatch):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,deployment,_=values
        snapshot=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
        quote=DexReferenceQuote(Decimal('1'),Decimal('1.01'),clock[0],clock[0])
        with vault.session() as db:seed=[bar.json() for bar in bots.run_record(db,started['id'])[3]['bars']]
        for _ in range(12):
            with vault.session() as db:
                result=bots.record_decision(db,started['id'],account_id=account,claim=claim,candles=seed,quote=quote,
                    snapshot=snapshot,execution=None,exit_cap_units=0,now=clock[0])
            assert result['plan']['action']=='WAIT'
        calls=[];original=bots._replay_event
        def replay(*args,**kwargs):
            # Instrument actual semantic verification, without replacing it.
            calls.append(True);return original(*args,**kwargs)
        monkeypatch.setattr(bots,'_replay_event',replay)
        for _ in range(4):
            with vault.session() as db:
                row,_,state,archive=bots.run_record(db,started['id'])
                assert state['head']['sequence_no']==11 and len(archive['bars'])==len(seed)
        assert calls==[]
        vault.backup(tmp_path/'verified-history.db')
        assert len(calls)>=12
        from tests.test_dex_deployments import PASSWORD
        calls.clear();vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert bots.run_record(db,started['id'])[2]['head']['sequence_no']==11
        assert len(calls)>=12


@pytest.mark.parametrize('change',['raw-bytes','rehash-plan','metadata','delete'])
def test_a_cached_prefix_does_not_hide_old_decision_edits_or_deletion(tmp_path,change):
    from app.portfolio.models import RealDexBotDecision,RealDexBotRun
    from app.portfolio.wallet_funding import _decoded,_encoded
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,deployment,_=values
        snapshot=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
        with vault.session() as db:
            seed=[bar.json() for bar in bots.run_record(db,started['id'])[3]['bars']]
            bots.record_decision(db,started['id'],account_id=account,claim=claim,candles=seed,
                quote=DexReferenceQuote(Decimal('1'),Decimal('1.01'),clock[0],clock[0]),snapshot=snapshot,
                execution=None,exit_cap_units=0,now=clock[0])
        with vault.session() as db:assert bots.run_record(db,started['id'])[2]['head']['sequence_no']==0
        with vault.session() as db:
            item=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.run_id==started['id']))
            if change=='raw-bytes':item.payload_json+=' '
            elif change=='metadata':item.created_at+=timedelta(seconds=1)
            elif change=='delete':db.delete(item)
            else:
                event=_decoded(item.payload_json,item.payload_digest);event['plan']['amount_units']='1'
                item.payload_json,item.payload_digest=_encoded(event)
                row=db.get(RealDexBotRun,started['id']);state=_decoded(row.state_json,row.state_digest)
                state['head']['digest']=item.payload_digest;row.state_json,row.state_digest=_encoded(state)
            db.commit()
        with pytest.raises(DexExecutorError):
            with vault.session() as db:bots.run_record(db,started['id'])


def test_rehashing_a_receipt_referenced_by_the_cached_prefix_is_still_rejected(tmp_path,monkeypatch):
    from contextlib import nullcontext
    from app.live_execution.dex_bot_worker import DexBotWorker
    from app.portfolio.models import RealDexOperatorIntent
    from app.portfolio.wallet_funding import _decoded,_encoded
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        clock[0]+=timedelta(seconds=1);mine()
        assert worker.run_once()['pending_receipt']
        assert worker.run_once()['last_receipt_status']=='FINALIZED'
        assert worker.run_once()['last_action']=='WAIT'
        with vault.session() as db:assert len(bots.run_record(db,started['id'])[3]['settled'])==1
        with vault.session() as db:
            item=db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==values[-1]['grant_id']))
            state=_decoded(item.state_json,item.state_digest);state['receipt']['gas_fee_wei']=str(int(state['receipt']['gas_fee_wei'])+1)
            item.state_json,item.state_digest=_encoded(state);db.commit()
        with pytest.raises(DexExecutorError):
            with vault.session() as db:bots.run_record(db,started['id'])
        worker.stop()
