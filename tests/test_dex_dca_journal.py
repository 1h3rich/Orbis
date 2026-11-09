"""DCA → exact bounded send → receipts and replay; own disposable EVM only."""
from contextlib import nullcontext
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_bot_market import reference_quote
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_operator_transport import BnbOperatorSender,collect_operator_receipt
from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio import dex_operator_journal as operator
from app.portfolio.vault import VaultError
from app.portfolio import dex_bot_journal as bots
from app.portfolio.models import RealDexBotDecision
from app.portfolio.wallet_funding import _decoded,_encoded
import pytest
from tests.dex_dca_worker_fixture import dca_running_case
from tests.dex_bot_source_fixture import DEX_EMA_RULE
from tests.test_dex_deployments import PASSWORD


@pytest.mark.parametrize('average_rule,expected', [(None,2*10**16),(DEX_EMA_RULE,75*10**14)], ids=['legacy','ema'])
def test_dca_worker_confirms_base_and_dip_with_cooldown_then_recovers_the_same_ledger(tmp_path,monkeypatch,average_rule,expected):
    with dca_running_case(tmp_path,average_rule=average_rule) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        assert started['engine']=='orbis.dex.bot.dca.v1'
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine()
            assert worker.run_once()['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            with vault.session() as db:archive=bots.run_record(db,started['id'])[3]
            snapshot=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
            with vault.session() as db:
                preview=bots.preview_decision(db,started['id'],account_id=account,claim=claim,
                    candles=[bar.json() for bar in archive['bars']],quote=reference_quote(market.get_paper_quote('BNB/USDT'),observed_at=clock[0]),
                    snapshot=snapshot,exit_cap_units=0,now=clock[0])
                assert preview.reason=='ENTRY_COOLDOWN'
            waiting=worker.run_once();assert waiting['last_action']=='WAIT',waiting
            for _ in range(7):
                clock[0]+=timedelta(seconds=10);mine();current=worker.run_once()
                if current['last_action']=='BUY':break
            assert current['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            assert worker.run_once()['last_action']=='WAIT'
            worker.stop()
            physical=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
            assert physical.owned_wbnb==expected and physical.balance_wbnb==10**20+expected
            with vault.session() as db:
                run,terms,state,archive=bots.run_record(db,started['id'])
                assert run.status=='STOPPED'
                assert archive['dca_projection'].total_spent_units==expected
                assert archive['dca_projection'].monthly_spent_units=={clock[0].strftime('%Y-%m'):expected}
                buys=[_decoded(row.payload_json,row.payload_digest)['plan'] for row in db.scalars(
                    select(RealDexBotDecision).where(RealDexBotDecision.run_id==run.id).order_by(RealDexBotDecision.sequence_no))
                    if _decoded(row.payload_json,row.payload_digest)['plan']['action']=='BUY']
                assert [row['dca']['kind'] for row in buys]==['BASE','DIP']
                assert len({row['dca']['key'] for row in buys})==2
                if average_rule:
                    assert [row['dca']['requested_quote'] for row in buys]==['0.00500','0.00250']
                    assert all(row['evidence']['average']['amount_factor']=='0.00005' for row in buys)
                    assert archive['last_average']['signal']['ma_kind']=='ema'
            from app.portfolio import dex_receipt_booking as accounting
            from app.portfolio.wallet_links import save_wallet_link
            from app.portfolio.repository import append_event,list_events
            from app.portfolio.domain import EventInput
            from decimal import Decimal
            with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
            for currency,amount in [('USDT','100'),('BNB','0.01')]:
                with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION',currency,clock[0]-timedelta(days=1),amount=Decimal(amount)))
            with vault.session() as db:receipts=[item for item in accounting.list_receipts(db,account_id=account,wallet_address=meta['owner']) if item['source_kind']=='OPERATOR']
            assert len(receipts)==2
            linked=[]
            for item in receipts:
                with vault.session() as db:preview=accounting.preview_booking(db,'OPERATOR',item['source_id'],context_account_id=account,wallet_address=meta['owner'])
                assert preview['trade_candidates']==[]  # Identical nearby buys are distinct fills, never one reused event.
                with vault.session() as db:
                    booked=accounting.book_receipt(db,'OPERATOR',item['source_id'],context_account_id=account,wallet_address=meta['owner'],
                        expected_revision=preview['revision'],preview_digest=preview['preview_digest'],now=clock[0])
                    linked+=booked['event_ids']
            assert len(linked)==len(set(linked))==4
            with vault.session() as db:assert len([event for event in list_events(db,account) if event.input.kind=='BUY'])==2
            from app.automation.access import automation_access
            automation_access.lock(tmp_path/'source'/'dca.db')
            vault.backup(tmp_path/'dca-ledger.db');vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                recovered=bots.run_record(db,started['id'])
                assert recovered[3]['dca_projection'].total_spent_units==expected
                assert not bots._public(*recovered[:3])['automatic_execution']
        finally:worker.stop()


def test_dex_worker_zero_amount_archives_signal_without_creating_a_swap(tmp_path,monkeypatch):
    opposite={'ma_adjustment':DEX_EMA_RULE['ma_adjustment']|{'condition':'above'}}
    with dca_running_case(tmp_path,average_rule=opposite) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        sends=[]

        def sender(plan):
            sends.append(plan)
            pytest.fail('A zero amount cannot request a signer or sender')

        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0],sender_factory=sender)
        try:
            clock[0]+=timedelta(seconds=1);mine()
            assert worker.run_once()['last_action']=='WAIT'
            with vault.session() as db:
                archive=bots.run_record(db,started['id'])[3]
                assert archive['last_average']['reason']=='DCA_MA_ZERO_AMOUNT'
                assert archive['last_average']['signal']['amount_factor']=='0'
                assert not archive['dca_projection'].consumed_keys and not archive['buy_decisions']
            assert not sends
        finally:worker.stop()


