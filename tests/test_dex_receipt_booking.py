"""Own EVM receipts become exact wallet ledger events, never invented funds."""
from contextlib import contextmanager,nullcontext
from datetime import timedelta
from decimal import Decimal,localcontext
from uuid import uuid4
import pytest
from sqlalchemy import select
from app.live_execution.dex_bot_worker import DexBotWorker
from app.portfolio.domain import EventInput,PortfolioError
from app.portfolio.repository import create_account,append_event,account_snapshot,list_events
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio import dex_receipt_booking as booking
from app.portfolio.models import RealDexReceiptBooking,RealDexReceiptBookingEvent
from app.portfolio.wallet_funding import _encoded,_decoded
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_operator import write
from tests.test_dex_deployments import PASSWORD


@contextmanager
def case(directory,monkeypatch,*,native=True,evm_backend=None):
    with running_case(directory,policy_changes={'max_exit_notional':str(7*10**15)},funding_bnb='0.002',evm_backend=evm_backend) as values:
        data,started,claim,connection,clock,mine,market=values
        vault,account,_,_,meta,rpc,_,_=data;monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        with vault.session() as db:owner=create_account(db,'Wallet propia','USDT').id;db.commit()
        with vault.session() as db:save_wallet_link(db,owner,meta['owner'],'0x38','Principal');db.commit()
        entries=[EventInput('CONTRIBUTION','USDT',clock[0]-timedelta(days=1),amount=Decimal('100'))]
        if native:entries.append(EventInput('CONTRIBUTION','BNB',clock[0]-timedelta(days=1),amount=Decimal('0.01')))
        for item in entries:
            with vault.session() as db:append_event(db,owner,str(uuid4()),item)
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0])
        try:yield values,owner,worker
        finally:worker.stop()


def preview(vault,account,meta,identifier,*,kind='OPERATOR',**selection):
    with vault.session() as db:return booking.preview_booking(db,kind,identifier,context_account_id=account,wallet_address=meta['owner'],**selection)


def book(vault,account,meta,identifier,now,proposed,*,kind='OPERATOR',**selection):
    with vault.session() as db:return booking.book_receipt(db,kind,identifier,context_account_id=account,wallet_address=meta['owner'],
        expected_revision=proposed['revision'],preview_digest=proposed['preview_digest'],now=now,**selection)


def test_actual_buy_partial_sales_gas_and_wallet_target_are_booked_once_across_reopen(tmp_path,monkeypatch):
    with case(tmp_path,monkeypatch) as (values,owner,worker):
        data,started,claim,connection,clock,mine,market=values;vault,account,_,_,meta,rpc,_,_=data
        # Existing WBNB is independent of the bot's position and cost basis.
        with vault.session() as db:append_event(db,owner,str(uuid4()),EventInput('BUY','USDT',clock[0]-timedelta(hours=1),asset='WBNB',quantity=Decimal('0.02'),unit_price=Decimal('2')))
        clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
        with vault.session() as db:pending=booking.list_receipts(db,account_id=account,wallet_address=meta['owner'])
        assert not any(row['source_kind']=='OPERATOR' for row in pending)
        assert worker.run_once()['last_receipt_status']=='FINALIZED'
        def receipts():
            with vault.session() as db:return [row for row in booking.list_receipts(db,account_id=account,wallet_address=meta['owner']) if row['source_kind']=='OPERATOR']
        buy=receipts()[0];proposal=preview(vault,account,meta,buy['source_id'])
        assert proposal['target_account_id']==owner and proposal['trade']['quantity']=='0.010000000000000001'
        assert proposal['trade']['gross_amount']=='0.010000000000000001' and proposal['gas_amount']==buy['gas_amount']
        first=book(vault,account,meta,buy['source_id'],clock[0],proposal);assert first['events_created']==2
        assert book(vault,account,meta,buy['source_id'],clock[0],proposal)['events_created']==0
        assert preview(vault,account,meta,buy['source_id'])['booked']
        write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[9950,False]);market.bid,market.ask='1.2','1.21'
        clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='SELL';assert worker.run_once()['last_receipt_status']=='FINALIZED'
        sale=receipts()[-1];partial=preview(vault,account,meta,sale['source_id']);assert partial['trade']['kind']=='SELL'
        assert book(vault,account,meta,sale['source_id'],clock[0],partial)['booked']
        assert worker.run_once()['last_action']=='SELL';assert worker.run_once()['last_receipt_status']=='FINALIZED'
        assert len(receipts())==3
        final=receipts()[-1];proposed=preview(vault,account,meta,final['source_id']);book(vault,account,meta,final['source_id'],clock[0],proposed)
        worker.stop()
        with vault.session() as db:
            snapshot=account_snapshot(db,owner)
            assert snapshot.positions[('WBNB','USDT')].quantity==Decimal('0.02')
            assert snapshot.positions[('WBNB','USDT')].cost_basis<Decimal('0.04')  # Weighted portfolio basis differs from the bot.
            assert snapshot.cash['BNB']==Decimal('0.01')-sum(Decimal(row['gas_amount']) for row in receipts())
            assert list_events(db,account)==[] and len(list_events(db,owner))==9
            original=[(row.id,row.payload_json,row.payload_digest) for row in db.scalars(select(RealDexReceiptBooking))]
        vault.backup(tmp_path/'booked.db');vault.lock();vault.unlock(PASSWORD)
        assert book(vault,account,meta,buy['source_id'],clock[0],proposal)['events_created']==0
        with vault.session() as db:assert original==[(row.id,row.payload_json,row.payload_digest) for row in db.scalars(select(RealDexReceiptBooking))]


