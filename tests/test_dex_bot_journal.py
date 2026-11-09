"""Causal closed-bar decision → exact bounded signature → actual EVM fill."""

from app.portfolio.vault import SCHEMA_VERSION
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
from dataclasses import replace

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_plan import DexReferenceQuote
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_operator import DexOperatorSession
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_operator_outbox import prepare_execution
from app.live_execution.dex_operator_transport import collect_operator_receipt
from app.live_execution.dex_allowance import allowance_cap,approval_transaction
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_trade import read_trade_plan
from app.portfolio import dex_bot_journal as bots,dex_operator_journal as journal,dex_bot_bindings as bindings
from tests.test_dex_gas_funding import case,now
from tests.test_dex_operator_connection import funded
from tests.test_dex_operator import write


@pytest.mark.parametrize('stop_stage',['none','before_signature_record','before_emission'])
def test_only_a_registered_causal_decision_can_trade_once_and_stop_blocks_a_signed_sale(tmp_path,stop_stage):
    with case(tmp_path,timeframe='1m',policy_changes={'max_buy_per_order':str(10**16+1)}) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        funded(values);nonce=int(deployment['nonce']);clock=[now()]
        def current():return read_executor_state(rpc,meta['owner'],nonce,500,now=clock[0],block_tag='finalized')
        write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
        for token,recipient in [('usdt',meta['owner']),('wbnb',meta['router'])]:
            write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**20])
        for token in ('USDT','WBNB'):
            state=current();cap=allowance_cap(state,token,now=clock[0])
            tx=approval_transaction(meta['owner'],nonce,token,cap,cap=cap,nonce=state.pending_nonce,gas=100000,gas_price=10**9)
            assert rpc('eth_getTransactionReceipt',[rpc('eth_sendTransaction',[tx])])['status']=='0x1'
        clock[0]=datetime.fromtimestamp((int(now().timestamp())//60+1)*60-.001,timezone.utc)
        rpc('evm_setTime',[int(clock[0].timestamp())*1000]);rpc('evm_mine',[])
        session=DexOperatorSession(vault);connection=DexOperatorConnection(vault,manager,session)
        try:
            connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
                wallet_address=meta['owner'],snapshot=read_funding_state(rpc,meta['owner'],nonce,500,now=clock[0]),ttl_seconds=900,now=clock[0])
            with vault.session() as db:
                source=bindings.binding_record(db,binding['id'])[1]['origin']['seed']
                while source[-1]['t']<int(clock[0].timestamp())//60*60-60:
                    source.append({**source[-1],'t':source[-1]['t']+60})
                bad_seed=[dict(bar) for bar in source];bad_seed[0]['wallet_address']=meta['owner']
                with pytest.raises(DexExecutorError):
                    bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                        connection=connection,snapshot=current(),seed=bad_seed,now=clock[0])
                db.commit()
                assert db.connection().exec_driver_sql('SELECT count(*) FROM real_dex_bot_runs').scalar_one()==0
                started,claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                    connection=connection,snapshot=current(),seed=source,now=clock[0])
            assert started['stored_status']=='ACTIVE' and not started['execution_enabled']
            clock[0]=datetime.fromtimestamp(int(clock[0].timestamp())//60*60+61,timezone.utc)
            rpc('evm_setTime',[int(clock[0].timestamp())*1000]);rpc('evm_mine',[])
            rows=source+[{'t':source[-1]['t']+60,'o':'97','h':'98','l':'95','c':'96','v':'10'}]
            trade=read_trade_plan(rpc,meta['owner'],nonce,500,buy=True,amount=10**16+1,slippage_bps=25,
                gas=400000,gas_price=10**9,max_gas_wei=10**15,now=clock[0])
            quote=DexReferenceQuote(Decimal('1'),Decimal('1.01'),clock[0],clock[0])
            with vault.session() as db:
                decision=bots.record_decision(db,started['id'],account_id=account,claim=claim,candles=rows,quote=quote,
                    snapshot=trade.snapshot,execution=trade,exit_cap_units=0,now=clock[0])
            assert decision['plan']['action']=='BUY' and decision['plan']['amount_units']==str(10**16+1)
            with pytest.raises(DexExecutorError):
                prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=binding['grant_id'],plan=trade,
                    decision_ref='ff'*32,now=clock[0])
            prepared=prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=binding['grant_id'],plan=trade,
                decision_ref=decision['decision_ref'],now=clock[0])
            hashes=[]
            def send(raw):
                result=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(result);return result
            assert prepared.broadcast_once(send,now=clock[0])['send_result']=='HASH_MATCHED'
            proof=collect_operator_receipt(rpc,meta['owner'],nonce,hashes[0])
            with vault.session() as db:
                settled=journal.check_intent(db,prepared.identifier,account_id=account,evidence=proof,now=clock[0])
                assert settled['status']=='FINALIZED' and int(settled['receipt']['owned_wbnb'])>0
            with pytest.raises(DexExecutorError):
                prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=binding['grant_id'],plan=trade,
                    decision_ref=decision['decision_ref'],now=clock[0])
            sale=read_trade_plan(rpc,meta['owner'],nonce,500,buy=False,amount=int(settled['receipt']['owned_wbnb']),slippage_bps=25,
                gas=400000,gas_price=10**9,max_gas_wei=10**15,now=clock[0])
            quote=DexReferenceQuote(Decimal('1.2'),Decimal('1.21'),clock[0],clock[0])
            with vault.session() as db:
                exit=bots.record_decision(db,started['id'],account_id=account,claim=claim,candles=rows,quote=quote,snapshot=sale.snapshot,
                    execution=sale,exit_cap_units=sale.amount,now=clock[0])
            assert exit['plan']['reason']=='TAKE_PROFIT'
            if stop_stage=='before_signature_record':
                identifier=str(uuid4())
                with vault.session() as db:
                    journal.reserve_intent(db,identifier=identifier,account_id=account,grant_id=binding['grant_id'],plan=sale,
                        decision_ref=exit['decision_ref'],now=clock[0])
                signature=session.sign_swap(sale,now=clock[0])
                with vault.session() as db:bots.stop_run(db,started['id'],account_id=account,now=clock[0])
                with pytest.raises(DexExecutorError):
                    with vault.session() as db:journal.record_signature(db,identifier,account_id=account,signed=signature,plan=sale,now=clock[0])
                with vault.session() as db:
                    saved,_,state=journal.intent_record(db,identifier)
                    assert saved.status=='RESERVED' and state['tx_ref'] is None
                assert len(hashes)==1
            else:
                signed_sale=prepare_execution(vault,session,identifier=str(uuid4()),account_id=account,grant_id=binding['grant_id'],plan=sale,
                    decision_ref=exit['decision_ref'],now=clock[0])
            if stop_stage=='before_emission':
                with vault.session() as db:bots.stop_run(db,started['id'],account_id=account,now=clock[0])
                with pytest.raises(DexExecutorError):signed_sale.broadcast_once(send,now=clock[0])
                assert len(hashes)==1
            elif stop_stage=='none':
                assert signed_sale.broadcast_once(send,now=clock[0])['send_result']=='HASH_MATCHED'
                proof=collect_operator_receipt(rpc,meta['owner'],nonce,hashes[-1])
                with vault.session() as db:
                    closed=journal.check_intent(db,signed_sale.identifier,account_id=account,evidence=proof,now=clock[0])
                    assert closed['receipt']['owned_wbnb']=='0' and closed['receipt']['cost_usdt']=='0'
                    # MEXC's 1.2 reference triggered TP; the controlled DEX
                    # actually returned 1.0. Never book the reference gain.
                    assert closed['receipt']['realized_pnl_usdt']=='0'
                with vault.session() as db:
                    after=bots.record_decision(db,started['id'],account_id=account,claim=claim,candles=rows,quote=quote,
                        snapshot=current(),execution=None,exit_cap_units=0,now=clock[0])
                    assert after['plan']['action']=='WAIT' and after['plan']['trailing_high'] is None
                with vault.session() as db:bots.stop_run(db,started['id'],account_id=account,now=clock[0])
                assert len(hashes)==2
            vault.backup(tmp_path/'causal.db')
            from tests.test_dex_deployments import PASSWORD
            from app.portfolio.vault import VaultError
            from app.portfolio.models import RealDexBotDecision,RealDexBotRun
            from app.portfolio.wallet_funding import _decoded,_encoded
            from sqlalchemy import select
            vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                assert bots.run_record(db,started['id'])[0].status=='STOPPED'
                recovered,lost_claim=bots.start_run(db,identifier=started['id'],account_id=account,binding_id=binding['id'],
                    wallet_address=meta['owner'],connection=connection,snapshot=None,seed=None,now=clock[0])
                assert recovered['stored_status']=='STOPPED' and not recovered['execution_enabled'] and lost_claim is None
                db.connection().exec_driver_sql('PRAGMA user_version=63');db.commit()
            with pytest.raises(VaultError,match='causal DEX bot'):vault.backup(tmp_path/'hidden-run.db')
            with vault.session() as db:db.connection().exec_driver_sql(f'PRAGMA user_version={SCHEMA_VERSION}');db.commit()
            # Rehash every decision and the mutable head. Replay must still
            # reject an invented amount, rather than trusting valid hashes.
            with vault.session() as db:
                run=db.get(RealDexBotRun,started['id']);previous=run.payload_digest
                for item in db.scalars(select(RealDexBotDecision).where(RealDexBotDecision.run_id==run.id).order_by(RealDexBotDecision.sequence_no)):
                    event=_decoded(item.payload_json,item.payload_digest)
                    if item.sequence_no==0:event['plan']['amount_units']=str(int(event['plan']['amount_units'])+1)
                    event['previous_digest']=previous;item.previous_digest=previous
                    item.payload_json,item.payload_digest=_encoded(event);previous=item.payload_digest
                state=_decoded(run.state_json,run.state_digest);state['head']['digest']=previous
                run.state_json,run.state_digest=_encoded(state);db.commit()
            with pytest.raises(VaultError,match='causal DEX bot'):vault.backup(tmp_path/'forged-run.db')
        finally:connection.revoke();session.revoke()


def test_schema_63_migration_keeps_existing_grants_and_funding_bytes(tmp_path):
    from sqlalchemy import select
    from app.portfolio.models import RealDexBotBinding,RealDexOperatorGrant,RealDexGasFundingIntent,RealDexControlIntent
    from tests.test_dex_deployments import PASSWORD
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        funded(values)
        def stored(db):return [(type(row).__name__,row.id,row.payload_json,row.state_json)
            for model in (RealDexBotBinding,RealDexOperatorGrant,RealDexGasFundingIntent,RealDexControlIntent)
            for row in db.scalars(select(model).order_by(model.id))]
        with vault.session() as db:
            before=stored(db)
            db.connection().exec_driver_sql('DROP TABLE real_dex_bot_decisions')
            db.connection().exec_driver_sql('DROP TABLE real_dex_bot_runs')
            db.connection().exec_driver_sql('PRAGMA user_version=63');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            assert stored(db)==before
        vault.backup(tmp_path/'migrated64.db')