def test_lost_dca_post_response_recovers_only_the_original_receipt_without_rewriting_decisions(tmp_path,monkeypatch):
    with dca_running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values;hashes=[]
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def sender(plan):
            actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
            def send(raw):
                hashes.append(original(raw));raise TimeoutError('Controlled lost response')
            actual.send=send;return actual
        def read(method,params):return None if method=='eth_getTransactionReceipt' else rpc(method,params)
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(read),sender_factory=sender,clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine();unknown=worker.run_once()
            assert unknown['reason']=='OUTCOME_UNKNOWN' and unknown['pending_receipt'] and len(hashes)==1
            worker.run_once();assert len(hashes)==1
            with pytest.raises(DexExecutorError,match='pendiente|resolver|cambi'):
                connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],wallet_address=meta['owner'],
                    snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),ttl_seconds=900,now=clock[0])
            with vault.session() as db:
                before=[(row.id,row.payload_json,row.payload_digest) for row in db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==started['id']))]
                archived=bots.run_record(db,started['id'])
                assert archived[3]['dca_projection'].total_spent_units==0 and archived[3]['dca_projection'].pending
                intent=operator._identity(db,'tx',hashes[0])
                from app.portfolio.models import RealDexOperatorIntent
                identifier=next(row.id for row in db.scalars(select(RealDexOperatorIntent)) if operator.intent_record(db,row.id)[2]['tx_ref']==intent)
            vault.lock();vault.unlock(PASSWORD)
            proof=collect_operator_receipt(rpc,meta['owner'],int(deployment['nonce']),hashes[0])
            with vault.session() as db:
                assert operator.check_intent(db,identifier,account_id=account,evidence=proof,now=clock[0])['status']=='FINALIZED'
                after=bots.run_record(db,started['id'])
                assert after[3]['dca_projection'].total_spent_units==10**16
                assert not after[3]['dca_projection'].pending and after[0].status=='STOPPED'
                assert before==[(row.id,row.payload_json,row.payload_digest) for row in db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==started['id']))]
            assert len(hashes)==1;vault.backup(tmp_path/'recovered-dca.db')
        finally:worker.stop()


def test_rearm_does_not_buy_a_daily_close_that_was_downloaded_after_the_bot_stopped(tmp_path,monkeypatch):
    with dca_running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        worker.stop();clock[0]+=timedelta(seconds=61);mine()
        connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],wallet_address=meta['owner'],
            snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),ttl_seconds=900,now=clock[0])
        state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0],block_tag='finalized')
        with vault.session() as db:
            resumed,new_claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                connection=connection,snapshot=state,seed=[dict(row) for row in market.rows],now=clock[0],
                permission_mode='SESSION',requested_seconds=900,previous_run_id=started['id'])
        worker=DexBotWorker(vault,connection,run_id=resumed['id'],account_id=account,claim=new_claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:
            assert worker.run_once()['last_action']=='WAIT'
            with vault.session() as db:
                archive=bots.run_record(db,resumed['id'])[3]
                event=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.run_id==resumed['id']))
                assert _decoded(event.payload_json,event.payload_digest)['plan']['reason']=='DCA_WAITING_NEXT_CANDLE'
                assert archive['dca_projection'].total_spent_units==0 and not archive['buy_decisions']
                assert archive['cursor'].reference=='97'
            worker.stop();vault.backup(tmp_path/'unobserved-day-dca.db')
        finally:worker.stop()


