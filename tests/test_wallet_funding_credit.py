"""Cadena y MEXC ficticios, registro contable exacto y protección frente a duplicados."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.mexc_uid_guard import claim_mexc_uid_in_transaction
from app.portfolio.domain import EventInput, PortfolioError
from app.portfolio.mexc_transfers import save_transfer_snapshot, read_transfer_snapshot
from app.portfolio.models import RealWalletFundingCredit, RealWalletFundingBooking
from app.portfolio.repository import append_event, account_snapshot, create_account, transfer_cash_between_accounts
from app.portfolio.vault import RealVault, VaultError
from app.portfolio.wallet_funding import _encoded, funding_record, list_funding, report_funding
from app.portfolio.wallet_funding_credit import book_credit, confirm_credit, settlement_status
from app.portfolio.wallet_funding_receipt import TRANSFER, receipt_observation
from app.portfolio.wallet_links import save_wallet_link
from tests.test_wallet_funding import (
    ACCOUNT_REF, CONTRACT, DESTINATION, PASSWORD, TX_HASH, WALLET, setup, prepare, claim,
)


def evidence(plan, now, *, wallet=WALLET, destination=DESTINATION, tx_hash=TX_HASH):
    block_hash = "0x"+"c"*64
    tx = {**plan["transaction"],"hash":tx_hash,"input":plan["transaction"]["data"],
          "type":"0x0","blockNumber":"0x1234","blockHash":block_hash,"transactionIndex":"0x0"}
    tx["from"],tx["to"] = wallet,plan["transaction"]["to"]
    tx.pop("data")
    log = {"address":tx["to"],"removed":False,"transactionHash":tx_hash,"blockHash":block_hash,
        "blockNumber":"0x1234","transactionIndex":"0x0","logIndex":"0x2",
        "topics":[TRANSFER,"0x"+wallet[2:].lower().zfill(64),"0x"+destination[2:].lower().zfill(64)],
        "data":"0x"+hex(int(plan["amount_units"]))[2:].zfill(64)}
    receipt = {"transactionHash":tx_hash,"blockHash":block_hash,"blockNumber":"0x1234",
        "transactionIndex":"0x0","from":wallet,"to":tx["to"],"type":"0x0","status":"0x1",
        "gasUsed":"0xea60","effectiveGasPrice":tx["gasPrice"],"logs":[log]}
    block = {"number":"0x1234","hash":block_hash,"timestamp":hex(int(now.timestamp()))}
    return {"chain_id":"0x38","tx_hash":tx_hash,"transaction":tx,"receipt":receipt,
            "block":block,"finalized":dict(block)}


def snapshot(now, *, amount="12.345678901234567891", status=5, tx_hash=TX_HASH,
             destination=DESTINATION, contract=CONTRACT, network="BSC"):
    return {"source":"MEXC_SPOT_PRIVATE_READONLY","accounting":"OBSERVED_NOT_IMPORTED",
        "coverage":"RECENT_WINDOW_BELOW_PAGE_LIMIT","start_ms":int((now-timedelta(days=1)).timestamp()*1000),
        "end_ms":int(now.timestamp()*1000),"observed_at":now.isoformat(),"withdrawals":[],
        "deposits":[{"direction":"deposit","asset":"USDT","network":network,"amount":amount,
            "fee":"0","status":status,"time_ms":int(now.timestamp()*1000),"source_identity":tx_hash,
            "destination_identity":destination,"token_contract":contract}]}


def credited_setup(tmp_path, *, credited=True, status=5):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    assert claim(vault,account,now,plan)["claim_granted"]
    with vault.session() as db:
        report_funding(db,plan["id"],account_id=account,tx_hash=TX_HASH,now=now)
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        claim_mexc_uid_in_transaction(db,ACCOUNT_REF,"123456789",now=now)
        db.commit()
        sid = save_transfer_snapshot(db,ACCOUNT_REF,snapshot(now,status=status),view_uid="123456789")["id"]
        raw = evidence(plan,now)
        if credited:
            result = confirm_credit(db,plan["id"],account,raw,sid,now=now)
            assert result["exchange_credited"] and not result["booked"]
    return vault,account,now,plan,raw,sid


def test_receipt_and_mexc_credit_are_not_income_until_explicit_book_and_replay(tmp_path):
    vault,account,now,plan,raw,sid = credited_setup(tmp_path)
    with vault.session() as db:
        assert not account_snapshot(db,account).cash
        replay = confirm_credit(db,plan["id"],account,raw,sid,now=now)
        assert replay["status"] == "MEXC_CREDITED" and replay["chain_source"] == "WALLET_RPC_OBSERVED"
        result = book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",wallet_address=WALLET,now=now)
        assert result["booked"] and not result["ready_for_execution"]
        again = book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",wallet_address=WALLET,now=now)
        assert result == again
        book = account_snapshot(db,account)
        assert book.cash["USDT"] == Decimal("12.345678901234567891")
        assert not book.positions and book.contributions["USDT"] == Decimal("12.345678901234567891")
        credit = db.get(RealWalletFundingCredit,plan["id"])
        for private in (TX_HASH,WALLET,DESTINATION):
            assert private.lower() not in credit.payload_json.lower()
        assert "60000000000000" in credit.payload_json
        assert list_funding(db,account,now=now)[0]["settlement_status"] == "BOOKED"
    backup = vault.backup(tmp_path / "credit-backup.db")
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert settlement_status(db,plan["id"])["booked"]
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    with copied.session() as db:
        assert settlement_status(db,plan["id"])["booked"]
    copied.lock()
    vault.lock()


@pytest.mark.parametrize("change",[
    lambda e:e.update(chain_id="0x1"),
    lambda e:e.update(tx_hash="0x"+"d"*64),
    lambda e:e["transaction"].update(chainId="0x1"),
    lambda e:e["transaction"].update(nonce="0x99"),
    lambda e:e["transaction"].update(type="0x2"),
    lambda e:e["transaction"].update(value="0x1"),
    lambda e:e["transaction"].update(from_="invalid",gas="0x99999"),
    lambda e:e["transaction"].update(input=e["transaction"]["input"][:-1]+"0"),
    lambda e:e["receipt"].update(status="0x0"),
    lambda e:e["receipt"].update(blockHash="0x"+"f"*64),
    lambda e:e["receipt"].update(gasUsed="0x99999"),
    lambda e:e["receipt"]["logs"].append(deepcopy(e["receipt"]["logs"][0])),
    lambda e:e["receipt"]["logs"][0].update(removed=True),
    lambda e:e["receipt"]["logs"][0].update(data="0x"+"0"*64),
    lambda e:e["receipt"]["logs"][0]["topics"].__setitem__(2,"0x"+"1"*64),
    lambda e:e["finalized"].update(number="0x1233"),
    lambda e:e["finalized"].update(hash="0x"+"e"*64),
    lambda e:e["block"].update(timestamp="0x1"),
    lambda e:e.update(receipt=None),
])
def test_rpc_must_exactly_match_immutable_plan_and_finalized_log(tmp_path,change):
    vault,account,now,plan,raw,_ = credited_setup(tmp_path,credited=False)
    change(raw)
    with vault.session() as db:
        with pytest.raises(PortfolioError):
            receipt_observation(db,plan["id"],account,raw,now=now)
        assert db.query(RealWalletFundingCredit).count() == 0
    vault.lock()


@pytest.mark.parametrize("updates",[
    {"amount":"13"},{"destination":"0x"+"5"*40},{"contract":"0x"+"6"*40},{"network":"ERC20"},
])
def test_independent_mexc_credit_must_match_token_network_destination_and_amount(tmp_path,updates):
    vault,account,now,plan,raw,_ = credited_setup(tmp_path,credited=False)
    # Nueva captura contradictoria: ni el resultado anterior ni un txId bastan.
    with vault.session() as db:
        sid = save_transfer_snapshot(db,ACCOUNT_REF,snapshot(now+timedelta(seconds=1),**updates),view_uid="123456789")["id"]
        with pytest.raises(PortfolioError):
            confirm_credit(db,plan["id"],account,raw,sid,now=now+timedelta(seconds=1))
        assert db.query(RealWalletFundingCredit).count() == 0
        assert not account_snapshot(db,account).cash
    vault.lock()


def test_missing_view_credit_remains_pending_and_no_book_is_possible(tmp_path):
    vault,account,now,plan,raw,sid = credited_setup(tmp_path,credited=False,status=1)
    with vault.session() as db:
        pending = confirm_credit(db,plan["id"],account,raw,sid,now=now)
        assert pending["status"] == "PENDING_MEXC" and not pending["exchange_credited"]
        assert pending["mexc_deposit"] == {"code":1,"observed_at":now.isoformat(),"chain_source":"WALLET_RPC_OBSERVED"}
        with pytest.raises(PortfolioError,match="acreditación"):
            book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",wallet_address=WALLET,now=now)
        assert not account_snapshot(db,account).cash
    vault.lock()


def test_mexc_pending_status_keeps_last_matching_capture_and_clears_after_credit(tmp_path):
    vault,account,now,plan,raw,first = credited_setup(tmp_path,credited=False,status=9)
    with vault.session() as db:
        assert confirm_credit(db,plan["id"],account,raw,first,now=now)["mexc_deposit"]["code"] == 9
        later = now+timedelta(seconds=1)
        latest = {**snapshot(now,status=11),"observed_at":later.isoformat()}
        sid = save_transfer_snapshot(db,ACCOUNT_REF,latest,view_uid="123456789")["id"]
        current = confirm_credit(db,plan["id"],account,raw,sid,now=later)["mexc_deposit"]
        assert current["code"] == 11
        assert "mexc_deposit" not in confirm_credit(db,plan["id"],account,raw,first,now=later)
        empty = save_transfer_snapshot(db,ACCOUNT_REF,{**latest,"deposits":[]},view_uid="123456789")["id"]
        assert "mexc_deposit" not in confirm_credit(db,plan["id"],account,raw,empty,now=later)
        assert not account_snapshot(db,account).cash
        assert db.query(RealWalletFundingCredit).count() == 0
    backup = vault.backup(tmp_path / "pending-deposit-backup.db")
    vault.lock()
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    with copied.session() as db:
        history, = list_funding(db,account,now=later)
        assert history["mexc_deposit"] == current and not history["booked"]
        assert TX_HASH not in str(history) and DESTINATION not in str(history)
    copied.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        final_at = later+timedelta(seconds=1)
        final = save_transfer_snapshot(db,ACCOUNT_REF,{**snapshot(now),"observed_at":final_at.isoformat()},
                                       view_uid="123456789")["id"]
        assert confirm_credit(db,plan["id"],account,raw,final,now=final_at)["exchange_credited"]
        history, = list_funding(db,account,now=final_at)
        assert "mexc_deposit" not in history and not history["booked"]
        assert not account_snapshot(db,account).cash
    vault.lock()


def test_pending_status_needs_complete_deposit_terms_and_backup_rejects_altered_link(tmp_path):
    vault,account,now,plan,raw,_ = credited_setup(tmp_path,credited=False,status=11)
    with vault.session() as db:
        incomplete = snapshot(now,status=11)
        incomplete["deposits"][0].pop("destination_identity")
        sid = save_transfer_snapshot(db,ACCOUNT_REF,incomplete,view_uid="123456789")["id"]
        assert "mexc_deposit" not in confirm_credit(db,plan["id"],account,raw,sid,now=now)
        checked = now+timedelta(seconds=1)
        complete = save_transfer_snapshot(db,ACCOUNT_REF,{**snapshot(now,status=11),"observed_at":checked.isoformat()},
                                          view_uid="123456789")["id"]
        assert confirm_credit(db,plan["id"],account,raw,complete,now=checked)["mexc_deposit"]["code"] == 11
        row,_,state = funding_record(db,plan["id"])
        state["mexc_deposit"]["code"] = 9
        row.state_json,row.state_digest = _encoded(state)
        db.commit()
    with pytest.raises(VaultError,match="invalid wallet funding evidence"):
        vault.backup(tmp_path / "altered-deposit-backup.db")
    vault.lock()


def test_tracked_wallet_requires_existing_paired_transfer_without_new_contribution(tmp_path):
    vault,account,now,plan,_,_ = credited_setup(tmp_path)
    with vault.session() as db:
        source = create_account(db,"Wallet propia","USDT").id
        save_wallet_link(db,source,WALLET,"0x38","Principal")
        db.commit()
        append_event(db,source,str(uuid4()),EventInput("CONTRIBUTION","USDT",now-timedelta(days=1),amount=Decimal("100")))
        with pytest.raises(PortfolioError,match="traslado interno"):
            book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",wallet_address=WALLET,now=now)
        transfer = transfer_cash_between_accounts(db,source,account,str(uuid4()),currency="USDT",
            amount=Decimal("12.345678901234567891"),occurred_at=now)
        from app.portfolio.mexc_transfer_matches import confirm_mexc_transfer_match
        credit = db.get(RealWalletFundingCredit,plan["id"])
        confirm_mexc_transfer_match(db,account,credit.snapshot_id,"deposit",credit.source_ref,
            transfer["target_event_id"],confirmation="VINCULAR TRASLADO",now=now)
        result = book_credit(db,plan["id"],account,mode="EXISTING_EVENT",wallet_address=None,
            event_id=transfer["target_event_id"],now=now)
        assert result["booked"]
        assert account_snapshot(db,source).cash["USDT"]+account_snapshot(db,account).cash["USDT"] == Decimal("100")
        other = transfer_cash_between_accounts(db,source,account,str(uuid4()),currency="USDT",
            amount=Decimal("12.345678901234567891"),occurred_at=now)
        with pytest.raises(PortfolioError,match="already linked differently"):
            confirm_mexc_transfer_match(db,account,credit.snapshot_id,"deposit",credit.source_ref,
                other["target_event_id"],confirmation="VINCULAR TRASLADO",now=now)
    vault.lock()


def test_concurrent_booking_with_independent_vault_connections_credits_only_once(tmp_path):
    vault,account,now,plan,_,_ = credited_setup(tmp_path)
    other = RealVault(vault.path)
    other.unlock(PASSWORD)
    barrier = Barrier(2)
    def perform(local):
        barrier.wait()
        with local.session() as db:
            return book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",wallet_address=WALLET,now=now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(perform,[vault,other]))
    assert results[0]["event_id"] == results[1]["event_id"]
    with vault.session() as db:
        assert db.query(RealWalletFundingBooking).count() == 1
        assert account_snapshot(db,account).cash["USDT"] == Decimal("12.345678901234567891")
    other.lock()
    vault.lock()


def test_failed_evidence_write_rolls_back_new_contribution_together(tmp_path,monkeypatch):
    vault,account,now,plan,_,_ = credited_setup(tmp_path)
    import app.portfolio.wallet_funding_credit as module
    def fail(*args):
        raise PortfolioError("fixture evidence failure after event creation")
    monkeypatch.setattr(module,"_book_event",fail)
    with vault.session() as db:
        with pytest.raises(PortfolioError,match="after event creation"):
            book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",wallet_address=WALLET,now=now)
        assert not account_snapshot(db,account).cash
        assert db.query(RealWalletFundingBooking).count() == 0
        assert db.query(RealWalletFundingCredit).count() == 1
    vault.lock()


def test_backup_detects_altered_booking(tmp_path):
    vault,account,now,plan,_,_ = credited_setup(tmp_path)
    with vault.session() as db:
        book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",wallet_address=WALLET,now=now)
        row = db.get(RealWalletFundingBooking,plan["id"])
        row.payload_json = row.payload_json.replace("NEW_CONTRIBUTION","EXISTING_EVENT")
        db.commit()
    with pytest.raises(VaultError,match="funding credit or booking"):
        vault.backup(tmp_path / "tampered.db")
    vault.lock()


def test_v26_migration_preserves_intent_and_adds_empty_credit_tables(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_wallet_funding_bookings")
        db.connection().exec_driver_sql("DROP TABLE real_wallet_funding_credits")
        db.connection().exec_driver_sql("PRAGMA user_version = 26")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    with vault.session() as db:
        assert list_funding(db,account,now=now)[0]["id"] == plan["id"]
        assert db.query(RealWalletFundingCredit).count() == db.query(RealWalletFundingBooking).count() == 0
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
    vault.lock()
