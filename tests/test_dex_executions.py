"""DEX markers derive only from verified own receipts, never reference quotes."""
from contextlib import nullcontext
from datetime import datetime,timedelta
from decimal import Decimal,localcontext
import hashlib
import json
from uuid import uuid4

import pytest

from app.lab.store import content_hash
from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_executions import read_executions,executions_page
from app.live_execution.dex_research_export import export_dex_bot_swaps
from app.portfolio import dex_operator_journal as operator
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_operator import write


def test_projection_keeps_real_prices_timestamps_partial_sales_and_stable_revision_across_waits(tmp_path,monkeypatch):
    with running_case(tmp_path,policy_changes={'max_exit_notional':str(7*10**15)}) as case:
        values,started,claim,connection,clock,mine,market=case
        vault,account,manager,draft,meta,rpc,_,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        def view():
            with vault.session() as db:return read_executions(db,account_id=account,run_id=started['id'])
        try:
            assert view()['total']==0
            clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
            pending=view();assert pending['total']==0 and pending['pending_count']==1
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            bought=view();fill=bought['executions'][0]
            assert bought['environment']=='REAL' and bought['provider']=='uniswap-v3' and bought['reference_provider']=='mexc'
            assert bought['base_asset']=='WBNB' and bought['quote_asset']=='USDT' and not bought['financially_booked']
            assert fill['side']=='BUY' and fill['quantity_base']=='0.010000000000000001' and fill['gross_quote']=='0.010000000000000001' and fill['price']=='1'
            assert fill['fee_asset']=='BNB' and Decimal(fill['fee'])>0 and fill['run_id']==started['id']
            assert fill['occurred_at']==datetime.fromtimestamp(fill['time_ms']//1000,clock[0].tzinfo).isoformat()
            assert fill['time_ms']//1000>fill['signal_candle_t'] and fill['event_id'] is None and not fill['financially_booked']
            assert fill['reference_price']!='1'  # MEXC ask is not the receipt's execution price.
            original_revision=bought['revision'];assert worker.run_once()['last_action']=='WAIT'
            assert view()['revision']==original_revision
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[9950,False])
            market.bid,market.ask='1.2','1.21';clock[0]+=timedelta(seconds=1);mine()
            assert worker.run_once()['last_action']=='SELL' and worker.run_once()['last_receipt_status']=='FINALIZED'
            sold=view();sale=sold['executions'][1]
            assert sold['total']==2 and sale['side']=='SELL' and sale['partial'] and Decimal(sale['position_after_base'])>0
            with localcontext() as context:
                context.prec=80
                assert Decimal(sale['price'])==Decimal(sale['gross_quote'])/Decimal(sale['quantity_base'])
            old=sold['revision'];assert worker.run_once()['last_action']=='SELL'
            with vault.session() as db:
                with pytest.raises(DexExecutorError,match='actualiza el gráfico'):
                    export_dex_bot_swaps(db,account_id=account,run_id=started['id'],revision=old)
                revision=read_executions(db,account_id=account,run_id=started['id'])['revision']
                exported=export_dex_bot_swaps(db,account_id=account,run_id=started['id'],revision=revision)
                for attr,limit,error in (('MAX_RESEARCH_ROWS',1,'máximo de swaps'),
                                         ('MAX_RESEARCH_BYTES',32,'tamaño máximo')):
                    with monkeypatch.context() as patch, pytest.raises(DexExecutorError,match=error):
                        patch.setattr('app.live_execution.dex_research_export.'+attr,limit)
                        export_dex_bot_swaps(db,account_id=account,run_id=started['id'],revision=revision)
                rows=[json.loads(line) for line in exported['jsonl'].splitlines()]
            assert exported['record_count']==2 and rows[1]['swap']['partial']
            assert rows[0]['provenance']['reserve'] is None  # Legacy archive without a reserve.
            assert rows[0]['provenance']['lab_backtest_id'] and rows[0]['provenance']['paper_session_id']
            assert rows[1]['swap']['realized_pnl_quote_before_gas']==sale['realized_pnl_quote']
            # A pending next partial execution changes the economic snapshot.
            assert view()['revision']!=old and view()['total']==2
            assert worker.run_once()['last_receipt_status']=='FINALIZED'
            worker.stop();current=view()
            for limit,offset,revision in ((True,0,None),(1,1,None),(501,0,None),(1,100,current['revision']),(1,0,'ab'*32)):
                with pytest.raises(DexExecutorError):executions_page(current,limit=limit,offset=offset,revision=revision)
            first=executions_page(current,limit=1,offset=0)
            second=executions_page(current,limit=1,offset=1,revision=first['revision'])
            assert first['executions'][0]['fill_id']!=second['executions'][0]['fill_id']
            with pytest.raises(DexExecutorError):
                with vault.session() as db:read_executions(db,account_id=str(uuid4()),run_id=started['id'])
            vault.backup(tmp_path/'execution-projection.db')
        finally:worker.stop()


