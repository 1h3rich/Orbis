"""Ingreso desde wallet registrada: traslado pareado, nunca aportación doble."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest

from app.portfolio.domain import EventInput,PortfolioError,realized_pnl_by_currency
from app.portfolio.models import RealEvent,RealWalletFundingBooking,RealMexcAccountLink
from app.portfolio.repository import account_snapshot,append_event,create_account,verify_paired_transfers,transfer_cash_between_accounts
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_funding_credit import book_credit,booking_options,booking_record
from app.portfolio.wallet_links import save_wallet_link,remove_wallet_link
from tests.test_wallet_funding import PASSWORD,WALLET
from tests.test_wallet_funding_credit import credited_setup


AMOUNT = Decimal("12.345678901234567891")


def source_wallet(vault,now,*,balance="100",currency="USDT",at=None,address=WALLET):
    with vault.session() as db:
        source = create_account(db,"Wallet propia","USDT").id
        link = save_wallet_link(db,source,address,"0x38","Principal")["id"]
        db.commit()
        append_event(db,source,str(uuid4()),EventInput("CONTRIBUTION",currency,at or now-timedelta(days=1),amount=Decimal(balance)))
    return source,link


def preview_and_payload(case,source):
    vault,account,now,plan,_,_ = case
    with vault.session() as db:
        preview = booking_options(db,plan["id"],account,WALLET)
        assert not preview["external_available"] and preview["internal"]["source_account_id"] == source
        assert db.query(RealEvent).count() == 1
    return dict(mode="NEW_INTERNAL",wallet_address=WALLET,source_account_id=source,
                source_revision=preview["internal"]["source_revision"],now=now)


def test_internal_booking_exact_atomic_conservation_replay_and_backup(tmp_path):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,link = source_wallet(vault,now)
    args = preview_and_payload(case,source)
    with vault.session() as db:
        result = book_credit(db,plan["id"],account,**args)
        assert result["booked"] and not result["ready_for_execution"]
        assert book_credit(db,plan["id"],account,**args) == result
        src,dst = account_snapshot(db,source),account_snapshot(db,account)
        assert src.cash["USDT"] == Decimal("87.654321098765432109")
        assert dst.cash["USDT"] == AMOUNT and src.cash["USDT"]+dst.cash["USDT"] == 100
        assert src.contributions["USDT"] == 100 and dst.contributions.get("USDT",0) == 0
        assert all(value == 0 for value in realized_pnl_by_currency(src).values())
        assert all(value == 0 for value in realized_pnl_by_currency(dst).values())
        assert verify_paired_transfers(db) == 1 and db.query(RealEvent).count() == 3
        row,data = booking_record(db,plan["id"])
        assert data["source_account_id"] == source
        assert WALLET.lower() not in row.payload_json.lower()
        assert booking_options(db,plan["id"],account,None)["booked"]
        # Quitar un vínculo actual no reescribe la evidencia histórica.
        remove_wallet_link(db,source,link); db.commit()
        assert book_credit(db,plan["id"],account,**args) == result
    backup = vault.backup(tmp_path / "paired-funding.db")
    copied = RealVault(backup); copied.unlock(PASSWORD)
    with copied.session() as db:
        assert booking_record(db,plan["id"])[1]["source_account_id"] == source
        assert verify_paired_transfers(db) == 1
    copied.lock(); vault.lock()


@pytest.mark.parametrize("change",["wallet","account","revision","event"])
def test_wrong_origin_or_stale_preview_cannot_change_either_book(tmp_path,change):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,_ = source_wallet(vault,now)
    args = preview_and_payload(case,source)
    if change == "wallet": args["wallet_address"] = "0x"+"7"*40
    if change == "account": args["source_account_id"] = str(uuid4())
    if change == "event": args["event_id"] = str(uuid4())
    if change == "revision":
        with vault.session() as db:
            append_event(db,source,str(uuid4()),EventInput("CONTRIBUTION","USDT",now,amount=Decimal(1)))
    with vault.session() as db:
        before = account_snapshot(db,source)
        with pytest.raises(PortfolioError): book_credit(db,plan["id"],account,**args)
        assert account_snapshot(db,source) == before and not account_snapshot(db,account).cash
        assert db.query(RealWalletFundingBooking).count() == 0
    vault.lock()


@pytest.mark.parametrize("balance,currency,offset",[("1","USDT",-1),("100","EUR",-1),("100","USDT",1)])
def test_no_conversion_or_use_of_future_or_insufficient_funds(tmp_path,balance,currency,offset):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,_ = source_wallet(vault,now,balance=balance,currency=currency,at=now+timedelta(days=offset))
    with vault.session() as db:
        options = booking_options(db,plan["id"],account,WALLET)
        assert options["internal"] is None and not options["external_available"] and options["reason"]
        assert db.query(RealEvent).count() == 1
    vault.lock()


def test_existing_compatible_pair_requires_linking_not_another_debit(tmp_path):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,_ = source_wallet(vault,now)
    args = preview_and_payload(case,source)
    with vault.session() as db:
        pair = transfer_cash_between_accounts(db,source,account,str(uuid4()),currency="USDT",amount=AMOUNT,occurred_at=now)
        options = booking_options(db,plan["id"],account,WALLET)
        assert options["internal"] is None and options["events"][0]["id"] == pair["target_event_id"]
        with pytest.raises(PortfolioError,match="asientos compatibles"): book_credit(db,plan["id"],account,**args)
        result = book_credit(db,plan["id"],account,mode="EXISTING_EVENT",wallet_address=None,event_id=pair["target_event_id"],now=now)
        assert result["booked"] and db.query(RealEvent).count() == 3
    vault.lock()


def test_source_exchange_link_and_unlinked_wallet_cannot_create_internal_transfer(tmp_path):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,link = source_wallet(vault,now)
    args = preview_and_payload(case,source)
    with vault.session() as db:
        db.add(RealMexcAccountLink(account_id=source,account_ref="mexc:fixture-other",created_at=now)); db.commit()
        assert booking_options(db,plan["id"],account,WALLET)["internal"] is None
        with pytest.raises(PortfolioError,match="separada"): book_credit(db,plan["id"],account,**args)
        remove_wallet_link(db,source,link); db.commit()
        assert booking_options(db,plan["id"],account,WALLET)["external_available"]
        with pytest.raises(PortfolioError,match="vinculada"): book_credit(db,plan["id"],account,**args)
    vault.lock()


def test_failure_after_pair_creation_rolls_back_both_events_and_booking(tmp_path,monkeypatch):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,_ = source_wallet(vault,now)
    args = preview_and_payload(case,source)
    import app.portfolio.wallet_funding_credit as module
    original = module._book_event
    def fail(db,*arguments):
        assert db.query(RealEvent).count() == 3
        raise PortfolioError("fixture after pair insertion")
    monkeypatch.setattr(module,"_book_event",fail)
    with vault.session() as db:
        with pytest.raises(PortfolioError,match="pair insertion"): book_credit(db,plan["id"],account,**args)
        assert db.query(RealEvent).count() == 1 and db.query(RealWalletFundingBooking).count() == 0
        assert account_snapshot(db,source).cash["USDT"] == 100 and not account_snapshot(db,account).cash
    monkeypatch.setattr(module,"_book_event",original)
    vault.lock()


def test_independent_connections_cannot_duplicate_internal_transfer(tmp_path):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,_ = source_wallet(vault,now)
    args = preview_and_payload(case,source)
    other = RealVault(vault.path); other.unlock(PASSWORD)
    barrier = Barrier(2)
    def perform(local):
        barrier.wait()
        with local.session() as db: return book_credit(db,plan["id"],account,**args)
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(perform,[vault,other]))
    assert results[0] == results[1]
    with vault.session() as db:
        assert db.query(RealEvent).count() == 3 and db.query(RealWalletFundingBooking).count() == 1
    other.lock(); vault.lock()


@pytest.mark.parametrize("change",["event","booking"])
def test_backup_rejects_changed_source_side_of_internal_pair(tmp_path,change):
    case = credited_setup(tmp_path)
    vault,account,now,plan,_,_ = case
    source,_ = source_wallet(vault,now)
    args = preview_and_payload(case,source)
    with vault.session() as db:
        book_credit(db,plan["id"],account,**args)
        data = booking_record(db,plan["id"])[1]
        if change == "event":
            db.get(RealEvent,data["source_event_id"]).gross_amount = Decimal("1")
        else:
            from app.portfolio.wallet_funding import _encoded
            data["source_event_digest"] = "0"*64
            row = db.get(RealWalletFundingBooking,plan["id"])
            row.payload_json,row.payload_digest = _encoded(data)
        db.commit()
    with pytest.raises((PortfolioError,VaultError)): vault.backup(tmp_path / "tampered-pair.db")
    vault.lock()
