"""Salida CEX → wallet ficticia: pruebas de neto, comisión, vínculo y atomicidad."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID,uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.mexc_uid_guard import claim_mexc_uid_in_transaction
from app.portfolio.domain import EventInput,PortfolioError,realized_pnl_by_currency
from app.portfolio.models import RealMexcWithdrawalBooking,RealMexcWithdrawalReceipt,RealMexcWithdrawalObservation
from app.portfolio.mexc_withdrawal_journal import assert_no_pending_withdrawals,public_record
from app.portfolio.mexc_withdrawal_observation import save_withdrawal_observation
from app.portfolio.mexc_withdrawal_receipt import confirm_receipt
from app.portfolio.mexc_withdrawal_booking import book_withdrawal,resolve_rejected,settlement_status
from app.portfolio.repository import account_snapshot,append_event,create_account,verify_paired_transfers,transfer_cash_between_accounts
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_funding_receipt import TRANSFER
from app.portfolio.wallet_links import save_wallet_link
from tests.test_mexc_withdrawal import setup as base_setup,prepare,claim
from tests.test_wallet_funding import ACCOUNT_REF,WALLET,CONTRACT,PASSWORD,TX_HASH


UID = "123456789"
HOT = "0x"+"8"*40
BLOCK = "0x"+"c"*64


def snapshot(plan,at,*,observed=None,status=7,amount="10",fee="0.01",network="BSC",chain=TX_HASH,
             request=True,destination=WALLET,external="own-withdrawal-id"):
    now = observed or at
    raw = {"direction":"withdrawal","asset":"USDT","network":network,"amount":amount,"fee":fee,"status":status,
        "time_ms":int(at.timestamp()*1000),"source_identity":external,"transfer_type":0,"destination_identity":destination}
    if chain is not None:
        raw["chain_identity"] = chain
    if request:
        raw["request_identity"] = UUID(plan["id"]).hex
    return {"source":"MEXC_SPOT_PRIVATE_READONLY","coverage":"RECENT_WINDOW_BELOW_PAGE_LIMIT","accounting":"OBSERVED_NOT_IMPORTED",
        "start_ms":int((at-timedelta(days=1)).timestamp()*1000),"end_ms":int(now.timestamp()*1000),
        "requests":2,"observed_at":now.isoformat(),"deposits":[],"withdrawals":[raw]}


def evidence(now,*,received="9.99",recipient=WALLET,chain=TX_HASH,contract=CONTRACT):
    from app.portfolio.wallet_funding import exact_units
    tx = {"hash":chain,"chainId":"0x38","from":HOT,"to":contract,"blockNumber":"0x1234","blockHash":BLOCK,"transactionIndex":"0x0"}
    log = {"address":contract,"removed":False,"transactionHash":chain,"blockHash":BLOCK,"blockNumber":"0x1234",
        "transactionIndex":"0x0","logIndex":"0x1","topics":[TRANSFER,"0x"+HOT[2:].zfill(64),"0x"+recipient[2:].zfill(64)],
        "data":"0x"+hex(exact_units(received,18))[2:].zfill(64)}
    receipt = {"transactionHash":chain,"status":"0x1","from":HOT,"to":contract,"blockHash":BLOCK,
        "blockNumber":"0x1234","transactionIndex":"0x0","logs":[log]}
    block = {"number":"0x1234","hash":BLOCK,"timestamp":hex(int(now.timestamp()))}
    return {"chain_id":"0x38","tx_hash":chain,"transaction":tx,"receipt":receipt,"block":block,"finalized":dict(block)}


def setup(tmp_path,*,acknowledged=True,status=7,amount="10",fee="0.01",received="9.99",network="BSC",confirm=True,chain=TX_HASH):
    vault,account,now = base_setup(tmp_path)
    plan = prepare(vault,account,now,amount="10")
    assert claim(vault,account,now,plan)["claim_granted"]
    with vault.session() as db:
        if acknowledged:
            from app.portfolio.mexc_withdrawal_journal import acknowledge_withdrawal
            acknowledge_withdrawal(db,plan["id"],"own-withdrawal-id",now=now)
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        claim_mexc_uid_in_transaction(db,ACCOUNT_REF,UID,now=now)
        db.commit()
        source = snapshot(plan,now,status=status,amount=amount,fee=fee,network=network,chain=chain)
        observed,_ = save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,source,UID,now=now)
        if confirm:
            confirm_receipt(db,plan["id"],account,WALLET,observed["observation_id"],evidence(now,received=received),now=now)
    return vault,account,now,plan,observed["observation_id"]


def balance(now,amount="90"):
    return {"balances":{"free":amount,"locked":"0"},"observed_at":now.isoformat()}


def book(vault,account,now,plan,observation,**changes):
    values = dict(address=WALLET,mode="NEW_EXTERNAL",view=balance(now),uid=UID,observation_id=observation,now=now)
    values.update(changes)
    with vault.session() as db:
        return book_withdrawal(db,plan["id"],account,**values)


@pytest.mark.parametrize("received,reported,remaining,model",[("9.99","10","90","DEDUCTED"),("10","10","89.99","ADDED"),("9.99","9.99","90","DEDUCTED")])
def test_real_net_and_fee_explain_debit_and_outside_flow_without_fake_profit(tmp_path,received,reported,remaining,model):
    vault,account,now,plan,observation = setup(tmp_path,amount=reported,received=received)
    with vault.session() as db:
        before = settlement_status(db,plan["id"])
        assert before["wallet_received"] and not before["booked"] and before["fee_model"] == model
        assert account_snapshot(db,account).cash["USDT"] == Decimal("100")
        with pytest.raises(PortfolioError):
            assert_no_pending_withdrawals(db,ACCOUNT_REF,now)
    result = book(vault,account,now,plan,observation,view=balance(now,remaining))
    assert result["booked"] and result["resolved"] and not result["trading_reopened"]
    assert book(vault,account,now,plan,observation,view=balance(now,remaining)) == result
    with vault.session() as db:
        state = account_snapshot(db,account)
        assert state.cash["USDT"] == Decimal(remaining) and state.withdrawals["USDT"] == Decimal(received)
        assert state.cash_fees["USDT"] == Decimal("0.01") and realized_pnl_by_currency(state)["USDT"] == Decimal("-0.01")
        assert_no_pending_withdrawals(db,ACCOUNT_REF,now)
        public = public_record(db,plan["id"],now)
        assert public["status"] in ("ACKNOWLEDGED","SUBMISSION_UNKNOWN") and public["booked"]
        for row in (db.get(RealMexcWithdrawalReceipt,plan["id"]),db.get(RealMexcWithdrawalBooking,plan["id"]),db.get(RealMexcWithdrawalObservation,observation)):
            assert WALLET not in row.payload_json and TX_HASH not in row.payload_json and HOT not in row.payload_json
    backup = vault.backup(tmp_path / "receipt-backup.db")
    vault.lock()
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    with copied.session() as db:
        assert settlement_status(db,plan["id"])["booked"]
    copied.lock()


def test_internal_transfer_keeps_net_in_wallet_without_new_contribution_and_fee_once(tmp_path):
    vault,account,now,plan,observation = setup(tmp_path)
    with vault.session() as db:
        target = create_account(db,"Wallet propia","USDT").id
        save_wallet_link(db,target,WALLET,"0x38","Principal")
        db.commit()
    with pytest.raises(PortfolioError,match="wallet está registrada"):
        book(vault,account,now,plan,observation)
    result = book(vault,account,now,plan,observation,mode="NEW_INTERNAL",target_account_id=target)
    assert result["booked"]
    with vault.session() as db:
        left,right = account_snapshot(db,account),account_snapshot(db,target)
        assert left.cash["USDT"] == Decimal("90") and right.cash["USDT"] == Decimal("9.99")
        assert left.cash["USDT"]+right.cash["USDT"] == Decimal("99.99")
        assert not left.withdrawals and not right.contributions and realized_pnl_by_currency(left)["USDT"] == Decimal("-0.01")
        assert verify_paired_transfers(db) == 1
    vault.lock()


def test_unknown_post_can_recover_by_client_request_id_and_network_absence_is_explicit(tmp_path):
    vault,account,now,plan,observation = setup(tmp_path,acknowledged=False,network=None)
    with vault.session() as db:
        result = settlement_status(db,plan["id"])
        assert result["network_evidence"] == "PLAN_AND_RPC_HISTORY_NETWORK_ABSENT"
        assert result["wallet_received"]
    assert book(vault,account,now,plan,observation)["booked"]
    vault.lock()


@pytest.mark.parametrize("change",[
    lambda e:e.update(chain_id="0x1"),lambda e:e.update(tx_hash="0x"+"d"*64),
    lambda e:e["transaction"].update(chainId="0x1"),lambda e:e["receipt"].update(status="0x0"),
    lambda e:e["finalized"].update(number="0x1233"),lambda e:e["block"].update(hash="0x"+"e"*64),
    lambda e:e["receipt"]["logs"][0].update(removed=True),lambda e:e["receipt"]["logs"][0].update(address=HOT),
    lambda e:e["receipt"]["logs"][0]["topics"].__setitem__(2,"0x"+HOT[2:].zfill(64)),
    lambda e:e["receipt"]["logs"].append(deepcopy(e["receipt"]["logs"][0])),
    lambda e:e.update(receipt=None),
])
def test_wrong_chain_hash_receipt_log_recipient_or_finality_never_credits_wallet(tmp_path,change):
    vault,account,now,plan,observation = setup(tmp_path,confirm=False)
    rpc = evidence(now)
    change(rpc)
    with vault.session() as db:
        with pytest.raises(PortfolioError):
            confirm_receipt(db,plan["id"],account,WALLET,observation,rpc,now=now)
        assert db.query(RealMexcWithdrawalReceipt).count() == 0
        assert account_snapshot(db,account).cash["USDT"] == Decimal("100")
    vault.lock()


def test_batch_to_other_recipients_is_supported_without_counting_their_tokens(tmp_path):
    vault,account,now,plan,observation = setup(tmp_path,confirm=False)
    rpc = evidence(now)
    other = deepcopy(rpc["receipt"]["logs"][0])
    other["logIndex"],other["topics"][2] = "0x2","0x"+HOT[2:].zfill(64)
    rpc["receipt"]["logs"].append(other)
    with vault.session() as db:
        result = confirm_receipt(db,plan["id"],account,WALLET,observation,rpc,now=now)
        assert Decimal(result["received"]) == Decimal("9.99")
    vault.lock()


def test_final_status_or_fee_regression_is_kept_as_evidence_and_blocks_booking(tmp_path):
    vault,account,now,plan,observation = setup(tmp_path)
    with vault.session() as db:
        with pytest.raises(PortfolioError,match="estado final"):
            save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,snapshot(plan,now,observed=now+timedelta(seconds=1),status=6),UID,now=now+timedelta(seconds=1))
        assert db.query(RealMexcWithdrawalObservation).count() == 2
    with pytest.raises(PortfolioError):
        book(vault,account,now+timedelta(seconds=1),plan,observation)
    vault.lock()


def test_exceeded_fee_is_recorded_as_fact_but_requires_explicit_review_before_book(tmp_path):
    vault,account,now,plan,observation = setup(tmp_path,fee="0.03",received="9.97")
    with pytest.raises(PortfolioError,match="superó el filtro"):
        book(vault,account,now,plan,observation)
    assert book(vault,account,now,plan,observation,accept_fee_override=True)["booked"]
    with vault.session() as db:
        assert account_snapshot(db,account).cash_fees["USDT"] == Decimal("0.03")
    vault.lock()


def test_missing_request_id_can_use_known_ack_but_not_an_unknown_submission(tmp_path):
    vault,account,now,plan,_ = setup(tmp_path,confirm=False)
    with vault.session() as db:
        result,_ = save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,snapshot(plan,now,request=False),UID,now=now)
        assert result["observation_id"]
    vault.lock()
    other = tmp_path / "unknown"
    other.mkdir()
    vault,account,now,plan,_ = setup(other,acknowledged=False,confirm=False)
    with vault.session() as db:
        result,_ = save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,snapshot(plan,now,request=False),UID,now=now)
        assert result["status"] == "NOT_FOUND_IN_RECENT_VIEW"
    vault.lock()


def test_concurrent_booking_and_failure_after_insertion_are_atomic(tmp_path,monkeypatch):
    vault,account,now,plan,observation = setup(tmp_path)
    import app.portfolio.mexc_withdrawal_booking as module
    original = module._source_event
    def fail(db,*args):
        assert db.connection().connection.driver_connection.in_transaction
        raise PortfolioError("fixture failure after writing source event")
    monkeypatch.setattr(module,"_source_event",fail)
    with pytest.raises(PortfolioError,match="after writing"):
        book(vault,account,now,plan,observation)
    with vault.session() as db:
        assert account_snapshot(db,account).cash["USDT"] == Decimal("100")
    monkeypatch.setattr(module,"_source_event",original)
    other = RealVault(vault.path)
    other.unlock(PASSWORD)
    barrier = Barrier(2)
    def perform(local):
        barrier.wait()
        return book(local,account,now,plan,observation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(result["booked"] for result in pool.map(perform,[vault,other]))
    with vault.session() as db:
        assert db.query(RealMexcWithdrawalBooking).count() == 1
        assert account_snapshot(db,account).cash["USDT"] == Decimal("90")
    other.lock()
    vault.lock()


def test_existing_manual_event_is_linked_without_charging_twice(tmp_path):
    vault,account,now,plan,observation = setup(tmp_path)
    with vault.session() as db:
        from app.portfolio.mexc_withdrawal_booking import booking_options
        event = append_event(db,account,str(uuid4()),EventInput("WITHDRAWAL","USDT",now,amount=Decimal("9.99"),fee=Decimal("0.01")))
        assert booking_options(db,plan["id"],account,WALLET)["events"][0]["id"] == event.id
    assert book(vault,account,now,plan,observation,mode="EXISTING_EVENT",event_id=event.id)["booked"]
    with vault.session() as db:
        assert account_snapshot(db,account).cash["USDT"] == Decimal("90")
    vault.lock()


@pytest.mark.parametrize("status",[8,9])
def test_rejection_requires_no_hash_fee_debit_and_never_creates_a_cash_event(tmp_path,status):
    vault,account,now,plan,observation = setup(tmp_path,status=status,fee="0",chain=None,confirm=False)
    with vault.session() as db:
        result = resolve_rejected(db,plan["id"],account,observation,balance(now,"100"),UID,now=now)
        assert result["resolved"] and result["settlement_status"] == "REJECTED_CONFIRMED"
        assert not result["wallet_received"] and not result["booked"]
        assert account_snapshot(db,account).cash["USDT"] == Decimal("100")
        assert_no_pending_withdrawals(db,ACCOUNT_REF,now)
        assert db.get(RealMexcWithdrawalBooking,plan["id"]).source_event_id is None
    assert not claim(vault,account,now,plan)["claim_granted"]
    vault.lock()


@pytest.mark.parametrize("chain,fee,remaining",[(TX_HASH,"0","100"),(None,"0.01","100"),(None,"0","99.99")])
def test_rejection_with_hash_fee_or_changed_balance_stays_unresolved(tmp_path,chain,fee,remaining):
    vault,account,now,plan,observation = setup(tmp_path,status=8,fee=fee,chain=chain,confirm=False)
    with vault.session() as db:
        with pytest.raises(PortfolioError):
            resolve_rejected(db,plan["id"],account,observation,balance(now,remaining),UID,now=now)
        assert db.get(RealMexcWithdrawalBooking,plan["id"]) is None
    vault.lock()


def test_failed_history_cannot_erase_a_previously_observed_pending_chain_hash(tmp_path):
    vault,account,now,plan,_ = setup(tmp_path,status=4,fee="0",chain=TX_HASH,confirm=False)
    later = now+timedelta(seconds=1)
    with vault.session() as db:
        observed,_ = save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,
            snapshot(plan,now,observed=later,status=8,fee="0",chain=None),UID,now=later)
        with pytest.raises(PortfolioError):
            resolve_rejected(db,plan["id"],account,observed["observation_id"],balance(later,"100"),UID,now=later)
    vault.lock()


def test_success_without_hash_then_hash_arrival_pins_it_against_later_changes(tmp_path):
    vault,account,now,plan,_ = setup(tmp_path,status=7,chain=None,confirm=False)
    with vault.session() as db:
        later = now+timedelta(seconds=1)
        save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,snapshot(plan,now,observed=later),UID,now=later)
        later += timedelta(seconds=1)
        with pytest.raises(PortfolioError,match="hash"):
            save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,
                snapshot(plan,now,observed=later,chain="0x"+"d"*64),UID,now=later)
        assert db.query(RealMexcWithdrawalObservation).count() == 3
        from app.portfolio.models import RealLiveStop
        assert db.get(RealLiveStop,ACCOUNT_REF).stopped
    vault.lock()


@pytest.mark.parametrize("mutation",["network","recipient","uid","amount"])
def test_wrong_view_facts_do_not_book_or_erase_conflicting_evidence(tmp_path,mutation):
    vault,account,now,plan,_ = setup(tmp_path,status=4,fee="0",chain=None,confirm=False)
    later = now+timedelta(seconds=1)
    captured = snapshot(plan,now,observed=later)
    uid = UID
    if mutation == "network": captured["withdrawals"][0]["network"] = "ETH"
    if mutation == "recipient": captured["withdrawals"][0]["destination_identity"] = HOT
    if mutation == "uid": uid = "987654321"
    if mutation == "amount": captured["withdrawals"][0]["amount"] = "15"
    with vault.session() as db:
        if mutation == "amount":
            observed,_ = save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,captured,uid,now=later)
            with pytest.raises(PortfolioError):
                confirm_receipt(db,plan["id"],account,WALLET,observed["observation_id"],evidence(later),now=later)
        else:
            with pytest.raises(PortfolioError):
                save_withdrawal_observation(db,plan["id"],account,ACCOUNT_REF,captured,uid,now=later)
        assert db.get(RealMexcWithdrawalReceipt,plan["id"]) is None
        assert account_snapshot(db,account).cash["USDT"] == Decimal("100")
    vault.lock()


def test_one_wei_fee_stays_exact_and_is_a_cost_not_a_cash_outflow(tmp_path):
    vault,account,now,plan,observation = setup(tmp_path,fee="0.000000000000000001",received="9.999999999999999999")
    assert book(vault,account,now,plan,observation)["booked"]
    with vault.session() as db:
        state = account_snapshot(db,account)
        assert state.withdrawals["USDT"] == Decimal("9.999999999999999999")
        assert realized_pnl_by_currency(state)["USDT"] == Decimal("-0.000000000000000001")
    vault.lock()


def test_v28_migration_keeps_an_inflight_withdrawal_and_adds_receipt_tables(tmp_path):
    vault,account,now,plan,_ = setup(tmp_path,confirm=False)
    with vault.session() as db:
        for table in ("real_mexc_withdrawal_bookings","real_mexc_withdrawal_receipts","real_mexc_withdrawal_observations"):
            db.connection().exec_driver_sql("DROP TABLE "+table)
        db.connection().exec_driver_sql("PRAGMA user_version = 28")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert public_record(db,plan["id"],now)["status"] == "ACKNOWLEDGED"
        assert db.query(RealMexcWithdrawalObservation).count() == 0
        assert account_snapshot(db,account).cash["USDT"] == Decimal("100")
    vault.lock()


@pytest.mark.parametrize("model",[RealMexcWithdrawalReceipt,RealMexcWithdrawalBooking,RealMexcWithdrawalObservation])
def test_backup_rejects_changed_receipt_booking_or_observation(tmp_path,model):
    vault,account,now,plan,observation = setup(tmp_path)
    book(vault,account,now,plan,observation)
    with vault.session() as db:
        row = db.query(model).first()
        row.payload_digest = "0"*64
        db.commit()
    with pytest.raises(VaultError):
        vault.backup(tmp_path / "must-not-export.db")
    vault.lock()
