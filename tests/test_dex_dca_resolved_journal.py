"""Native DCA replacements, explicit rearm, next activation and encrypted reopen."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from eth_account import Account
from eth_utils import to_checksum_address
import pytest
from sqlalchemy import select

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_gas_funding import read_funding_state
from app.live_execution.dex_executions import read_executions
from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio import dex_bot_journal as bots,dex_dca_journal as dca
from app.portfolio import dex_call_resolution as resolutions,dex_operator_journal as operator
from app.portfolio import dex_receipt_booking as ledger
from app.portfolio.domain import EventInput,PortfolioError
from app.portfolio.repository import create_account,append_event,account_snapshot
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.models import RealDexOperatorIntent
from app.portfolio.wallet_funding import _encoded
from tests.dex_dca_worker_fixture import dca_running_case
from tests.test_dex_deployments import PASSWORD


@pytest.mark.parametrize(('outcome','resolve_second'),[
    ('EXECUTED',False),('CANCELLED',False),('REVERTED',False),('CANCELLED',True)])
def test_dca_resolution_preserves_keys_spend_gas_and_explicit_next_activation(tmp_path,monkeypatch,outcome,resolve_second):
    cancel=outcome=='CANCELLED';executed=outcome=='EXECUTED'
    with dca_running_case(tmp_path,evm_backend='anvil',policy_changes={'max_gas_price':str(2*10**9)}) as case:
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def worker(identifier,claim):
            return DexBotWorker(vault,connection,run_id=identifier,account_id=account,claim=claim,wallet_address=meta['owner'],
                market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        current=worker(started['id'],claim)
        clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
        assert current.run_once()['pending_receipt']
        with vault.session() as db:
            raw=db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==binding['grant_id']))
            parent,terms,state,source=resolutions.source_record(db,'OPERATOR',raw.id,owner=meta['owner'])
            original=(parent.payload_json,parent.state_json,parent.updated_at)
        tx=source.expected_call()
        fields={'to':to_checksum_address(draft['operator_address'] if cancel else tx['to']),
            'data':'0x' if cancel else tx['data'],'value':0,'chainId':56,'nonce':source.nonce,
            'gas':21000 if cancel else int(terms['gas'])+1000,'gasPrice':1200000000}
        if outcome=='REVERTED':fields.update(data='0xdeadbeef',gas=60000)
        with manager._copy(draft['id'],account,meta['owner'],clock[0]) as (_,key):signed=Account.sign_transaction(fields,bytes(key))
        replacement=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
        clock[0]+=timedelta(seconds=2);mine();rpc('miner_start',[1]);current.stop()
        clock[0]+=timedelta(seconds=1)
        proof=collect_call_resolution(rpc,source,replacement)
        scope=dict(account_id=account,wallet_address=meta['owner'],evidence=proof,accept_changes=True,now=clock[0])
        with vault.session() as db:preview=resolutions.preview_resolution(db,'OPERATOR',parent.id,**scope)
        with vault.session() as db:
            resolutions.confirm_resolution(db,'OPERATOR',parent.id,preview_digest=preview['preview_digest'],
                source_state_digest=preview['source_state_digest'],**scope)
        supervisor=DexBotSupervisor(vault,connection,clock=lambda:clock[0])
        view=supervisor.view(started['id'],account_id=account)
        base_key='BASE:'+clock[0].strftime('%Y-%m')
        assert view['dca']['consumed_keys']==[base_key]
        assert view['dca']['spent_units']==str(10**16 if executed else 0)
        assert view['dca']['gas_spent_wei']==preview['receipt']['gas_fee_wei']
        assert view['worker']['pending_receipt'] is False
        assert view['worker']['last_receipt_status']==('FINALIZED' if executed else outcome)
        assert view['intents'][0]['effective_outcome']==outcome
        with vault.session() as db:
            row,definition,_,archive=bots.run_record(db,started['id'],verify_full=True)
            assert definition['schema']==dca.ENGINE
            assert len(archive['buy_decisions'])==1
            budget=operator.grant_view(db,binding['grant_id'],now=clock[0])
            fills=read_executions(db,account_id=account,run_id=row.id)
            assert fills['pending_count']==0 and fills['cancelled_count']==int(cancel)
            assert fills['reverted_count']==int(outcome=='REVERTED')
            assert fills['total']==int(executed)
            assert fills['cancelled_gas_wei']==(preview['receipt']['gas_fee_wei'] if cancel else '0')
            if executed:assert fills['executions'][0]['receipt_kind']=='CALL_RESOLUTION'
            # Historical observations cannot adopt a later acceptance, even
            # though the receipt and its independent journal now exist.
            earlier=scope['now']-timedelta(microseconds=1)
            witnesses=dca._live_witnesses(db,row,archive['buy_decisions'],engine=dca.OUTCOME_ENGINE,at=earlier)
            assert witnesses[0]['status']=='PENDING' and witnesses[0]['resolution'] is None
            attempts=dca._attempts(db,row,archive['buy_decisions'],witnesses,[],definition['baseline'],earlier,engine=dca.OUTCOME_ENGINE)
            assert attempts[0].gas_fee_wei==0 and attempts[0].executed_at is None
            current_witness=dca._live_witnesses(db,row,archive['buy_decisions'],engine=dca.OUTCOME_ENGINE,at=scope['now'])
            forged=deepcopy(current_witness);forged[0]['resolution']=None
            with pytest.raises(ValueError,match='outcome witness'):
                dca._attempts(db,row,archive['buy_decisions'],forged,[parent.id],
                    {'block_number':preview['receipt']['block_number'],'block_hash':preview['receipt']['block_hash']},scope['now'],engine=dca.OUTCOME_ENGINE)
        clock[0]+=timedelta(seconds=1);mine()
        funding=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
        connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
            wallet_address=meta['owner'],snapshot=funding,ttl_seconds=900,now=clock[0])
        with vault.session() as db:
            seed=[bar.json() for bar in bots.run_record(db,started['id'])[3]['bars']]
            resumed,next_claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                connection=connection,snapshot=funding.executor,seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=900,
                previous_run_id=started['id'])
            assert resumed['engine']==dca.OUTCOME_ENGINE
            child,child_terms,_,history=bots.run_record(db,resumed['id'])
            assert child_terms['resume']['resolved_calls'][0]['intent_id']==parent.id
            assert operator.grant_view(db,binding['grant_id'],now=clock[0])==budget
            assert history['carried_attempts'][0].status==('FINALIZED' if executed else outcome)
            # Rehashing a forged reference cannot make it a valid causal witness.
            before=(child.payload_json,child.payload_digest)
            altered=deepcopy(child_terms);altered['resume']['resolved_calls'][0]['payload_digest']='f'*64
            child.payload_json,child.payload_digest=_encoded(altered);db.flush()
            with pytest.raises(DexExecutorError,match='causal|coherente'):bots.run_record(db,child.id,verify_full=True)
            child.payload_json,child.payload_digest=before;db.flush();db.rollback()
        current=worker(resumed['id'],next_claim)
        # The new, separately identified DIP may execute; the consumed BASE
        # cannot repeat. An executed BASE retains its on-chain cooldown.
        for _ in range(7):
            clock[0]+=timedelta(seconds=10);mine()
            if resolve_second:rpc('miner_stop',[])
            tick=current.run_once()
            if tick['last_action']=='BUY':break
            assert tick['last_action']=='WAIT',tick
        assert tick['last_action']=='BUY',tick
        if resolve_second:
            with vault.session() as db:
                raw=db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==binding['grant_id'],RealDexOperatorIntent.id!=parent.id))
                second,recipe,_,source=resolutions.source_record(db,'OPERATOR',raw.id,owner=meta['owner'])
            fields={'to':to_checksum_address(draft['operator_address']),'value':0,'data':'0x','chainId':56,
                'nonce':source.nonce,'gas':21000,'gasPrice':1200000000}
            with manager._copy(draft['id'],account,meta['owner'],clock[0]) as (_,key):signed=Account.sign_transaction(fields,bytes(key))
            replacement=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])
            clock[0]+=timedelta(seconds=2);mine();rpc('miner_start',[1]);current.stop();clock[0]+=timedelta(seconds=1)
            evidence=collect_call_resolution(rpc,source,replacement)
            scope=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,accept_changes=True,now=clock[0])
            with vault.session() as db:proposal=resolutions.preview_resolution(db,'OPERATOR',second.id,**scope)
            with vault.session() as db:
                resolutions.confirm_resolution(db,'OPERATOR',second.id,preview_digest=proposal['preview_digest'],
                    source_state_digest=proposal['source_state_digest'],**scope)
        else:
            assert current.run_once()['last_receipt_status']=='FINALIZED'
            assert current.run_once()['last_action']=='WAIT'
            current.stop()
        final=supervisor.view(resumed['id'],account_id=account)
        assert len(final['dca']['consumed_keys'])==2 and base_key in final['dca']['consumed_keys']
        assert final['dca']['spent_units']==str((int(executed)+int(not resolve_second))*10**16)
        assert int(final['dca']['gas_spent_wei'])>int(preview['receipt']['gas_fee_wei'])
        with vault.session() as db:
            owner=create_account(db,'Wallet propia','USDT').id
            save_wallet_link(db,owner,meta['owner'],'0x38','Principal');db.commit()
        for currency,amount in [('USDT','100'),('BNB','0.01')]:
            with vault.session() as db:append_event(db,owner,str(uuid4()),EventInput('CONTRIBUTION',currency,clock[0]-timedelta(days=1),amount=Decimal(amount)))
        first_id=resolutions.identifier('OPERATOR',parent.id)
        second_kind='CALL_RESOLUTION' if resolve_second else 'OPERATOR'
        second_id=resolutions.identifier('OPERATOR',second.id) if resolve_second else final['intents'][0]['id']
        scope=dict(context_account_id=account,wallet_address=meta['owner'])
        if executed:
            with pytest.raises(PortfolioError,match='anterior'):
                with vault.session() as db:ledger.preview_booking(db,second_kind,second_id,**scope)
        for kind,identifier in [('CALL_RESOLUTION',first_id),(second_kind,second_id)]:
            with vault.session() as db:proposal=ledger.preview_booking(db,kind,identifier,**scope)
            with vault.session() as db:
                ledger.book_receipt(db,kind,identifier,expected_revision=proposal['revision'],preview_digest=proposal['preview_digest'],now=clock[0],**scope)
        with vault.session() as db:
            snapshot=account_snapshot(db,owner)
            assert snapshot.cash['USDT']==Decimal('100')-Decimal(final['dca']['spent_units'])/10**18
            assert snapshot.cash['BNB']==Decimal('0.01')-Decimal(final['dca']['gas_spent_wei'])/10**18
        with vault.session() as db:
            untouched=operator.intent_record(db,parent.id)[0]
            assert (untouched.payload_json,untouched.state_json,untouched.updated_at)==original
            family=read_executions(db,account_id=account,run_id=resumed['id'])
            assert family['family_run_ids']==[started['id'],resumed['id']]
            assert family['total']==int(executed)+int(not resolve_second) and family['pending_count']==0
            assert len({item['fill_id'] for item in family['executions']})==family['total']
            assert family['booked_count']==family['total']
        if resolve_second:
            clock[0]+=timedelta(seconds=1);mine()
            funding=read_funding_state(rpc,meta['owner'],int(deployment['nonce']),500,now=clock[0])
            connection.connect(request_id=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
                wallet_address=meta['owner'],snapshot=funding,ttl_seconds=900,now=clock[0])
            with vault.session() as db:
                seed=[bar.json() for bar in bots.run_record(db,resumed['id'])[3]['bars']]
                third,third_claim=bots.start_run(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],wallet_address=meta['owner'],
                    connection=connection,snapshot=funding.executor,seed=seed,now=clock[0],permission_mode='SESSION',requested_seconds=900,
                    previous_run_id=resumed['id'])
                assert third['engine']==dca.OUTCOME_ENGINE
            idle=worker(third['id'],third_claim)
            assert idle.run_once()['last_action']=='WAIT';idle.stop()
            third_view=supervisor.view(third['id'],account_id=account)
            assert third_view['dca']==final['dca']
        vault.backup(tmp_path/'resolved-dca.db');vault.lock();vault.unlock(PASSWORD)
        recovered=supervisor.view(resumed['id'],account_id=account)
        assert recovered['dca']==final['dca'] and not recovered['execution_enabled']
        assert not recovered['worker']['pending_receipt']
