"""No method mutation across owned active bot/pending-order journals."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select
from app.portfolio import economic_operational_guard as guard,models as m,wallet_swap as swaps,dex_receipt_order as orders
from app.portfolio.repository import create_account,transfer_cash_between_accounts
from app.live_execution.journal import register_spot_policy,reserve_spot_intent,begin_spot_submission
from app.live_execution.dex_receipt_order import collect_order
from app.portfolio.dex_receipt_order_source import order_source
from app.portfolio.economic_receipt_source import ReceiptSelection
from app.portfolio.economic_receipt_acceptance import preview_receipt_acceptance,confirm_receipt_acceptance
from tests.test_dex_call_booking import resolved_case
from tests.test_dex_call_resolution import prepare
from tests.test_economic_receipt_acceptance import scope,preview,commit
from tests.test_economic_receipt_source import capture,counts,stamp
from tests.test_live_spot_journal import case as order_case
from tests.test_dex_receipt_booking import case as operator_case


def test_unknown_wallet_request_blocks_atomic_acceptance_without_stopping_or_sending(tmp_path):
    with resolved_case(tmp_path)as case:
        selections,options=scope(case);proposal=preview(case,selections,options)
        vault,context,_,meta,rpc,*_=case
        pending,_,_=prepare(vault,context,meta,rpc)
        before=counts(vault)
        with pytest.raises(guard.AccountingOperationActive)as error:commit(case,selections,options,proposal)
        assert any(item['kind']=='SWAP'and item['id']==pending['id']for item in error.value.report['blockers'])
        assert not error.value.report['stops_workers'] and not error.value.report['moves_funds']
        assert counts(vault)==before
        with vault.session()as db:assert swaps.swap_record(db,pending['id'])[0].status=='WALLET_REQUESTED'
        with vault.session()as db:swaps.report_swap(db,pending['id'],account_id=context,rejected=True,now=datetime.now(timezone.utc))
        assert commit(case,selections,options,proposal)['created']


@pytest.mark.parametrize('linked',[False,True])
@pytest.mark.parametrize('unknown',[False,True])
def test_mexc_reservation_and_unknown_submission_only_block_the_linked_account_closure(tmp_path,linked,unknown):
    with resolved_case(tmp_path)as case:
        vault,_,owner,*_=case
        policy,intent,state=order_case();at=datetime.now(timezone.utc)
        policy=replace(policy,expires_at=at+timedelta(hours=1));intent=replace(intent,signal_at=at-timedelta(seconds=1))
        state=replace(state,observed_at=at,quote_at=at)
        with vault.session()as db:
            peer=create_account(db,'MEXC propio','USDT').id
            db.add(m.RealMexcAccountLink(account_ref=policy.account_ref,account_id=peer,created_at=at));db.commit()
        if linked:
            with vault.session()as db:transfer_cash_between_accounts(db,owner,peer,str(uuid4()),currency='USDT',amount=D('1'),occurred_at=at)
        register_spot_policy(vault,policy,now=at);assert reserve_spot_intent(vault,intent,state,now=at).status=='RESERVED'
        if unknown:assert begin_spot_submission(vault,intent,state,now=at).status=='SUBMISSION_UNKNOWN'
        selections,options=scope(case);proposal=preview(case,selections,options);before=counts(vault)
        if linked:
            with pytest.raises(guard.AccountingOperationActive)as error:commit(case,selections,options,proposal)
            assert {'code':'ORDER_PENDING','kind':'MEXC','id':intent.intent_id}in error.value.report['blockers']
            assert counts(vault)==before
        else:assert commit(case,selections,options,proposal)['created']
        with vault.session()as db:
            row=db.get(m.RealLiveSpotIntent,intent.intent_id)
            assert row.status==('SUBMISSION_UNKNOWN'if unknown else 'RESERVED')


def test_real_active_dex_run_keeps_its_position_and_native_authority_until_explicit_local_stop(tmp_path,monkeypatch):
    with operator_case(tmp_path,monkeypatch,evm_backend='anvil')as (values,owner,worker):
        data,_,_,_,clock,mine,_=values;vault,context,_,_,meta,rpc,_,_=data
        clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
        assert worker.run_once()['last_receipt_status']=='FINALIZED'
        with vault.session()as db:
            original_row=db.scalar(select(m.RealDexOperatorIntent).where(m.RealDexOperatorIntent.status=='FINALIZED'))
            source=order_source(db,'OPERATOR',original_row.id,wallet_address=meta['owner'])
        import json
        block=rpc('eth_getBlockByNumber',[hex(int(json.loads(source.receipt_json)['block_number'])),False])
        args=dict(context_account_id=context,wallet_address=meta['owner'],evidence=collect_order(rpc,block['transactions'][0]),
            now=max(datetime.now(timezone.utc),source.updated_at+timedelta(seconds=1)))
        with vault.session()as db:proof=orders.preview_order(db,'OPERATOR',source.id,**args)
        with vault.session()as db:orders.confirm_order(db,'OPERATOR',source.id,preview_digest=proof['preview_digest'],**args)
        selections=[ReceiptSelection('OPERATOR',source.id,context,meta['owner'])];inputs=capture(vault,owner,selections)
        options=dict(batch_id=str(uuid4()),recorded_at=stamp(inputs))
        proposal=preview_receipt_acceptance(vault,owner,selections,**options);before=counts(vault)
        balance=rpc('eth_getBalance',[meta['owner'],'latest']);status=worker.status()
        with pytest.raises(guard.AccountingOperationActive)as error:
            confirm_receipt_acceptance(vault,owner,selections,**options,preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=1))
        assert any(item['code']=='BOT_ACTIVE'and item['kind']=='DEX'for item in error.value.report['blockers'])
        assert worker.status()==status and not worker.status()['stop_recorded'] and counts(vault)==before
        assert rpc('eth_getBalance',[meta['owner'],'latest'])==balance
        worker.stop()
        saved=confirm_receipt_acceptance(vault,owner,selections,**options,preview_digest=proposal['preview_digest'],now=options['recorded_at']+timedelta(seconds=1))
        assert saved['created'] and rpc('eth_getBalance',[meta['owner'],'latest'])==balance


def test_mexc_bot_report_does_not_stop_its_cycles_or_disable_exit_policy(tmp_path,monkeypatch):
    from tests.test_mexc_spot_bot import bot_case,tick
    from app.live_execution.spot_bot_store import pause_spot_bot,bot_record
    env=bot_case(tmp_path);vault,policy,account,_,clock,_,_,trader,bot_id,_=env
    # Unit-test the activity reader against an authentic bot journal. Source
    # scope authentication is exercised with actual signed receipts above.
    monkeypatch.setattr(guard,'_scope',lambda *args:guard._Scope(frozenset([account]),frozenset([account]),frozenset(),frozenset(),frozenset(),frozenset()))
    try:
        assert tick(env)['action']=='WAIT'
        with vault.session()as db:
            before=bot_record(db,bot_id);changes=db.connection().connection.driver_connection.total_changes
            report=guard.operational_report(db,[account],[{}],now=clock())
            assert db.connection().connection.driver_connection.total_changes==changes
            assert bot_record(db,bot_id)==before
        assert not report['can_accept'] and {'code':'BOT_ACTIVE','kind':'MEXC','id':bot_id}in report['blockers']
        assert policy.allow_risk_reducing_exits and not trader.posts
        assert tick(env)['action']=='WAIT'and not trader.posts
        pause_spot_bot(vault,bot_id,now=clock(),reason='MANUAL')
        with vault.session()as db:assert guard.operational_report(db,[account],[{}],now=clock())['can_accept']
    finally:vault.lock()