def test_reverted_execution_is_only_a_gas_incident_not_a_fill_marker(tmp_path,monkeypatch):
    from app.live_execution.dex_operator_transport import BnbOperatorSender
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,_,_,meta,rpc,_,_=values;monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def sender(plan):
            actual=BnbOperatorSender(plan,clock=lambda:clock[0]);original=actual.send
            def send(raw):
                write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,True]);return original(raw)
            actual.send=send;return actual
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=sender,clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
            assert worker.run_once()['last_receipt_status']=='REVERTED'
            with vault.session() as db:value=read_executions(db,account_id=account,run_id=started['id'])
            assert value['total']==0 and value['executions']==[] and value['reverted_count']==1 and int(value['reverted_gas_wei'])>0
            with vault.session() as db, pytest.raises(DexExecutorError,match='no tiene swaps confirmados'):
                export_dex_bot_swaps(db,account_id=account,run_id=started['id'],revision=value['revision'])
            from app.portfolio import dex_receipt_booking as accounting
            from app.portfolio.wallet_links import save_wallet_link
            from app.portfolio.repository import append_event,list_events,account_snapshot
            from app.portfolio.domain import EventInput
            with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
            with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',clock[0]-timedelta(days=1),amount=Decimal('0.01')))
            with vault.session() as db:receipt=next(item for item in accounting.list_receipts(db,account_id=account,wallet_address=meta['owner']) if item['source_kind']=='OPERATOR')
            with vault.session() as db:preview=accounting.preview_booking(db,'OPERATOR',receipt['source_id'],context_account_id=account,wallet_address=meta['owner'])
            assert preview['trade'] is None
            with vault.session() as db:
                accounting.book_receipt(db,'OPERATOR',receipt['source_id'],context_account_id=account,wallet_address=meta['owner'],
                    expected_revision=preview['revision'],preview_digest=preview['preview_digest'],now=clock[0])
                assert account_snapshot(db,account).positions=={} and len(list_events(db,account))==2

        finally:worker.stop()