@pytest.mark.parametrize('forgery',['omit','abandon','budget'])
def test_rehashed_dca_witness_or_budget_forgery_is_rejected_by_reads_and_backup(tmp_path,monkeypatch,forgery):
    from app.portfolio.models import RealDexBotRun
    with dca_running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='FINALIZED';assert worker.run_once()['last_action']=='WAIT'
            worker.stop()
            with vault.session() as db:
                row=db.get(RealDexBotRun,started['id']);saved_run=(row.payload_json,row.payload_digest,row.state_json,row.state_digest)
                last=db.scalar(select(RealDexBotDecision).where(RealDexBotDecision.run_id==row.id).order_by(RealDexBotDecision.sequence_no.desc()))
                last_id=last.id;saved_event=(last.payload_json,last.payload_digest)
                if forgery=='budget':
                    terms=_decoded(row.payload_json,row.payload_digest);terms['dca']['limits']['total_budget_units']+=1
                    row.payload_json,row.payload_digest=_encoded(terms)
                else:
                    event=_decoded(last.payload_json,last.payload_digest)
                    if forgery=='omit':event['attempts']=[]
                    else:event['attempts'][0]['status']='ABANDONED_UNSENT'
                    last.payload_json,last.payload_digest=_encoded(event)
                    state=_decoded(row.state_json,row.state_digest);state['head']['digest']=last.payload_digest
                    row.state_json,row.state_digest=_encoded(state)
                db.commit()
            with pytest.raises(DexExecutorError):
                with vault.session() as db:bots.run_record(db,started['id'])
            with pytest.raises(VaultError):vault.backup(tmp_path/'bad-dca.db')
            with vault.session() as db:
                row=db.get(RealDexBotRun,started['id']);row.payload_json,row.payload_digest,row.state_json,row.state_digest=saved_run
                last=db.get(RealDexBotDecision,last_id);last.payload_json,last.payload_digest=saved_event;db.commit()
            vault.backup(tmp_path/'fixed-dca.db')
        finally:worker.stop()


def test_rearm_keeps_base_key_and_budget_then_executes_only_the_pending_dip(tmp_path,monkeypatch):
    with dca_running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            worker.stop();clock[0]+=timedelta(seconds=1);mine()
            connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],wallet_address=meta['owner'],
                snapshot=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0]),ttl_seconds=900,now=clock[0])
            with vault.session() as db:old=bots.run_record(db,started['id']);seed=[bar.json() for bar in old[3]['bars']]
            snapshot=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0],block_tag='finalized')
            with vault.session() as db:
                resumed,new_claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                    connection=connection,snapshot=snapshot,
                    seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=900,previous_run_id=started['id'])
            assert resumed['previous_run_id']==started['id']
            worker=DexBotWorker(vault,connection,run_id=resumed['id'],account_id=account,claim=new_claim,wallet_address=meta['owner'],
                market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
            assert worker.run_once()['last_action']=='WAIT'
            for _ in range(7):
                clock[0]+=timedelta(seconds=10);mine();current=worker.run_once()
                if current['last_action']=='BUY':break
            assert current['last_action']=='BUY';assert worker.run_once()['last_receipt_status']=='FINALIZED'
            assert worker.run_once()['last_action']=='WAIT';worker.stop()
            from app.live_execution.dex_executions import read_executions
            with vault.session() as db:
                prefix=read_executions(db,account_id=account,run_id=started['id'])
                continuation=read_executions(db,account_id=account,run_id=resumed['id'])
            assert prefix['total']==1 and continuation['total']==2
            assert [fill['side'] for fill in continuation['executions']]==['BUY','BUY']
            assert [fill['quantity_base'] for fill in continuation['executions']]==['0.01','0.01']
            assert [fill['activation_kind'] for fill in continuation['executions']]==['BASE','DIP']
            assert len({fill['activation_key'] for fill in continuation['executions']})==2

            with vault.session() as db:
                current=bots.run_record(db,resumed['id'])
                assert current[3]['dca_projection'].total_spent_units==2*10**16
                assert len(current[3]['dca_projection'].consumed_keys)==2
                events=[_decoded(row.payload_json,row.payload_digest) for row in db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==resumed['id']))]
                assert [event['plan']['dca']['kind'] for event in events if event['plan']['action']=='BUY']==['DIP']
            vault.backup(tmp_path/'rearmed-dca.db');vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:assert bots.run_record(db,resumed['id'])[3]['dca_projection'].total_spent_units==2*10**16
        finally:worker.stop()