def test_missing_gas_stale_revision_and_existing_events_do_not_create_or_duplicate_capital(tmp_path,monkeypatch):
    with case(tmp_path,monkeypatch,native=False) as (values,owner,worker):
        data,started,_,_,clock,mine,_=values;vault,account,_,_,meta,_,_,_=data
        clock[0]+=timedelta(seconds=1);mine();worker.run_once();worker.run_once();worker.stop()
        with vault.session() as db:receipt=next(row for row in booking.list_receipts(db,account_id=account,wallet_address=meta['owner']) if row['source_kind']=='OPERATOR')
        with pytest.raises(PortfolioError,match='BNB'):preview(vault,account,meta,receipt['source_id'])
        with vault.session() as db:assert len(list_events(db,owner))==1
        with vault.session() as db:append_event(db,owner,str(uuid4()),EventInput('CONTRIBUTION','BNB',clock[0]-timedelta(hours=1),amount=Decimal('0.01')))
        proposed=preview(vault,account,meta,receipt['source_id'])
        with vault.session() as db:append_event(db,owner,str(uuid4()),EventInput('CONTRIBUTION','USDT',clock[0]-timedelta(hours=1),amount=Decimal('1')))
        with pytest.raises(PortfolioError,match='cambi'):book(vault,account,meta,receipt['source_id'],clock[0],proposed)
        proposed=preview(vault,account,meta,receipt['source_id'])
        trade=proposed['trade'];at=clock[0].fromisoformat(trade['occurred_at'])
        with vault.session() as db:
            existing=append_event(db,owner,str(uuid4()),EventInput('BUY','USDT',at,asset='WBNB',quantity=Decimal(trade['quantity']),unit_price=Decimal(trade['unit_price'])))
        selectable=preview(vault,account,meta,receipt['source_id']);assert not selectable['available']
        selected=preview(vault,account,meta,receipt['source_id'],trade_event_id=existing.id)
        assert book(vault,account,meta,receipt['source_id'],clock[0],selected,trade_event_id=existing.id)['events_created']==1
        with vault.session() as db:
            row=db.scalar(select(RealDexReceiptBooking));booking_id=row.id;payload=_decoded(row.payload_json,row.payload_digest)
            payload['account_id']=account;row.account_id=account;row.payload_json,row.payload_digest=_encoded(payload);db.commit()
        with pytest.raises(PortfolioError):
            with vault.session() as db:booking.booking_record(db,booking_id)
        from app.portfolio.vault import VaultError
        with pytest.raises(VaultError):vault.backup(tmp_path/'forged.db')