def test_http_projection_never_shows_an_uncertain_send_and_recovers_only_original_receipt(tmp_path,monkeypatch):
    from eth_account import Account
    from tests.test_dex_bot_run_api import http_case,eventually
    from tests.test_wallet_funding_api import prove
    from tests.test_dex_deployments import PASSWORD
    with http_case(tmp_path,monkeypatch,lost_response=True) as (client,headers,base,body,values,clock,mine,hashes,controls):
        identifier=body['request_id'];url=base+'/'+identifier+'/executions-page'
        export_url=base+'/'+identifier+'/research-export'
        body_for=lambda revision:{'confirmation':'EXPORTAR_JSONL_UNISWAP_REAL','revision':revision}
        assert client.get(url).status_code==401
        assert client.post(export_url,json=body_for('a'*64)).status_code==401
        assert client.post(export_url,headers={**headers,'Origin':'https://outside.invalid'},json=body_for('a'*64)).status_code==403
        assert client.post(base,headers=headers,json=body).status_code==200
        clock[0]+=timedelta(seconds=1);mine()
        eventually(lambda:client.get(base+'/'+identifier,headers=headers).json(),lambda row:row.get('reason')=='OUTCOME_UNKNOWN')
        pending=client.get(url,headers=headers);assert pending.status_code==200,pending.text
        assert pending.headers['cache-control']=='no-store'
        value=pending.json();assert value['total']==0 and value['pending_count']==1 and len(hashes)==1
        empty=client.post(export_url,headers=headers,json=body_for(value['revision']))
        assert empty.status_code==409 and empty.headers['cache-control']=='no-store'
        assert 'swaps confirmados' in empty.json()['detail']
        assert client.post(export_url,headers=headers,json={'revision':value['revision']}).status_code==422
        assert client.get(url,headers=headers,params={'offset':1}).status_code==409
        assert client.get(url,headers=headers,params={'limit':501}).status_code==422
        assert client.get(url,headers=headers,params={'revision':'corrupt'}).status_code==422
        assert client.get(url.replace(values[1],str(uuid4())),headers=headers).status_code==409
        intent=client.get(base+'/'+identifier,headers=headers).json()['intents'][0]['id']
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        assert client.get(url,headers=headers).status_code==401
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        controls['receipts']=True;prove(client,headers,values[1],Account.from_key('0x'+'11'*32))
        observed=client.post(base+'/'+identifier+'/intents/'+intent+'/observe',headers=headers,
            json={'address':values[4]['owner'],'tx_hash':hashes[0]})
        assert observed.status_code==200,observed.text
        recovered=client.get(url,headers=headers).json()
        assert recovered['total']==1 and recovered['pending_count']==0 and recovered['revision']!=value['revision'] and len(hashes)==1
        assert client.get(url,headers=headers,params={'revision':value['revision']}).status_code==409
        # Read-only projection exposes no custody address, tx hash or key.
        assert values[4]['owner'].lower() not in str(recovered).lower() and hashes[0].lower() not in str(recovered).lower()
        assert client.post(export_url,headers=headers,json=body_for(value['revision'])).status_code==409
        assert client.post(export_url.replace(values[1],str(uuid4())),headers=headers,
                           json=body_for(recovered['revision'])).status_code==409
        reply=client.post(export_url,headers=headers,json=body_for(recovered['revision']))
        assert reply.status_code==200,reply.text
        assert reply.headers['cache-control']=='no-store'
        export=reply.json();row=json.loads(export['jsonl'])
        assert export['schema']=='orbis.uniswap.real.bot-swaps.v1' and export['record_count']==1
        assert hashlib.sha256(export['jsonl'].encode()).hexdigest()==export['sha256']
        assert row.pop('row_sha256')==content_hash(row)
        assert row['execution_authority']=='NONE' and row['market']['base_asset']=='WBNB'
        assert row['provenance']['strategy_kind']=='STRATEGY'
        assert row['provenance']['reserve']['artifact_id'] and row['provenance']['reserve']['historical_model']=='SIGNAL_MARKET_WITHOUT_DEX_EXECUTION_COSTS'
        assert row['signal']['reason']=='ENTRY_RULE' and row['signal']['reference_provider']=='mexc'
        assert row['signal']['signal_candle_t']==recovered['executions'][0]['signal_candle_t']
        assert row['swap']['gas_fee_wei']==recovered['executions'][0]['gas_fee_wei']
        assert row['swap']['quantity_base']=='0.010000000000000001' and row['swap']['financially_booked'] is False
        for private in (values[1],values[4]['owner'],hashes[0],intent,identifier,body['binding_id'],
                        recovered['executions'][0]['block_hash']):
            assert private.lower() not in export['jsonl'].lower()
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        assert client.post(export_url,headers=headers,json=body_for(recovered['revision'])).status_code==401
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        assert client.post(export_url,headers=headers,json=body_for(recovered['revision'])).json()==export
        assert len(hashes)==1


def test_display_price_is_reproducible_even_with_a_different_callers_decimal_context():
    from decimal import Inexact,ROUND_DOWN,ROUND_UP
    from app.live_execution.dex_executions import _price
    for rounding in (ROUND_DOWN,ROUND_UP):
        with localcontext() as context:
            context.prec=6;context.rounding=rounding;context.Emax=6;context.traps[Inexact]=True
            assert _price('2','3')=='0.'+'6'*79+'7'
            assert _price('1','8')=='0.125'
            assert _price('100000000000000000000','0.000000000000000001')=='100000000000000000000000000000000000000'