def test_control_and_funding_fees_consume_only_native_gas_and_never_create_principal_events(tmp_path,monkeypatch):
    with case(tmp_path,monkeypatch) as (values,owner,worker):
        data,_,_,_,clock,_,_=values;vault,account,_,_,meta,_,_,_=data;worker.stop()
        with vault.session() as db:rows=booking.list_receipts(db,account_id=account,wallet_address=meta['owner'])
        assert {'CONTROL','GAS_FUNDING'}<=set(row['source_kind'] for row in rows)
        from datetime import datetime
        with vault.session() as db:existing=append_event(db,owner,str(uuid4()),EventInput('FEE','BNB',datetime.fromisoformat(rows[0]['occurred_at']),amount=Decimal('0.000000000000000001'),note='Comisión manual para enlazar'))
        for index,row in enumerate(rows):
            selected={'gas_event_ids':[existing.id]} if index==0 else {}
            initial=preview(vault,account,meta,row['source_id'],kind=row['source_kind'])
            if index==0:assert not initial['available']
            else:assert existing.id not in [item['id'] for item in initial['gas_candidates']]
            planned=preview(vault,account,meta,row['source_id'],kind=row['source_kind'],**selected)
            assert planned['trade'] is None and not planned['principal_creates_event']
            assert book(vault,account,meta,row['source_id'],clock[0],planned,kind=row['source_kind'],**selected)['events_created']==1
        with vault.session() as db:
            snapshot=account_snapshot(db,owner)
            assert snapshot.cash['USDT']==Decimal('100')
            assert snapshot.cash['BNB']==Decimal('0.01')-sum(Decimal(row['gas_amount']) for row in rows)
            assert snapshot.contributions=={'USDT':Decimal('100'),'BNB':Decimal('0.01')}
            assert all(event.input.kind=='FEE' for event in list_events(db,owner)[2:])
        vault.backup(tmp_path/'fee-only.db')


def test_schema_65_migration_preserves_old_book_and_rejects_hidden_new_bookings(tmp_path):
    from tests.test_dex_deployments import setup
    from app.portfolio.vault import SCHEMA_VERSION,VaultError
    vault,account,now=setup(tmp_path)
    with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','USDT',now,amount=Decimal('12.345678901234567891')))
    with vault.session() as db:
        before=[(row.id,row.payload_digest) for row in db.scalars(select(booking.m.RealEvent))]
        db.connection().exec_driver_sql('DROP TABLE real_dex_receipt_booking_events')
        db.connection().exec_driver_sql('DROP TABLE real_dex_receipt_bookings')
        db.connection().exec_driver_sql('PRAGMA user_version=65');db.commit()
    vault.lock();vault.unlock(PASSWORD);assert vault.last_migration_backup.is_file()
    assert b'SQLite format' not in vault.last_migration_backup.read_bytes()[:16]
    with vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
        assert before==[(row.id,row.payload_digest) for row in db.scalars(select(booking.m.RealEvent))]
        assert list(db.scalars(select(RealDexReceiptBooking)))==[]
        db.add(RealDexReceiptBooking(id=str(uuid4()),source_kind='CONTROL',source_id=str(uuid4()),account_id=account,payload_json='{}',payload_digest='0'*64))
        db.connection().exec_driver_sql('PRAGMA user_version=65');db.commit()
    with pytest.raises(VaultError,match='booking'):vault.backup(tmp_path/'hidden-booking.db')


@pytest.mark.parametrize('typed',[False,True])
def test_exact_create_receipt_books_only_its_verified_native_fee(tmp_path,typed):
    from datetime import datetime,timezone
    from tests.test_dex_deployments import setup
    from tests.dex_evm_fixture import evm
    from tests.test_dex_deployment_receipt import deployment_on_evm
    from app.portfolio.dex_deployments import check_deployment
    vault,account,now=setup(tmp_path)
    with evm(tmp_path/'evm') as (meta,rpc):
        plan,evidence=deployment_on_evm(vault,account,now,meta,rpc,typed=typed)
        now=datetime.now(timezone.utc)
        with vault.session() as db:check_deployment(db,plan['id'],account_id=account,evidence=evidence,now=now)
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',now-timedelta(days=1),amount=Decimal('1')))
        proposal=preview(vault,account,meta,plan['id'],kind='DEPLOYMENT');assert proposal['trade'] is None
        assert book(vault,account,meta,plan['id'],now,proposal,kind='DEPLOYMENT')['events_created']==1
        with vault.session() as db:
            snapshot=account_snapshot(db,account)
            assert snapshot.cash['BNB']==Decimal('1')-Decimal(proposal['gas_amount']) and snapshot.contributions=={'BNB':Decimal('1')}
        vault.backup(tmp_path/'create-booked.db')
